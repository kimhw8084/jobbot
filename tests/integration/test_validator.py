from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.browser_tasks import enqueue_validation, enqueue_validation_sample
from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.db import Database
from jobbot.validator import (
    VALIDATION_WINDOW_COMPLETE,
    _assert_isolated,
    _classify_supplemental,
    _record_validation_cutoff,
    _scope_diagnostics,
    _stage_pass,
    _write_report,
)


class ValidatorIntegrationTests(unittest.TestCase):
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


if __name__ == "__main__": unittest.main()
