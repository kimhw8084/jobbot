from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.db import Database
from jobbot.discoveries import claim_next_detail, upsert_card
from jobbot.query_yield import definition_is_due, economics, mark_definition_completed, query_yield_estimate, record_task_yield
from jobbot.legacy_engine import PrecisionStore, select_daily_plan
from jobbot.search_plan import compile_plan, compile_staged_plan, plan_counts
from jobbot.search_strategy import BANDS, cadence_economics, detail_priority, search_band, wilson_lower_bound
from tests.helpers import bundle_with_database, scored


class SearchPrecisionTests(unittest.TestCase):
    def test_bands_are_explicit_and_fastest_order_is_gold_first(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        tasks = compile_plan(bundle, "deep", ["linkedin"])
        self.assertEqual(len(tasks), 139)
        self.assertEqual({task.search_band for task in tasks}, set(BANDS))
        self.assertEqual(tasks[0].search_band, "GOLD")
        self.assertEqual(tasks[0].canonical_title, "Patient Enrollment Specialist")
        self.assertEqual(tasks[0].query_text, "patient enrollment specialist")
        self.assertEqual(tasks[0].resume_variant, "enrollment_operations")
        quality = next(task for task in tasks if task.canonical_title == "Healthcare Quality Analyst")
        implementation = next(task for task in tasks if task.canonical_title == "Healthcare Implementation Coordinator")
        self.assertEqual(quality.search_band, "GROWTH")
        self.assertEqual(quality.resume_variant, "healthcare_qa")
        self.assertEqual(implementation.resume_variant, "healthcare_qa")
        self.assertNotEqual(search_band("Credentialing Specialist", "HEALTHCARE_OPS_ACCESS", bundle.strategy), "GOLD")

    def test_query_text_can_differ_from_canonical_role_without_duplicate_execution(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        tasks = compile_plan(bundle, "deep", ["linkedin"])
        process = [task for task in tasks if task.canonical_title == "Business Process Analyst — Healthcare"]
        self.assertEqual(len(process), 1)
        self.assertEqual(process[0].query_text, "healthcare business process analyst")
        self.assertEqual(len({task.query_text for task in tasks}), len(tasks))
        self.assertEqual(plan_counts(compile_staged_plan(bundle, ["linkedin"]))["total"], 278)

    def test_cadence_reduces_repeat_starts_without_reducing_definitions(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        tasks = compile_plan(bundle, "deep", ["linkedin"])
        counts = {band: sum(task.search_band == band for task in tasks) for band in BANDS}
        economics_result = cadence_economics(counts)
        self.assertTrue(economics_result["coverage_preserved"])
        self.assertGreater(economics_result["theoretical_reduction_percent"], 0)
        self.assertGreater(economics_result["baseline_definitions_per_day"], economics_result["steady_state_definitions_per_day"])

    def test_detail_priority_is_scheduling_only_and_gold_claims_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            conn = Database(bundle).connect()
            try:
                now = "2026-09-09T12:00:00+00:00"
                run_id = conn.execute(
                    "INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)",
                    ("test", "precision", "linkedin", "running", now),
                ).lastrowid
                task_id = conn.execute(
                    """INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at,search_band,task_key)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (run_id, "linkedin", "patient enrollment specialist", 7, "https://www.linkedin.com/jobs/search/", "running", now, "GOLD", "gold-task"),
                ).lastrowid
                upsert_card(conn, run_id=run_id, task_id=task_id, platform="linkedin", source_job_id="tail", source_url="https://www.linkedin.com/jobs/view/tail", title_hint="Data Scientist", detail_priority=100, detail_priority_reason="deep_tail")
                upsert_card(conn, run_id=run_id, task_id=task_id, platform="linkedin", source_job_id="gold", source_url="https://www.linkedin.com/jobs/view/gold", title_hint="Patient Enrollment Specialist", detail_priority=500, detail_priority_reason="gold")
                conn.commit()
                claimed = claim_next_detail(conn, run_id=run_id, task_id=task_id, worker_id="test")
                self.assertEqual(claimed.source_job_id, "gold")
                self.assertEqual(claimed.detail_priority, 500)
                self.assertEqual(claimed.detail_priority_reason, "gold")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results",).fetchone()[0], 2)
            finally:
                conn.close()

    def test_cadence_state_is_durable_and_incomplete_work_is_not_scheduled_as_negative(self) -> None:
        self.assertTrue(definition_is_due(None, "2026-09-09T00:00:00+00:00"))
        self.assertFalse(definition_is_due({"next_due_at": "2026-09-10T00:00:00+00:00"}, "2026-09-09T00:00:00+00:00"))
        row = {"completed_descriptions": 29, "apply_now": 20, "apply_volume": 5, "observation_dates": '["2026-09-01"]'}
        self.assertFalse(query_yield_estimate(row)["sample_eligible"])
        row["completed_descriptions"] = 30
        row["observation_dates"] = '["2026-09-01", "2026-09-09"]'
        self.assertTrue(query_yield_estimate(row)["sample_eligible"])
        self.assertGreater(wilson_lower_bound(25, 30), 0.5)

    def test_query_yield_is_durable_idempotent_and_excludes_missing_details(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            store = PrecisionStore(path)
            try:
                now = "2026-09-09T12:00:00+00:00"
                run_id = store.conn.execute(
                    "INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)",
                    ("test", "precision", "linkedin", "running", now),
                ).lastrowid
                task_id = store.conn.execute(
                    """INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at,completed_at,search_band,cadence_hours,task_key)
                       VALUES(?,?,?,?,?,'exhausted',?,?,?,?,?)""",
                    (run_id, "linkedin", "patient enrollment specialist", 7, "https://www.linkedin.com/jobs/search/", now, now, "GOLD", 6, "yield-task"),
                ).lastrowid
                job = scored("Patient Enrollment Specialist", "Fully remote healthcare enrollment. " + "Required Qualifications and HIPAA documentation. " * 30)
                store.upsert(job, commit=False)
                job_id = store.resolve_job_id(job)
                store.conn.execute("UPDATE jobs SET description_state='COMPLETE' WHERE job_id=?", (job_id,))
                store.conn.execute(
                    """INSERT INTO search_task_results(task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,browser_run_id,detail_status,canonical_job_id)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (task_id, "linkedin", "yield-1", "https://www.linkedin.com/jobs/view/yield-1", now, now, run_id, "COMPLETE", job_id),
                )
                store.conn.commit()
                task = store.conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()
                mark_definition_completed(store.conn, task, "exhausted", now)
                self.assertTrue(record_task_yield(store.conn, task_id, now))
                self.assertFalse(record_task_yield(store.conn, task_id, now))
                stats = store.conn.execute("SELECT cards_persisted,completed_descriptions,apply_now,observation_runs FROM query_yield_stats").fetchone()
                self.assertEqual(tuple(stats), (1, 1, 1, 1))
                self.assertEqual(store.conn.execute("SELECT next_due_at FROM search_definition_state WHERE task_key='yield-task'").fetchone()[0], "2026-09-09T18:00:00+00:00")
                self.assertEqual(economics(store.conn)["bands"]["GOLD"]["queries"], 1)
            finally:
                store.close()

    def test_search_precision_fixture_bands_do_not_promote_growth_or_hard_noise(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        gold = [
            "Bilingual Enrollment Coordinator", "Patient Access Specialist",
            "Eligibility Specialist", "Insurance Verification Specialist", "RPM Coordinator",
        ]
        for title in gold:
            self.assertEqual(search_band(title, "HEALTHCARE_OPS_ACCESS", bundle.strategy), "GOLD", title)
        for title in ["Healthcare Data Quality Specialist", "Healthcare Quality Analyst", "Healthcare Implementation Coordinator", "Healthcare Data Analyst"]:
            self.assertEqual(search_band(title, "HEALTHCARE_QUALITY_DATA", bundle.strategy), "GROWTH", title)
        for title in ["Higher Education Records Specialist", "Bilingual Content Reviewer"]:
            self.assertEqual(search_band(title, "HIGHER_ED_EDTECH" if "Education" in title else "CONTENT_AI_QUALITY", bundle.strategy), "HEDGE", title)
        for title in ["Credentialing Specialist", "Care Coordinator", "Provider Enrollment Specialist"]:
            self.assertNotEqual(search_band(title, "HEALTHCARE_OPS_ACCESS", bundle.strategy), "GOLD", title)
        score, reason = detail_priority(title="Senior Software Engineer", query_text="patient enrollment specialist", search_band="GOLD")
        self.assertIn("occupation_review", reason)
        self.assertLess(score, 500)

    def test_today_precision_gate_does_not_pad_a_small_apply_ready_reservoir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            store = PrecisionStore(path)
            try:
                ids = []
                for index in range(20):
                    job = scored("Patient Enrollment Specialist", "Fully remote healthcare enrollment. " + "Required Qualifications and HIPAA documentation. " * 20)
                    job.source_job_id = f"today-{index}"
                    job.canonical_url = f"https://boards.greenhouse.io/example/jobs/today-{index}"
                    job.apply_url = job.canonical_url
                    job.company = f"Employer {index}"
                    store.upsert(job)
                    ids.append(store.resolve_job_id(job))
                store.conn.executemany("UPDATE jobs SET recommendation=? WHERE job_id=?", [("APPLY_NOW" if index < 8 else "HIGH_VALUE_STRETCH", job_id) for index, job_id in enumerate(ids)])
                store.conn.commit()
                plan = select_daily_plan(store.rows(active_only=True), bundle.strategy, 15)
                self.assertEqual(len(plan), 8)
                for index in range(4):
                    store.conn.execute("UPDATE jobs SET recommendation='APPLY_VOLUME' WHERE job_id=?", (ids[8 + index],))
                store.conn.commit()
                plan = select_daily_plan(store.rows(active_only=True), bundle.strategy, 15)
                ready = sum(row["recommendation"] in {"APPLY_NOW", "APPLY_VOLUME"} for row in plan)
                self.assertEqual(len(plan), 15)
                self.assertGreaterEqual(ready / len(plan), 0.8)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
