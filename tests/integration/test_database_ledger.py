from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT
from jobbot.db import Database
from jobbot.legacy_engine import PrecisionStore, select_daily_plan

from tests.helpers import bundle_with_database, scored


class DatabaseLedgerIntegrationTests(unittest.TestCase):
    def test_fresh_migrations_are_current_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = bundle_with_database(Path(td) / "jobs.sqlite3")
            result = Database(bundle).migrate()
            self.assertEqual(result.applied, (1, 2, 3, 4, 5, 6))
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
                    job = scored("Patient Enrollment Specialist", "Fully remote healthcare enrollment. Required Qualifications: 2 years relevant experience. Full-time permanent with benefits.")
                    job.source_job_id = f"job-{index}"; job.canonical_url = f"https://boards.greenhouse.io/example/jobs/{index}"; job.apply_url = job.canonical_url; job.company = f"Employer {index % 5}"
                    store.upsert(job)
                plan = select_daily_plan(store.rows(active_only=True), bundle.strategy, 15)
                self.assertEqual(len(plan), 15)
                self.assertTrue(all(row["recommendation"] in {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"} for row in plan))
                self.assertTrue(all(row["remote_gate"] == "pass" for row in plan))
            finally: store.close()


if __name__ == "__main__": unittest.main()
