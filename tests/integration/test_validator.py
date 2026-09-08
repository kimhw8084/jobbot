from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT
from jobbot.validator import _assert_isolated, _stage_pass, _write_report


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


if __name__ == "__main__": unittest.main()
