from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobbot.browser_tasks import enqueue_validation, enqueue_validation_sample
from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.db import Database
from jobbot.legacy_engine import fetch_remotelanders_exhaustive, source_failure_class
from jobbot.search_plan import compile_plan
from jobbot.validator import (
    VALIDATION_WINDOW_COMPLETE,
    _assert_isolated,
    _classify_supplemental,
    _record_validation_cutoff,
    _validation_metrics,
    _scope_diagnostics,
    _phase_coverage_pass,
    _band_coverage_pass,
    _stage_pass,
    _write_report,
    _performance_summary,
    _dashboard_probe,
    _bounded_timeout,
    _isolated_bundle,
    _prepare_validation_bundle,
    _run_semi_phase,
    VALIDATION_STOP_GRACE_SECONDS,
)


class ValidatorIntegrationTests(unittest.TestCase):
    def test_micro_task_is_the_compiled_gold_recent_definition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation(root, ["linkedin"], bundle=bundle)
            compiled = next(task for task in compile_plan(bundle, "fast", ["linkedin"])
                            if task.canonical_title == "Patient Enrollment Specialist")
            conn = Database(bundle).connect()
            try:
                row = conn.execute("SELECT * FROM browser_search_tasks WHERE browser_run_id=?", (run_id,)).fetchone()
                for field in ("task_key", "canonical_title", "query_text", "query_variant", "search_band",
                              "window_class", "cadence_hours", "career_lane", "resume_variant",
                              "execution_rank", "search_url"):
                    expected = compiled.search_url if field == "search_url" else (
                        compiled.lane if field == "career_lane" else getattr(compiled, field)
                    )
                    self.assertEqual(row[field], expected, field)
                self.assertEqual(row["window_days"], 7)
                self.assertIsNone(row["max_results"])
            finally:
                conn.close()

    def test_deadline_timeout_leaves_cleanup_reserve_and_refuses_expired_start(self) -> None:
        with patch("jobbot.validator.time.monotonic", return_value=100.0):
            self.assertEqual(_bounded_timeout(300, 400.0), 288)
        with patch("jobbot.validator.time.monotonic", return_value=399.0):
            with self.assertRaisesRegex(RuntimeError, "refusing to start"):
                _bounded_timeout(10, 400.0)

    def test_each_isolated_long_stage_is_migrated_before_dashboard_use(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            database_path = root / "soak.sqlite3"
            bundle = _isolated_bundle(database_path, root / "out", 18765)
            self.assertFalse(database_path.exists())
            _prepare_validation_bundle(bundle)
            self.assertTrue(database_path.is_file())
            conn = sqlite3.connect(database_path)
            try:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone())
            finally:
                conn.close()

    def test_validator_refuses_production_database(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "refusing production database"):
            _assert_isolated(PROJECT_ROOT / "data" / "jobs.sqlite3")

    def test_report_is_machine_readable_and_defaults_to_no_go(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            _write_report({"branch": "test", "head": "abc", "PROD_READY": False}, target)
            payload = json.loads((target / "latest.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["PROD_READY"])
            self.assertIn("PROD_READY=false", (target / "latest.md").read_text(encoding="utf-8"))

    def test_micro_report_has_separate_latest_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            _write_report({"validation_stage": "micro", "PROD_READY": False}, target, prefix="micro")
            self.assertTrue((target / "micro-latest.json").is_file())
            self.assertTrue((target / "micro-latest.md").is_file())
            self.assertFalse((target / "latest.json").exists())

    def test_stage_pass_requires_reconciliation_and_terminal_tasks(self) -> None:
        base = {
            "terminal_classification": "COMPLETED_FULL",
            "metrics": {
                "integrity": "ok", "reconciliation": {"ok": True},
                "extracted": 2, "persistence_attempted": 2, "persisted": 2, "persistence_failed": 0,
                "task_states": {"queued": 0, "running": 0, "exhausted": 2, "incomplete": 0,
                                 "challenged": 0, "auth_required": 0, "deferred_by_platform": 0,
                                 "failed": 0, "paused": 0, "stopped": 0},
            },
            "scope": {"linkedin": {"contamination": 0, "scope_missing_events": 0}},
            "extension_build_pass": True,
        }
        self.assertTrue(_stage_pass(base))
        base["metrics"]["task_states"]["incomplete"] = 1
        self.assertFalse(_stage_pass(base))

    def test_outside_links_are_allowed_until_one_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation(root, ["linkedin"], bundle=bundle)
            conn = Database(bundle).connect()
            try:
                task_id = conn.execute("SELECT task_id FROM browser_search_tasks WHERE browser_run_id=?", (run_id,)).fetchone()[0]
                diagnostics = {
                    "candidate_links_total": 6, "candidate_links_in_scope": 3,
                    "candidate_links_outside_scope": 3,
                    "outside_scope_source_ids": ["outside-1", "outside-2", "outside-3"],
                    "outside_scope_urls": [f"https://www.linkedin.com/jobs/view/{n}/" for n in (901, 902, 903)],
                }
                conn.execute(
                    "INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)",
                    (run_id, task_id, "2026-09-08T12:00:00Z", "scope_diagnostics", "scope", json.dumps({"payload": diagnostics})),
                )
                conn.execute(
                    "INSERT INTO search_task_results(browser_run_id,task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,detail_status) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, task_id, "linkedin", "inside-1", "https://www.linkedin.com/jobs/view/701/", "now", "now", "PENDING"),
                )
                conn.commit()
                clean = _scope_diagnostics(bundle, run_id)["linkedin"]
                self.assertEqual(clean["candidate_links_outside_scope"], 3)
                self.assertEqual(clean["persisted_outside_scope"], 0)
                self.assertEqual(clean["contamination_persisted"], 0)
                conn.execute(
                    "INSERT INTO search_task_results(browser_run_id,task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,detail_status) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, task_id, "linkedin", "outside-2", "https://www.linkedin.com/jobs/view/902/", "now", "now", "PENDING"),
                )
                conn.commit()
                contaminated = _scope_diagnostics(bundle, run_id)["linkedin"]
                self.assertEqual(contaminated["persisted_outside_scope"], 1)
                self.assertEqual(contaminated["contamination_persisted"], 1)
            finally:
                conn.close()

    def test_cross_query_in_scope_identity_cancels_outside_observation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation(root, ["linkedin"], bundle=bundle)
            conn = Database(bundle).connect()
            try:
                first_task = conn.execute(
                    "SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id LIMIT 1", (run_id,)
                ).fetchone()[0]
                second_task = conn.execute(
                    "INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,created_at) "
                    "VALUES(?,?,?,?,?,?) RETURNING task_id",
                    (run_id, "linkedin", "query B", 7, "https://www.linkedin.com/jobs/search/?keywords=query+b", "now"),
                ).fetchone()[0]
                for task_id, payload in (
                    (first_task, {"in_scope_source_ids": ["X"], "in_scope_urls": ["https://jobs.example/x"],
                                  "outside_scope_source_ids": [], "outside_scope_urls": []}),
                    (second_task, {"in_scope_source_ids": [], "in_scope_urls": [],
                                   "outside_scope_source_ids": ["X", "Y"],
                                   "outside_scope_urls": ["https://jobs.example/x", "https://jobs.example/y"]}),
                ):
                    conn.execute(
                        "INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)",
                        (run_id, task_id, "now", "scope_diagnostics", "scope", json.dumps({"payload": {
                            "candidate_links_total": 1, "candidate_links_in_scope": len(payload["in_scope_source_ids"]),
                            "candidate_links_outside_scope": len(payload["outside_scope_source_ids"]), **payload,
                        }})),
                    )
                conn.execute(
                    "INSERT INTO search_task_results(browser_run_id,task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,detail_status) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, first_task, "linkedin", "X", "https://jobs.example/x", "now", "now", "PENDING"),
                )
                conn.execute(
                    "INSERT INTO search_task_results(browser_run_id,task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,detail_status) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, second_task, "linkedin", "Y", "https://jobs.example/y", "now", "now", "PENDING"),
                )
                conn.commit()
                values = _scope_diagnostics(bundle, run_id)["linkedin"]
                self.assertNotIn("X", values["outside_only_ids"])
                self.assertIn("Y", values["outside_only_ids"])
                self.assertEqual(values["contamination_persisted"], 1)
            finally:
                conn.close()

    def test_bounded_natural_terminal_is_valid_but_internal_incomplete_is_not(self) -> None:
        def stage(classification: str) -> dict:
            return {
                "validation_classification": classification,
                "validation_cutoff": False,
                "validation_metrics": {
                    "integrity": "ok", "reconciliation": {"ok": True}, "unexplained": 0,
                    "extracted": 2, "persistence_attempted": 2, "persisted": 2, "persistence_failed": 0,
                    "attempted_tasks": 1, "progress_tasks": 1, "untouched_exhausted": 0,
                    "attempted_incomplete": 0, "attempted_failed": 0, "attempted_running": 0,
                    "task_states": {"queued": 0, "running": 0, "exhausted": 1, "incomplete": 0,
                                     "challenged": 0, "auth_required": 0, "deferred_by_platform": 0,
                                     "failed": 0, "paused": 0, "stopped": 0},
                },
                "scope": {"linkedin": {"contamination_persisted": 0, "scope_missing_events": 0,
                                         "attempted_pages": 1, "scope_found_events": 1}},
                "extension_build_pass": True,
            }
        self.assertTrue(_stage_pass(stage("COMPLETED_FULL"), bounded=True))
        self.assertTrue(_stage_pass(stage("COMPLETED_PARTIAL_EXTERNAL"), bounded=True))
        failed = stage("COMPLETED_FULL")
        failed["validation_metrics"]["attempted_incomplete"] = 1
        self.assertFalse(_stage_pass(failed, bounded=True))
        contaminated = stage("COMPLETED_FULL")
        contaminated["scope"]["linkedin"]["contamination_persisted"] = 1
        self.assertFalse(_stage_pass(contaminated, bounded=True))

    def test_phase_coverage_rejects_queued_only_b_and_c_and_accepts_progress(self) -> None:
        phases = {
            phase: {"linkedin": {"sampled_queued": 2, "started_tasks": 0, "progress_tasks": 0,
                                  "all_external_blocked": False}}
            for phase in ("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT", "C_DEEP_BACKFILL")
        }
        phases["A_FASTEST_DOOR_RECENT"]["linkedin"]["started_tasks"] = 2
        phases["A_FASTEST_DOOR_RECENT"]["linkedin"]["progress_tasks"] = 2
        self.assertFalse(_phase_coverage_pass(phases))
        for phase in ("B_REMAINING_CORE_RECENT", "C_DEEP_BACKFILL"):
            phases[phase]["linkedin"]["started_tasks"] = 1
            phases[phase]["linkedin"]["progress_tasks"] = 1
        self.assertTrue(_phase_coverage_pass(phases))

    def test_band_coverage_requires_progress_or_all_sampled_work_external(self) -> None:
        execution = {
            "A_FASTEST_DOOR_RECENT": {
                "linkedin": {
                    "bands": {
                        "GOLD": {"sampled_queued": 1, "progress_tasks": 1},
                        "SILVER": {"sampled_queued": 1, "progress_tasks": 1},
                        "GROWTH": {"sampled_queued": 1, "progress_tasks": 1},
                        "HEDGE": {"sampled_queued": 1, "progress_tasks": 1},
                        "DEEP_TAIL": {"sampled_queued": 1, "progress_tasks": 0, "external_tasks": 1},
                    }
                }
            }
        }
        self.assertTrue(_band_coverage_pass(execution))
        execution["A_FASTEST_DOOR_RECENT"]["linkedin"]["bands"]["DEEP_TAIL"]["external_tasks"] = 0
        self.assertFalse(_band_coverage_pass(execution))

    def test_soak_requires_gold_progress_but_not_all_bands(self) -> None:
        execution = {
            "A_FASTEST_DOOR_RECENT": {
                "linkedin": {
                    "bands": {
                        "GOLD": {"sampled_queued": 1, "progress_tasks": 1},
                        "SILVER": {"sampled_queued": 1, "progress_tasks": 0},
                        "GROWTH": {"sampled_queued": 1, "progress_tasks": 0},
                        "HEDGE": {"sampled_queued": 1, "progress_tasks": 0},
                        "DEEP_TAIL": {"sampled_queued": 1, "progress_tasks": 0},
                    }
                }
            }
        }
        self.assertTrue(_band_coverage_pass(execution, required_bands=("GOLD",)))
        self.assertFalse(_band_coverage_pass(execution))

    def test_bounded_phase_stop_has_grace_and_only_a_resumes(self) -> None:
        stopped = {"run_id": 7, "outcome": {"status": "stopped"}}
        with patch("jobbot.validator._live_run", return_value=stopped) as live, \
             patch("jobbot.validator._resume_live", return_value=stopped) as resume, \
             patch("jobbot.validator._phase_execution_metrics", return_value={}), \
             patch("jobbot.validator._stage_pass", return_value=True):
            _run_semi_phase(object(), phase="A_FASTEST_DOOR_RECENT", platforms=["linkedin"],
                            phase_seconds=180, active_runs=[])
            initial = live.call_args.kwargs
            self.assertEqual(initial["stop_after_seconds"], 30)
            self.assertEqual(initial["timeout_seconds"], 30 + VALIDATION_STOP_GRACE_SECONDS)
            self.assertEqual(resume.call_args.args[2], 150 + VALIDATION_STOP_GRACE_SECONDS)
            self.assertEqual(resume.call_args.args[3], 150)

        with patch("jobbot.validator._live_run", return_value=stopped) as live, \
             patch("jobbot.validator._resume_live") as resume, \
             patch("jobbot.validator._phase_execution_metrics", return_value={}), \
             patch("jobbot.validator._stage_pass", return_value=True):
            result = _run_semi_phase(object(), phase="B_REMAINING_CORE_RECENT", platforms=["linkedin"],
                                     phase_seconds=180, active_runs=[])
            self.assertEqual(live.call_count, 1)
            self.assertFalse(resume.called)
            self.assertIsNone(result["resume"])
            self.assertEqual(live.call_args.kwargs["stop_after_seconds"], 180)
            self.assertEqual(live.call_args.kwargs["timeout_seconds"], 180 + VALIDATION_STOP_GRACE_SECONDS)

    def test_bounded_cutoff_allows_untouched_queued_work_but_not_internal_incomplete(self) -> None:
        stage = {
            "validation_classification": VALIDATION_WINDOW_COMPLETE,
            "validation_cutoff": True,
            "validation_metrics": {
                "integrity": "ok", "reconciliation": {"ok": True}, "unexplained": 0,
                "extracted": 2, "persistence_attempted": 2, "persisted": 2, "persistence_failed": 0,
                "attempted_tasks": 1, "progress_tasks": 1, "untouched_queued": 10, "untouched_exhausted": 0,
                "attempted_incomplete": 0, "attempted_failed": 0, "attempted_running": 0,
                "task_states": {"queued": 0, "running": 0, "exhausted": 0, "incomplete": 0,
                                 "challenged": 0, "auth_required": 0, "deferred_by_platform": 0,
                                 "failed": 0, "paused": 0, "stopped": 1},
            },
            "scope": {"linkedin": {"contamination_persisted": 0, "scope_missing_events": 0,
                                     "attempted_pages": 1, "scope_found_events": 1}},
            "extension_build_pass": True,
        }
        self.assertTrue(_stage_pass(stage, bounded=True))
        stage["validation_metrics"]["untouched_exhausted"] = 1
        self.assertFalse(_stage_pass(stage, bounded=True))
        stage["validation_metrics"]["untouched_exhausted"] = 0
        stage["validation_metrics"]["attempted_incomplete"] = 1
        self.assertFalse(_stage_pass(stage, bounded=True))

    def test_validation_metrics_accept_string_canonical_job_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation(root, ["linkedin"], bundle=bundle)
            conn = Database(bundle).connect()
            try:
                task_id = conn.execute(
                    "SELECT task_id FROM browser_search_tasks WHERE browser_run_id=?", (run_id,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE browser_search_tasks SET status='stopped',started_at='now',pages_visited=1,"
                    "cards_extracted=1,cards_persistence_attempted=1,cards_persistence_succeeded=1 "
                    "WHERE task_id=?", (task_id,)
                )
                conn.execute(
                    "INSERT INTO search_task_results(browser_run_id,task_id,source_site,source_job_id,source_url,"
                    "canonical_job_id,first_seen_at,last_seen_at,detail_status) VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, task_id, "linkedin", "4462567941",
                     "https://www.linkedin.com/jobs/view/4462567941/", "J943D57AD3EB03C",
                     "now", "now", "COMPLETE"),
                )
                conn.commit()
            finally:
                conn.close()
            metrics = _validation_metrics(
                bundle,
                run_id,
                {"platforms": {}, "global": {}, "reconciliation": {"ok": True}, "integrity": "ok"},
            )
            self.assertEqual(metrics["canonical_jobs_attempted"], 1)

    def test_validation_sample_contains_representative_a_b_c_phases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation_sample(root, ["linkedin", "indeed"], per_phase_per_platform=2, bundle=bundle)
            conn = sqlite3.connect(bundle.database_path)
            try:
                rows = conn.execute(
                    "SELECT phase,platform,COUNT(*),SUM(CASE WHEN max_results IS NULL THEN 1 ELSE 0 END) "
                    "FROM browser_search_tasks WHERE browser_run_id=? GROUP BY phase,platform", (run_id,),
                ).fetchall()
                self.assertEqual({row[0] for row in rows}, {"A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT", "C_DEEP_BACKFILL"})
                self.assertTrue(all(row[2] == 2 and row[3] == 2 for row in rows))
            finally:
                conn.close()

    def test_validation_sample_can_probe_one_real_band_without_legacy_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation_sample(
                root, ["linkedin"], phases=("B_REMAINING_CORE_RECENT",),
                sample_bands=("HEDGE",), per_phase_per_platform=1, bundle=bundle,
            )
            conn = Database(bundle).connect()
            try:
                row = conn.execute(
                    "SELECT phase,search_band,window_class,query_variant,task_key FROM browser_search_tasks WHERE browser_run_id=?",
                    (run_id,),
                ).fetchone()
                self.assertEqual(tuple(row), ("B_REMAINING_CORE_RECENT", "HEDGE", "RECENT", "primary", row[4]))
                self.assertTrue(row[4])
            finally:
                conn.close()

    def test_validation_cutoff_is_durable_and_never_exhausts_untouched_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation_sample(root, ["linkedin"], per_phase_per_platform=1, bundle=bundle)
            _record_validation_cutoff(bundle, run_id, "test cutoff")
            conn = Database(bundle).connect()
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='exhausted'",
                    (run_id,),
                ).fetchone()[0], 0)
                event = conn.execute(
                    "SELECT payload_json FROM browser_events WHERE browser_run_id=? AND event_type='validation_cutoff'",
                    (run_id,),
                ).fetchone()
                self.assertIsNotNone(event)
                self.assertEqual(json.loads(event[0])["classification"], VALIDATION_WINDOW_COMPLETE)
            finally:
                conn.close()

    def test_supplemental_external_failure_isolated_but_internal_exception_fails(self) -> None:
        external = _classify_supplemental(
            {"source_a": {"ok": False, "error": "timeout"}, "source_b": {"ok": True, "count": 2}},
            code=0,
        )
        self.assertTrue(external["isolation_pass"])
        self.assertEqual(external["sources"]["source_a"]["state"], "external_failed")
        self.assertEqual(external["sources"]["source_b"]["state"], "completed")
        internal = _classify_supplemental({}, code=1, uncaught_error="ProgrammingError: broken orchestration")
        self.assertFalse(internal["isolation_pass"])
        self.assertTrue(internal["internal_failed"])

    def test_performance_summary_reports_bounded_p50_p95_without_page_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation(root, ["linkedin"], bundle=bundle)
            conn = Database(bundle).connect()
            try:
                for value in (10, 20, 30, 40, 50):
                    conn.execute(
                        "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                        (run_id, "now", "performance", f"search {value}ms", json.dumps({
                            "operation": "search_collect", "platform": "linkedin", "duration_ms": value,
                        })),
                    )
                conn.execute(
                    "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                    (run_id, "now", "performance", "cards", json.dumps({
                        "operation": "card_persist", "platform": "linkedin", "duration_ms": 600, "cards": 3,
                    })),
                )
                conn.execute(
                    "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                    (run_id, "now", "performance", "detail", json.dumps({
                        "operation": "detail_record", "platform": "linkedin", "duration_ms": 1200,
                    })),
                )
                conn.commit()
                summary = _performance_summary(conn, run_id)
            finally:
                conn.close()
            self.assertEqual(summary["samples"], 7)
            self.assertEqual(summary["operations"]["linkedin:search_collect"]["p50_ms"], 30)
            self.assertEqual(summary["operations"]["linkedin:search_collect"]["p95_ms"], 48)
            self.assertEqual(summary["throughput"]["linkedin"]["cards_per_minute"], 300.0)
            self.assertEqual(summary["throughput"]["linkedin"]["canonical_details_per_minute"], 50.0)

    def test_workspace_probe_uses_current_stage_recreation_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _isolated_bundle(root / "validation.sqlite3", root / "out", 18765)
            Database(bundle).migrate()
            identity = {
                "resolved_database_path": str(bundle.database_path.resolve()),
                "workspace_root": str(bundle.root.resolve()),
                "jobbot_version": "3.2.3",
            }
            active = {"workspace": {
                "isolated": True, "workspace_window_id": 7,
                "workspace_creation_method": "windows.create",
                "ownership_violations": 0, "role_tab_window_ids": {}, "worker_tab_window_ids": {},
                "focus_requests_by_jobbot": 0, "workspace_recreation_count": 1304,
            }}
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                probe = _dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_baseline=1304)
                self.assertTrue(probe["workspace_isolation_ok"])
                self.assertTrue(probe["workspace_proof"]["workspace_recreation_unchanged"])
            active["workspace"]["focus_requests_by_jobbot"] = 1
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_baseline=1304)["workspace_isolation_ok"])
            active["workspace"]["focus_requests_by_jobbot"] = 0
            active["workspace"]["role_tab_window_ids"] = {"anchor": 8}
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_baseline=1304)["workspace_isolation_ok"])
            active["workspace"]["role_tab_window_ids"] = {}
            active["workspace"]["workspace_recreation_count"] = 1305
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                probe = _dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_baseline=1304)
                self.assertFalse(probe["workspace_isolation_ok"])
                self.assertFalse(probe["workspace_proof"]["workspace_recreation_unchanged"])

    def test_supplemental_failure_taxonomy_keeps_programming_bugs_internal(self) -> None:
        import socket
        import urllib.error

        self.assertEqual(source_failure_class(TimeoutError("network timeout"))[0], "EXTERNAL_NETWORK")
        self.assertEqual(source_failure_class(urllib.error.HTTPError("https://x", 429, "rate", {}, None))[0], "EXTERNAL_RATE_LIMIT")
        self.assertEqual(source_failure_class(ValueError("bad parser value"))[0], "INTERNAL_PARSE_ERROR")
        self.assertEqual(source_failure_class(KeyError("missing parser field"))[1], True)
        self.assertEqual(source_failure_class(sqlite3.OperationalError("locked"))[0], "INTERNAL_DB_ERROR")
        self.assertEqual(source_failure_class(RuntimeError("orchestration defect"))[0], "INTERNAL_LOGIC_ERROR")
        self.assertEqual(source_failure_class(socket.timeout("timed out"))[0], "EXTERNAL_NETWORK")
        self.assertEqual(source_failure_class(json.JSONDecodeError("bad json", "{", 1))[0], "EXTERNAL_SCHEMA_CHANGED")

    def test_remotelanders_safety_ceiling_is_capped_not_complete(self) -> None:
        class EndlessSource:
            def json(self, _url: str) -> dict[str, list[dict[str, str]]]:
                return {"jobs": [{"slug": "job-1", "title": "Patient Access Specialist", "company": "Example Health", "url": "https://example.com/jobs/1"}]}

        batch = fetch_remotelanders_exhaustive(
            EndlessSource(),
            {"url": "https://remotelanders.example/api/jobs", "page_size": 1, "safety_max_pages": 2},
        )
        self.assertFalse(batch.complete)
        self.assertEqual(batch.boundary, "CAPPED_EXTERNAL_BOUNDARY")


if __name__ == "__main__": unittest.main()
