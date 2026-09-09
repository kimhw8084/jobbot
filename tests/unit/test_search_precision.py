from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.candidate import readiness_warnings
from jobbot.db import Database
from jobbot.discoveries import claim_next_detail, upsert_card
from jobbot.query_yield import definition_is_due, economics, mark_definition_completed, query_yield_estimate, record_task_yield
from jobbot.legacy_engine import PrecisionStore, select_daily_plan
from jobbot.search_plan import compile_plan, compile_staged_plan, plan_counts
from jobbot.search_strategy import BANDS, cadence_economics, detail_priority, search_band, staged_cadence_economics, wilson_lower_bound
from jobbot.bridge.rpc import task_selection_key
from tests.helpers import bundle_with_database, scored


class SearchPrecisionTests(unittest.TestCase):
    def test_bands_are_explicit_and_fastest_order_is_gold_first(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        tasks = compile_plan(bundle, "deep", ["linkedin"])
        self.assertEqual(len(tasks), 148)
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
        self.assertEqual(len(process), 2)
        self.assertEqual(process[0].query_text, "healthcare business process analyst")
        self.assertEqual({task.query_variant for task in process}, {"primary", "variant_1"})
        eligibility = [task for task in tasks if task.canonical_title == "Eligibility Specialist"]
        self.assertEqual({task.query_text for task in eligibility}, {
            "healthcare eligibility specialist", "enrollment and eligibility representative",
        })
        self.assertEqual({task.query_variant for task in eligibility}, {"primary", "variant_1"})
        outreach = [task for task in tasks if task.canonical_title == "Patient Outreach Representative"]
        self.assertEqual({task.query_text for task in outreach}, {
            "patient outreach representative", "bilingual patient outreach",
        })
        self.assertEqual(len({task.query_text for task in tasks}), len(tasks))
        self.assertEqual(plan_counts(compile_staged_plan(bundle, ["linkedin"]))["total"], 296)

    def test_cadence_reduces_repeat_starts_without_reducing_definitions(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        tasks = compile_plan(bundle, "deep", ["linkedin"])
        counts = {band: sum(task.search_band == band for task in tasks) for band in BANDS}
        economics_result = cadence_economics(counts)
        self.assertTrue(economics_result["coverage_preserved"])
        self.assertGreater(economics_result["theoretical_reduction_percent"], 0)
        self.assertGreater(economics_result["baseline_definitions_per_day"], economics_result["steady_state_definitions_per_day"])

    def test_every_explicit_band_entry_is_executable_or_explicitly_classification_only(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        normalized = lambda value: " ".join(str(value).replace("—", " ").replace("–", " ").split()).casefold()
        configured = {
            normalized(title)
            for band in BANDS
            for title in bundle.strategy["strategy"]["search_bands"].get(band.lower() + "_titles", [])
        }
        lane_titles = {normalized(title) for lane in bundle.strategy["lanes"] for title in lane.get("titles", [])}
        compiled = {normalized(task.canonical_title) for task in compile_plan(bundle, "deep", ["linkedin"])}
        classification_only = {normalized(title) for title in bundle.strategy["strategy"]["search_bands"].get("classification_only_titles", [])}
        self.assertEqual(configured - lane_titles - compiled, classification_only)
        self.assertIn("healthcare eligibility specialist", classification_only)

    def test_recent_and_deep_cadence_are_separate_and_staged_economics_is_actual(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        recent = next(task for task in compile_plan(bundle, "fast", ["linkedin"]) if task.search_band == "GOLD")
        deep = next(task for task in compile_plan(bundle, "deep", ["linkedin"]) if task.search_band == "GOLD")
        self.assertEqual((recent.window_class, recent.cadence_hours), ("RECENT", 6))
        self.assertEqual((deep.window_class, deep.cadence_hours), ("DEEP", 168))
        result = staged_cadence_economics(compile_staged_plan(bundle, ["linkedin"]))
        self.assertEqual(result["definition_counts"]["RECENT"]["GOLD"], result["definition_counts"]["DEEP"]["GOLD"])
        self.assertEqual(result["old_recent_starts_per_day"], 592.0)
        self.assertGreater(result["actual_reduction_percent"], 0)

    def test_staged_economics_uses_compiled_task_cadence_not_defaults(self) -> None:
        original = load_bundle(PROJECT_ROOT)
        strategy = copy.deepcopy(original.strategy)
        strategy["strategy"]["search_band_cadence"]["gold_hours"] = 18
        bundle = type(original)(original.root, strategy, original.candidate, original.runtime)
        baseline = staged_cadence_economics(compile_staged_plan(original, ["linkedin"]))
        changed = staged_cadence_economics(compile_staged_plan(bundle, ["linkedin"]))
        self.assertLess(
            changed["new_recent_starts_per_day_by_band"]["GOLD"],
            baseline["new_recent_starts_per_day_by_band"]["GOLD"],
        )

    def test_scheduler_orders_recent_band_before_deep_tail_phase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            conn = Database(bundle).connect()
            try:
                now = "2026-09-09T12:00:00+00:00"
                run_id = conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)", ("test", "staged", "linkedin", "running", now)).lastrowid
                def add(phase, band, task_id):
                    conn.execute("""INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at,phase,search_band,window_class,task_key,execution_rank,priority)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, "linkedin", task_id, 7, "https://example.test", "queued", now, phase, band, "RECENT", task_id, 10, 0))
                add("A_FASTEST_DOOR_RECENT", "DEEP_TAIL", "tail")
                add("B_REMAINING_CORE_RECENT", "HEDGE", "hedge")
                conn.commit()
                rows = conn.execute("SELECT * FROM browser_search_tasks ORDER BY task_id").fetchall()
                self.assertLess(task_selection_key(conn, next(row for row in rows if row["task_key"] == "hedge")), task_selection_key(conn, next(row for row in rows if row["task_key"] == "tail")))
            finally:
                conn.close()

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

    def test_yield_windows_and_active_time_do_not_mix_or_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            store = PrecisionStore(path)
            try:
                now = "2026-09-09T12:00:00+00:00"
                for window_class, days, active in (("RECENT", 7, 100), ("DEEP", 30, 200)):
                    run_id = store.conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)", ("test", "precision", "linkedin", "running", now)).lastrowid
                    task_id = store.conn.execute("""INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at,completed_at,search_band,window_class,cadence_hours,task_key)
                        VALUES(?,?,?,?,?,'exhausted',?,?,?,?,?,?)""", (run_id, "linkedin", "patient enrollment specialist", days, "https://example.test", now, now, "GOLD", window_class, 168, f"yield-{window_class}")).lastrowid
                    store.conn.execute("INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)", (run_id, task_id, now, "performance", "operation", '{"duration_ms":999}'))
                    store.conn.execute("INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)", (run_id, task_id, now, "task_active_time", "active", '{"task_active_browser_ms":%d}' % active))
                    job = scored("Patient Enrollment Specialist", "Fully remote healthcare enrollment. Required Qualifications and HIPAA documentation. " * 20)
                    job.source_job_id = f"yield-{window_class}"; job.canonical_url = f"https://example.test/{window_class}"; job.apply_url = job.canonical_url
                    store.upsert(job, commit=False); job_id = store.resolve_job_id(job)
                    store.conn.execute("UPDATE jobs SET description_state='COMPLETE' WHERE job_id=?", (job_id,))
                    store.conn.execute("INSERT INTO search_task_results(task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,browser_run_id,detail_status,canonical_job_id) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, "linkedin", f"yield-{window_class}", f"https://example.test/{window_class}", now, now, run_id, "COMPLETE", job_id))
                    store.conn.commit()
                    task = store.conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()
                    self.assertTrue(record_task_yield(store.conn, task_id, now))
                stats = store.conn.execute("SELECT window_class,window_days,task_active_browser_ms,total_browser_ms FROM query_yield_stats ORDER BY window_class").fetchall()
                self.assertEqual([(row[0], row[1], row[2], row[3]) for row in stats], [("DEEP", 30, 200, 200), ("RECENT", 7, 100, 100)])
            finally:
                store.close()

    def test_yield_recommendations_use_only_complete_descriptions_and_separate_hard_reject(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            store = PrecisionStore(path)
            try:
                now = "2026-09-09T12:00:00+00:00"
                run_id = store.conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)", ("test", "precision", "linkedin", "running", now)).lastrowid
                task_id = store.conn.execute("""INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at,completed_at,search_band,window_class,cadence_hours,task_key)
                    VALUES(?,?,?,?,?,'exhausted',?,?,?,?,?,?)""", (run_id, "linkedin", "patient enrollment specialist", 7, "https://example.test", now, now, "GOLD", "RECENT", 6, "classification-task")).lastrowid
                jobs = [("complete-apply", "APPLY_NOW", "COMPLETE"), ("missing-apply", "APPLY_NOW", "MISSING"), ("complete-out", "OUT_OF_SCOPE", "COMPLETE"), ("complete-hard", "SKIP_HARD_GATE", "COMPLETE")]
                for job_id, recommendation, description_state in jobs:
                    store.conn.execute("INSERT INTO jobs(job_id,title,recommendation,remote_gate,description_state,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)", (job_id, "Patient Enrollment Specialist", recommendation, "pass", description_state, now, now))
                    store.conn.execute("INSERT INTO search_task_results(task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,browser_run_id,detail_status,canonical_job_id) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, "linkedin", job_id, f"https://example.test/{job_id}", now, now, run_id, "COMPLETE", job_id))
                store.conn.execute("INSERT INTO search_task_results(task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,browser_run_id,detail_status,canonical_job_id) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, "indeed", "complete-apply-mirror", "https://example.test/complete-apply-mirror", now, now, run_id, "COMPLETE", "complete-apply"))
                store.conn.commit()
                task = store.conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()
                self.assertTrue(record_task_yield(store.conn, task_id, now))
                row = store.conn.execute("SELECT completed_descriptions,apply_now,hard_reject,out_of_scope,descriptions_missing FROM query_yield_stats").fetchone()
                self.assertEqual(tuple(row), (3, 1, 1, 1, 1))
            finally:
                store.close()

    def test_query_yield_credits_only_marginal_new_actionable_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.sqlite3"
            bundle = bundle_with_database(path)
            Database(bundle).migrate()
            store = PrecisionStore(path)
            try:
                now = "2026-09-09T12:00:00+00:00"
                for query, ledger_status in (("query A", "new"), ("query B", "unchanged")):
                    run_id = store.conn.execute(
                        "INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)",
                        ("test", "precision", "linkedin", "running", now),
                    ).lastrowid
                    task_id = store.conn.execute(
                        """INSERT INTO browser_search_tasks(
                           browser_run_id,platform,query_text,window_days,search_url,status,created_at,
                           completed_at,search_band,window_class,cadence_hours,task_key,task_active_browser_ms
                        ) VALUES(?,?,?,?,?,'exhausted',?,?,?,?,?,?,?)""",
                        (run_id, "linkedin", query, 7, "https://example.test", now, now, "GOLD", "RECENT", 6, query, 60000),
                    ).lastrowid
                    store.conn.execute(
                        "INSERT OR REPLACE INTO jobs(job_id,title,recommendation,remote_gate,description_state,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                        ("canonical-X", "Patient Enrollment Specialist", "APPLY_NOW", "pass", "COMPLETE", now, now),
                    )
                    store.conn.execute(
                        """INSERT INTO search_task_results(
                           task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,
                           browser_run_id,detail_status,canonical_job_id
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (task_id, "linkedin", f"source-{query}", "https://example.test/canonical-X", now, now, run_id, "COMPLETE", "canonical-X"),
                    )
                    store.conn.execute(
                        "INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)",
                        (run_id, task_id, now, "job_recorded", "recorded", json.dumps({"job_id": "canonical-X", "ledger_status": ledger_status})),
                    )
                    store.conn.commit()
                    self.assertTrue(record_task_yield(store.conn, task_id, now))
                rows = store.conn.execute(
                    "SELECT normalized_query,canonical_jobs_observed,new_canonical_jobs,apply_ready_observed,new_apply_ready FROM query_yield_stats ORDER BY normalized_query"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [
                    ("query a", 1, 1, 1, 1),
                    ("query b", 1, 0, 1, 0),
                ])
            finally:
                store.close()

    def test_search_precision_fixture_bands_do_not_promote_growth_or_hard_noise(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        gold = [
            "Bilingual Enrollment Coordinator", "Patient Access Specialist",
            "Eligibility Specialist", "Insurance Verification Specialist", "RPM Coordinator",
            "Behavioral Health Intake Coordinator",
        ]
        for title in gold:
            self.assertEqual(search_band(title, "HEALTHCARE_OPS_ACCESS", bundle.strategy), "GOLD", title)
        for title in ["Healthcare Data Quality Specialist", "Healthcare Quality Analyst", "Healthcare Implementation Coordinator", "Healthcare Data Analyst"]:
            self.assertEqual(search_band(title, "HEALTHCARE_QUALITY_DATA", bundle.strategy), "GROWTH", title)
        for title in ["Higher Education Records Specialist", "Bilingual Content Reviewer"]:
            self.assertEqual(search_band(title, "HIGHER_ED_EDTECH" if "Education" in title else "CONTENT_AI_QUALITY", bundle.strategy), "HEDGE", title)
        for title in ["Credentialing Specialist", "Care Coordinator", "Provider Enrollment Specialist"]:
            self.assertNotEqual(search_band(title, "HEALTHCARE_OPS_ACCESS", bundle.strategy), "GOLD", title)
        for title in ["Member Services Specialist", "Member Support Specialist"]:
            self.assertEqual(search_band(title, "HEALTHCARE_OPS_ACCESS", bundle.strategy), "SILVER", title)
        self.assertNotEqual(search_band("Clinical Documentation Specialist", "HEALTHCARE_INFO_QA", bundle.strategy), "GOLD")
        score, reason = detail_priority(title="Senior Software Engineer", query_text="patient enrollment specialist", search_band="GOLD")
        self.assertIn("occupation_review", reason)
        self.assertLess(score, 500)

    def test_remote_training_logistics_never_reach_apply_now(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        job = scored("Patient Outreach Representative", "Fully remote after training. Mandatory in-person training in Houston, Texas for two weeks. Required Qualifications: healthcare outreach and HIPAA documentation.")
        self.assertEqual(job.remote_gate, "review")
        self.assertEqual(job.recommendation, "REVIEW_REMOTE")

    def test_candidate_readiness_warnings_are_visible_without_inventing_constraints(self) -> None:
        warnings = readiness_warnings(load_bundle(PROJECT_ROOT))
        self.assertTrue(any("work authorization" in warning for warning in warnings))
        self.assertTrue(any("travel tolerance" in warning for warning in warnings))
        self.assertTrue(any("salary floor" in warning for warning in warnings))

    def test_confidence_adjusted_roi_requires_sample_and_uses_active_time(self) -> None:
        row = {"completed_descriptions": 30, "apply_now": 15, "apply_volume": 5, "task_active_browser_ms": 600000, "observation_dates": '["2026-09-01", "2026-09-09"]'}
        estimate = query_yield_estimate(row)
        self.assertTrue(estimate["sample_eligible"])
        self.assertGreater(estimate["conservative_actionable_per_minute"], 0)
        self.assertLess(estimate["conservative_actionable_per_minute"], estimate["actionable_jobs_per_minute"])

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
