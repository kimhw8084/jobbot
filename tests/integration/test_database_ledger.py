from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT
from jobbot.db import Database, apply_pending
from jobbot.legacy_engine import PrecisionStore, select_daily_plan
from jobbot.scoring import score_job
from jobbot.strategy_runtime import fallback_activation_enabled

from tests.helpers import bundle_with_database, scored


class DatabaseLedgerIntegrationTests(unittest.TestCase):
    def test_fresh_migrations_are_current_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = bundle_with_database(Path(td) / "jobs.sqlite3")
            result = Database(bundle).migrate()
            self.assertEqual(result.applied, tuple(range(1, 23)))
            conn = Database(bundle).connect()
            try:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(
                    conn.execute("SELECT type FROM sqlite_master WHERE name='source_occurrences'").fetchone()[0],
                    "table",
                )
                self.assertIsNone(conn.execute("SELECT type FROM sqlite_master WHERE name='occurrences'").fetchone())
                self.assertIsNone(conn.execute("SELECT max_results FROM browser_search_tasks LIMIT 1").fetchone())
                fields = {row[1] for row in conn.execute("PRAGMA table_info(search_task_results)")}
                task_fields = {row[1] for row in conn.execute("PRAGMA table_info(browser_search_tasks)")}
                occurrence_fields = {row[1] for row in conn.execute("PRAGMA table_info(source_occurrences)")}
                self.assertTrue({"title_hint", "card_json", "detail_status", "detail_lease_until"} <= fields)
                self.assertTrue({"cards_extracted", "cards_persistence_succeeded", "execution_rank", "phase"} <= task_fields)
                self.assertTrue({"strategy_profile", "query_family", "query_kind", "query_pass", "initial_order"} <= task_fields)
                self.assertTrue({"baseline_execution_rank", "effective_execution_rank", "learned_order_reason", "learned_order_sample_size", "ordering_algorithm_version"} <= task_fields)
                self.assertTrue({"provider_metadata_json", "provider_requests_submitted", "provider_records_delivered", "provider_reported_cost_json"} <= task_fields)
                self.assertTrue({"strategy_profile", "query_family", "query_kind", "query_pass", "initial_order"} <= occurrence_fields)
                job_fields = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertTrue({"discovery_url", "board_detail_url", "observed_board_apply_url", "employer_job_url", "ats_requisition_url", "verified_application_url"} <= job_fields)
                self.assertTrue({"identity_evidence_state", "detail_evidence_state", "requirements_evidence_state", "source_verification_state", "application_destination_verification_state", "evidence_readiness_state", "qualification_readiness_state", "evidence_readiness_json"} <= job_fields)
                self.assertEqual(conn.execute("SELECT type FROM sqlite_master WHERE name='extension_refresh_requests'").fetchone()[0], "table")
                refresh_fields = {row[1] for row in conn.execute("PRAGMA table_info(extension_refresh_requests)")}
                self.assertTrue({"observed_source_identity", "observed_deployment_root", "diagnostics_json"} <= refresh_fields)
            finally: conn.close()

    def test_existing_ledger_migration_uses_sqlite_backup_and_preserves_count(self) -> None:
        source = PROJECT_ROOT / "data" / "jobs.sqlite3"
        if not source.is_file(): self.skipTest("local historical ledger is unavailable")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "jobs.sqlite3"
            src, dst = sqlite3.connect(source), sqlite3.connect(target)
            try: src.backup(dst)
            finally: dst.close(); src.close()
            conn = sqlite3.connect(target)
            before = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            conn.execute("DELETE FROM schema_migrations")
            conn.execute("DROP TABLE IF EXISTS job_diffs"); conn.execute("DROP TABLE IF EXISTS funnel_events"); conn.execute("DROP TABLE IF EXISTS source_verifications")
            conn.commit(); conn.close()
            bundle = bundle_with_database(target)
            result = Database(bundle).migrate()
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(result.integrity_after, "ok")
            conn = sqlite3.connect(target)
            try: self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], before)
            finally: conn.close()

    def test_migration_history_must_remain_a_sequential_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            conn.execute("INSERT INTO schema_migrations VALUES(2,'durable normal-Chrome search tasks','2026-09-07T00:00:00+00:00')")
            conn.commit()
            with self.assertRaisesRegex(RuntimeError, "sequential prefix"):
                apply_pending(conn)
            self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(2,)])
            conn.close()

    def test_fallback_activation_uses_the_configured_unapplied_reservoir_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = bundle_with_database(Path(td) / "jobs.sqlite3")
            Database(bundle).migrate()
            conn = Database(bundle).connect()
            self.assertTrue(fallback_activation_enabled(conn, bundle.runtime))
            conn.executemany(
                """INSERT INTO jobs(job_id,remote_gate,recommendation,application_status,is_active,posting_status,first_seen,last_seen,evidence_readiness_state,qualification_readiness_state)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [(f"reservoir-{index}", "pass", "APPLY_NOW", "NEW", 1, "", "2026-09-14T00:00:00+00:00", "2026-09-14T00:00:00+00:00", "READY", "READY") for index in range(50)],
            )
            conn.commit()
            self.assertFalse(fallback_activation_enabled(conn, bundle.runtime))
            conn.close()

    def test_new_unchanged_updated_closed_reopened_and_cross_source_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            Database(bundle_with_database(path)).migrate()
            store = PrecisionStore(path)
            try:
                first = scored("Patient Enrollment Specialist", "Fully remote healthcare enrollment. Required Qualifications: 2 years relevant experience. Full-time permanent. " + "HIPAA Excel documentation. " * 30)
                first.raw = {"_board": "example"}
                self.assertEqual(store.upsert(first), "new")
                self.assertEqual(store.upsert(first), "unchanged")
                mirror = scored(first.title, first.description, source="indeed")
                mirror.source_job_id = "indeed-900"; mirror.canonical_url = "https://www.indeed.com/viewjob?jk=indeed-900"; mirror.apply_url = first.apply_url
                self.assertEqual(store.upsert(mirror), "unchanged")
                changed = scored(
                    first.title,
                    first.description
                    + " Required Qualifications: 3 years of healthcare enrollment. Evening schedule in Central Time. RN preferred.",
                )
                changed.salary_text = "$60,000 - $70,000"; changed.raw = {"_board": "example"}
                self.assertEqual(store.upsert(changed), "updated")
                job_id = store.resolve_job_id(first)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM source_occurrences").fetchone()[0], 2)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (job_id,)).fetchone()[0], 2)
                self.assertGreater(store.conn.execute("SELECT COUNT(*) FROM job_diffs WHERE job_id=?", (job_id,)).fetchone()[0], 0)
                diff_rows = {
                    row["field_name"]: row
                    for row in store.conn.execute("SELECT * FROM job_diffs WHERE job_id=?", (job_id,))
                }
                self.assertIn("description", diff_rows)
                self.assertIn("requirements", diff_rows)
                self.assertIn("licenses_certifications", diff_rows)
                self.assertIn("schedule", diff_rows)
                self.assertNotEqual(diff_rows["description"]["old_value_json"], "null")
                self.assertNotEqual(diff_rows["description"]["new_value_json"], "null")
                self.assertEqual(store.reconcile_complete_board("greenhouse", "example", set(), 1, 2), 0)
                self.assertEqual(store.reconcile_complete_board("greenhouse", "example", set(), 2, 2), 1)
                self.assertEqual(store.conn.execute("SELECT change_status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "CLOSED")
                self.assertEqual(store.upsert(changed), "updated")
                self.assertEqual(store.conn.execute("SELECT change_status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "REOPENED")
            finally: store.close()

    def test_daily_planner_returns_15_without_weakening_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"; bundle = bundle_with_database(path); Database(bundle).migrate(); store = PrecisionStore(path)
            try:
                for index in range(24):
                    job = scored(
                        "Patient Enrollment Specialist",
                        "Example Health is currently accepting applications for a fully remote United States role. "
                        "This is a full-time permanent employee position. The employer provides a $60,000 annual base salary. "
                        "No travel required. No required office days. No onsite training. No field work. No in-person events. "
                        "Healthcare patient enrollment operations include reviewing enrollment records, verifying documentation, "
                        "resolving discrepancies, coordinating workflow handoffs, and preparing accurate case records. "
                        "Required Qualifications: 2 years of healthcare enrollment or relevant operations experience. "
                        f"HIPAA documentation and Excel workflows. Requisition reference {index}."
                    )
                    job.salary_text = "$60,000/year"; job.salary_min = 60000; job.salary_max = 60000
                    job.raw = {"posting_status": "active"}
                    score_job(job, bundle.strategy, bundle.legacy_runtime()["candidate"])
                    self.assertIn(job.recommendation, {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}, job.qualification_gates)
                    job.source_job_id = f"job-{index}"; job.canonical_url = f"https://boards.greenhouse.io/example/jobs/{index}"; job.apply_url = job.canonical_url; job.company = f"Employer {index % 5}"
                    store.upsert(job)
                plan = select_daily_plan(store.rows(active_only=True), bundle.strategy, 15)
                self.assertEqual(len(plan), 15, [
                    (row["recommendation"], row["application_status"], json.loads(row["qualification_gates_json"]))
                    for row in store.rows(active_only=True)
                ])
                self.assertTrue(all(row["recommendation"] in {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"} for row in plan))
                self.assertTrue(all(row["remote_gate"] == "pass" for row in plan))
            finally: store.close()


if __name__ == "__main__": unittest.main()
