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
    _coverage_cell_evidence,
    _active_stage_blocker,
    _external_blocker_provenance,
    _inherited_external_cell,
    _inherited_semi_phase,
    _semi_cell_phase_seconds,
    _semi_probe_schedule,
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
    def _state_bundle(self, td: str, platform: str, auth_status: str, retry_at: str | None = None):
        root = Path(td)
        (root / "config").mkdir()
        for item in (PROJECT_ROOT / "config").iterdir():
            (root / "config" / item.name).write_bytes(item.read_bytes())
        bundle = _isolated_bundle(root / "state.sqlite3", root / "out", 18766)
        _prepare_validation_bundle(bundle)
        conn = Database(bundle).connect()
        try:
            conn.execute(
                "UPDATE platform_state SET auth_status=?,auth_reason=?,challenged_at=?,"
                "manual_retry_requested_at=? WHERE platform=?",
                (auth_status, "fixture external state", "2026-09-11T02:00:00+00:00", retry_at, platform),
            )
            conn.commit()
        finally:
            conn.close()
        return bundle

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

    def test_bounded_external_only_cell_is_allowed_only_with_explicit_external_proof(self) -> None:
        stage = {
            "validation_classification": "COMPLETED_PARTIAL_EXTERNAL",
            "validation_cutoff": False,
            "validation_metrics": {
                "integrity": "ok", "reconciliation": {"ok": True}, "unexplained": 0,
                "extracted": 0, "persistence_attempted": 0, "persisted": 0, "persistence_failed": 0,
                "attempted_tasks": 1, "progress_tasks": 0, "untouched_exhausted": 0,
                "attempted_incomplete": 0, "attempted_failed": 0, "attempted_running": 0,
                "task_states": {"queued": 0, "running": 0, "exhausted": 0, "incomplete": 0,
                                 "challenged": 0, "auth_required": 1, "deferred_by_platform": 0,
                                 "failed": 0, "paused": 0, "stopped": 0},
            },
            "scope": {"indeed": {"contamination_persisted": 0, "scope_missing_events": 0,
                                   "attempted_pages": 0, "scope_found_events": 0}},
            "extension_build_pass": True,
        }
        self.assertFalse(_stage_pass(stage, bounded=True))
        self.assertTrue(_stage_pass(stage, bounded=True, allow_external_only=True))

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

    def test_semi_schedule_gives_each_required_platform_cell_a_call(self) -> None:
        schedule = _semi_probe_schedule()
        self.assertEqual(len(schedule), 15)
        self.assertEqual({platform for _, _, platform in schedule}, {"linkedin", "indeed", "glassdoor"})
        for phase, band in (
            ("A_FASTEST_DOOR_RECENT", "GOLD"),
            ("A_FASTEST_DOOR_RECENT", "SILVER"),
            ("B_REMAINING_CORE_RECENT", "GROWTH"),
            ("B_REMAINING_CORE_RECENT", "HEDGE"),
            ("C_DEEP_BACKFILL", "DEEP_TAIL"),
        ):
            self.assertEqual(
                [platform for current_phase, current_band, platform in schedule
                 if (current_phase, current_band) == (phase, band)],
                ["linkedin", "indeed", "glassdoor"],
            )

    def test_semi_cell_budget_stays_within_the_fixed_30_minute_stage(self) -> None:
        cell_count = len(_semi_probe_schedule())
        per_cell = _semi_cell_phase_seconds(30, cell_count)
        worst_case = cell_count * (per_cell + VALIDATION_STOP_GRACE_SECONDS) + VALIDATION_STOP_GRACE_SECONDS
        self.assertLessEqual(worst_case, 30 * 60 - 12)

    def test_platform_aware_coverage_cannot_be_masked_by_linkedin(self) -> None:
        cells = []
        for phase, band, platform in _semi_probe_schedule():
            passed = platform == "linkedin"
            cells.append({"phase": phase, "band": band, "platform": platform,
                          "coverage_pass": passed, "coverage_reason": "progress" if passed else "queued_only"})
        self.assertFalse(_phase_coverage_pass(cells))
        self.assertFalse(_band_coverage_pass(cells))

    def test_platform_aware_coverage_accepts_all_progress_or_external_third(self) -> None:
        cells = [{"phase": phase, "band": band, "platform": platform,
                  "coverage_pass": True, "coverage_reason": "progress"}
                 for phase, band, platform in _semi_probe_schedule()]
        self.assertTrue(_phase_coverage_pass(cells))
        self.assertTrue(_band_coverage_pass(cells))
        for index, cell in enumerate(cells):
            if cell["platform"] == "glassdoor":
                cell["coverage_reason"] = "external_blocked"
        self.assertTrue(_phase_coverage_pass(cells))
        self.assertTrue(_band_coverage_pass(cells))

    def test_external_cell_does_not_exempt_another_unattempted_cell(self) -> None:
        cells = [{"phase": phase, "band": band, "platform": platform,
                  "coverage_pass": platform != "glassdoor",
                  "coverage_reason": "progress" if platform == "linkedin" else (
                      "external_blocked" if platform == "indeed" else "queued_only")}
                 for phase, band, platform in _semi_probe_schedule()]
        self.assertFalse(_phase_coverage_pass(cells))
        self.assertFalse(_band_coverage_pass(cells))

    def test_coverage_cell_evidence_requires_sampling_and_real_progress_or_external(self) -> None:
        execution = {
            "B_REMAINING_CORE_RECENT": {
                "linkedin": {"bands": {"HEDGE": {
                    "sampled_queued": 1, "started_tasks": 1, "progress_tasks": 1,
                    "cards_persisted": 2, "details_complete": 1, "external_tasks": 0,
                    "external_classifications": {},
                }}}
            },
            "A_FASTEST_DOOR_RECENT": {
                "indeed": {"bands": {"HEDGE": {
                    "sampled_queued": 1, "started_tasks": 1, "progress_tasks": 0,
                    "cards_persisted": 0, "details_complete": 0, "external_tasks": 1,
                    "external_classifications": {"auth_required": 1},
                }}},
            },
        }
        progress = _coverage_cell_evidence(execution, phase="B_REMAINING_CORE_RECENT",
                                           band="HEDGE", platform="linkedin", run_id=7)
        self.assertTrue(progress["coverage_pass"])
        self.assertEqual(progress["coverage_reason"], "progress")
        external = _coverage_cell_evidence(execution, phase="A_FASTEST_DOOR_RECENT",
                                            band="HEDGE", platform="indeed", run_id=8)
        self.assertTrue(external["coverage_pass"])
        self.assertEqual(external["external_classification"], {"auth_required": 1})
        queued = _coverage_cell_evidence({}, phase="C_DEEP_BACKFILL",
                                         band="DEEP_TAIL", platform="glassdoor", run_id=9)
        self.assertFalse(queued["coverage_pass"])
        self.assertEqual(queued["coverage_reason"], "unsampled")
        externally_deferred_before_start = _coverage_cell_evidence(
            {"C_DEEP_BACKFILL": {"glassdoor": {"bands": {"DEEP_TAIL": {
                "sampled_queued": 1, "started_tasks": 0, "progress_tasks": 0,
                "cards_persisted": 0, "details_complete": 0, "external_tasks": 1,
                "external_classifications": {"deferred_by_platform": 1},
            }}}}},
            phase="C_DEEP_BACKFILL", band="DEEP_TAIL", platform="glassdoor", run_id=10,
        )
        self.assertFalse(externally_deferred_before_start["coverage_pass"])
        self.assertEqual(externally_deferred_before_start["coverage_reason"], "queued_only")

    def test_stage_external_blockers_inherit_across_all_later_cells(self) -> None:
        indeed_blocker = {
            "platform": "indeed", "classification": "challenged",
            "originating_cell": {"phase": "A_FASTEST_DOOR_RECENT", "band": "GOLD", "platform": "indeed"},
            "originating_run_id": 2, "observed_at": "2026-09-11T02:39:37+00:00",
        }
        glassdoor_blocker = {
            "platform": "glassdoor", "classification": "auth_required",
            "originating_cell": {"phase": "A_FASTEST_DOOR_RECENT", "band": "GOLD", "platform": "glassdoor"},
            "originating_run_id": 3, "observed_at": "2026-09-11T02:40:04+00:00",
        }
        cells = []
        for phase, band, platform in _semi_probe_schedule():
            blocker = {"indeed": indeed_blocker, "glassdoor": glassdoor_blocker}.get(platform)
            if blocker:
                cell = _inherited_external_cell(phase=phase, band=band, platform=platform, blocker=blocker)
                self.assertIsNone(cell["run_id"])
                self.assertEqual(cell["coverage_reason"], "inherited_external_blocked")
                self.assertEqual(cell["inherited_from_run_id"], blocker["originating_run_id"])
            else:
                cell = {"phase": phase, "band": band, "platform": platform,
                        "coverage_pass": True, "coverage_reason": "progress", "progress_count": 1}
            cells.append(cell)
        self.assertEqual(len(cells), 15)
        self.assertTrue(_phase_coverage_pass(cells))
        self.assertTrue(_band_coverage_pass(cells))
        self.assertTrue(all(cell["progress_count"] == 0 for cell in cells if cell["platform"] != "linkedin"))

    def test_active_stage_blocker_is_platform_and_stage_local(self) -> None:
        blocker = {
            "platform": "indeed", "classification": "challenged",
            "originating_cell": {"phase": "A_FASTEST_DOOR_RECENT", "band": "GOLD", "platform": "indeed"},
            "originating_run_id": 2, "observed_at": "2026-09-11T02:39:37+00:00",
        }
        with tempfile.TemporaryDirectory() as td:
            bundle = self._state_bundle(td, "indeed", "challenged")
            stage_blockers = {"indeed": blocker}
            self.assertIsNotNone(_active_stage_blocker(bundle, stage_blockers, "indeed"))
            self.assertIsNone(_active_stage_blocker(bundle, stage_blockers, "glassdoor"))
            self.assertIsNone(_active_stage_blocker(bundle, {}, "indeed"))

    def test_manual_retry_clears_inheritance_without_validator_retry_or_state_mutation(self) -> None:
        blocker = {
            "platform": "indeed", "classification": "challenged",
            "originating_cell": {"phase": "A_FASTEST_DOOR_RECENT", "band": "GOLD", "platform": "indeed"},
            "originating_run_id": 2, "observed_at": "2026-09-11T02:39:37+00:00",
        }
        with tempfile.TemporaryDirectory() as td:
            bundle = self._state_bundle(td, "indeed", "challenged")
            self.assertIsNotNone(_active_stage_blocker(bundle, {"indeed": blocker}, "indeed"))
            conn = Database(bundle).connect()
            try:
                before = conn.execute(
                    "SELECT auth_status,manual_retry_requested_at FROM platform_state WHERE platform='indeed'"
                ).fetchone()
                conn.execute(
                    "UPDATE platform_state SET auth_status='unchecked',manual_retry_requested_at=? WHERE platform='indeed'",
                    ("2026-09-11T02:45:00+00:00",),
                )
                conn.commit()
                after = conn.execute(
                    "SELECT auth_status,manual_retry_requested_at FROM platform_state WHERE platform='indeed'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(before), ("challenged", None))
            self.assertEqual(tuple(after), ("unchecked", "2026-09-11T02:45:00+00:00"))
            self.assertIsNone(_active_stage_blocker(bundle, {"indeed": blocker}, "indeed"))

    def test_inherited_phase_has_no_browser_run_and_keeps_blocker_provenance(self) -> None:
        blocker = {
            "platform": "glassdoor", "classification": "auth_required",
            "originating_cell": {"phase": "A_FASTEST_DOOR_RECENT", "band": "GOLD", "platform": "glassdoor"},
            "originating_run_id": 3, "reason": "auth_required", "observed_event_id": 18,
            "observed_at": "2026-09-11T02:40:04+00:00",
        }
        result = _inherited_semi_phase(
            phase="B_REMAINING_CORE_RECENT", band="HEDGE", platform="glassdoor", blocker=blocker,
        )
        cell = result["coverage_cell"]
        self.assertTrue(result["pass"])
        self.assertIsNone(result["final"]["run_id"])
        self.assertEqual(cell["coverage_reason"], "inherited_external_blocked")
        self.assertEqual(cell["external_classification"], {"auth_required": 1})
        self.assertEqual(cell["inherited_from_cell"], blocker["originating_cell"])
        self.assertEqual(cell["inherited_from_run_id"], 3)

    def test_direct_external_provenance_is_safe_and_run_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            for item in (PROJECT_ROOT / "config").iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_validation_sample(
                root, ["indeed"], phases=("A_FASTEST_DOOR_RECENT",),
                sample_bands=("GOLD",), per_phase_per_platform=1, bundle=bundle,
            )
            conn = Database(bundle).connect()
            try:
                task_id = conn.execute(
                    "SELECT task_id FROM browser_search_tasks WHERE browser_run_id=?", (run_id,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE browser_search_tasks SET status='challenged',started_at=?,challenge_reason=? WHERE task_id=?",
                    ("2026-09-11T02:39:37+00:00", "captcha", task_id),
                )
                conn.execute(
                    "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                    (run_id, "2026-09-11T02:39:37+00:00", "platform_paused", "indeed: captcha", "{}"),
                )
                conn.commit()
            finally:
                conn.close()
            provenance = _external_blocker_provenance(bundle, cell={
                "run_id": run_id, "phase": "A_FASTEST_DOOR_RECENT", "band": "GOLD",
                "platform": "indeed", "external_classification": {"challenged": 1},
            })
            self.assertEqual(provenance["originating_run_id"], run_id)
            self.assertEqual(provenance["classification"], "challenged")
            self.assertEqual(provenance["reason"], "captcha")
            self.assertIsInstance(provenance["observed_event_id"], int)

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
            _run_semi_phase(object(), phase="A_FASTEST_DOOR_RECENT", stage_id="semi-stage-aaaaaaaa",
                            platforms=["linkedin"], phase_seconds=180, active_runs=[])
            initial = live.call_args.kwargs
            self.assertEqual(initial["stop_after_seconds"], 30)
            self.assertEqual(initial["timeout_seconds"], 30 + VALIDATION_STOP_GRACE_SECONDS)
            self.assertEqual(resume.call_args.args[2], 150 + VALIDATION_STOP_GRACE_SECONDS)
            self.assertEqual(resume.call_args.args[3], "semi-stage-aaaaaaaa")
            self.assertEqual(initial["stage_id"], "semi-stage-aaaaaaaa")

        with patch("jobbot.validator._live_run", return_value=stopped) as live, \
             patch("jobbot.validator._resume_live") as resume, \
             patch("jobbot.validator._phase_execution_metrics", return_value={}), \
             patch("jobbot.validator._stage_pass", return_value=True):
            result = _run_semi_phase(object(), phase="B_REMAINING_CORE_RECENT", stage_id="semi-stage-bbbbbbbb",
                                     platforms=["linkedin"], phase_seconds=180, active_runs=[])
            self.assertEqual(live.call_count, 1)
            self.assertFalse(resume.called)
            self.assertIsNone(result["resume"])
            self.assertEqual(live.call_args.kwargs["stage_id"], "semi-stage-bbbbbbbb")
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

    def test_workspace_probe_requires_stage_scoped_recreation_proof(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _isolated_bundle(root / "validation.sqlite3", root / "out", 18765)
            Database(bundle).migrate()
            identity = {
                "resolved_database_path": str(bundle.database_path.resolve()),
                "workspace_root": str(bundle.root.resolve()),
                "jobbot_version": "3.2.3",
            }
            stage_id = "micro-stage-aaaaaaaa"
            active = {"workspace": {
                "isolated": True, "workspace_window_id": 7,
                "workspace_creation_method": "windows.create",
                "ownership_violations": 0, "non_jobbot_tab_count": 0,
                "role_tab_window_ids": {}, "worker_tab_window_ids": {}, "focus_requests_by_jobbot": 0,
                "workspace_recreation_count": 1304,
                "validation_stage_id": stage_id,
                "workspace_recreation_baseline": 1304,
                "workspace_recreation_current": 1304,
                "workspace_recreation_delta": 0,
                "workspace_recreation_baseline_captured_before_ensure": True,
            }}
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                probe = _dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)
                self.assertTrue(probe["workspace_isolation_ok"])
                self.assertTrue(probe["workspace_proof"]["workspace_recreation_unchanged"])
                self.assertTrue(probe["workspace_proof"]["workspace_recreation_proof_ok"])
            active["workspace"]["focus_requests_by_jobbot"] = 1
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)["workspace_isolation_ok"])
            active["workspace"]["focus_requests_by_jobbot"] = 0
            active["workspace"]["non_jobbot_tab_count"] = 1
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)["workspace_isolation_ok"])
            active["workspace"]["non_jobbot_tab_count"] = 0
            active["workspace"]["role_tab_window_ids"] = {"anchor": 8}
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)["workspace_isolation_ok"])
            active["workspace"]["role_tab_window_ids"] = {}
            active["workspace"]["workspace_recreation_count"] = 1305
            active["workspace"]["workspace_recreation_current"] = 1305
            active["workspace"]["workspace_recreation_delta"] = 1
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                probe = _dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)
                self.assertFalse(probe["workspace_isolation_ok"])
                self.assertFalse(probe["workspace_proof"]["workspace_recreation_unchanged"])
            active["workspace"]["workspace_recreation_baseline"] = 1304
            active["workspace"]["workspace_recreation_delta"] = 1
            active["workspace"]["validation_stage_id"] = "soak-stage-bbbbbbbb"
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)["workspace_isolation_ok"])
            active["workspace"]["validation_stage_id"] = "soak-stage-bbbbbbbb"
            active["workspace"]["workspace_recreation_baseline"] = 1305
            active["workspace"]["workspace_recreation_delta"] = 0
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                fresh_stage = _dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id="soak-stage-bbbbbbbb")
                self.assertTrue(fresh_stage["workspace_isolation_ok"])
                self.assertTrue(fresh_stage["workspace_proof"]["workspace_recreation_stage_id_matches"])
            active["workspace"].pop("workspace_recreation_baseline")
            with patch("jobbot.validator._dashboard_json", side_effect=[identity, {}, active]):
                self.assertFalse(_dashboard_probe(bundle, "http://127.0.0.1:18765/", recreation_stage_id=stage_id)["workspace_isolation_ok"])

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
