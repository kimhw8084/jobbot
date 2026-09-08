from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from jobbot.cli import command_acceptance, parser
from jobbot.run_now import preflight

from tests.helpers import bundle_with_database


class RunNowIntegrationTests(unittest.TestCase):
    def test_clean_room_preflight_and_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = bundle_with_database(root / "jobs.sqlite3", root / "out")
            self.assertFalse(bundle.database_path.exists())
            result = preflight(bundle, ["linkedin"])
            self.assertEqual(result.task_count, 105)
            self.assertTrue(result.database_path.is_file())
            self.assertTrue((root / "out" / "search_plan.json").is_file())
        args = parser().parse_args(["run-now", "--platform", "linkedin", "--enqueue-only", "--no-open"])
        self.assertEqual(args.command, "run-now")
        launcher = bundle.root / "RUN_NOW.command"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        launcher_text = launcher.read_text(encoding="utf-8")
        self.assertIn("-m jobbot run-now", launcher_text)
        self.assertIn('"$@"', launcher_text)

    def test_acceptance_can_use_an_isolated_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "acceptance.sqlite3"
            output = Path(td) / "out"
            with patch.dict(os.environ, {
                "JOBBOT_DATABASE_PATH": str(database), "JOBBOT_OUTPUT_DIR": str(output),
                "JOBBOT_DASHBOARD_PORT": "8766",
            }, clear=False):
                code = command_acceptance(Namespace(
                    platform="linkedin", days=7, max_results=20,
                    enqueue_only=True, no_open=True, use_production=False,
                ))
            self.assertEqual(code, 0)
            conn = sqlite3.connect(database)
            try:
                self.assertEqual(conn.execute("SELECT mode FROM browser_runs").fetchone()[0], "acceptance")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks").fetchone()[0], 3)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
