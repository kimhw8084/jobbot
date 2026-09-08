from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from jobbot import browser_tasks
from jobbot.cli import command_acceptance, parser
from jobbot.config import PROJECT_ROOT
from jobbot import run_now
from jobbot.run_now import preflight

from tests.helpers import bundle_with_database


class RunNowIntegrationTests(unittest.TestCase):
    def test_clean_room_preflight_and_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = bundle_with_database(root / "jobs.sqlite3", root / "out")
            self.assertFalse(bundle.database_path.exists())
            result = preflight(bundle, ["linkedin"])
            self.assertEqual(result.task_count, 278)
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
        self.assertIn('"open", "-g"', (bundle.root / "src/jobbot/orchestrator.py").read_text(encoding="utf-8"))
        self.assertIn('"open", "-g"', (bundle.root / "src/jobbot/run_now.py").read_text(encoding="utf-8"))

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

    def test_staged_run_now_persists_all_phases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(PROJECT_ROOT / "config", root / "config")
            run_id = browser_tasks.enqueue_production(root, "staged")
            conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=?", (run_id,)).fetchone()[0], 834)
                phases = dict(conn.execute("SELECT phase,COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? GROUP BY phase", (run_id,)).fetchall())
                self.assertEqual(phases, {
                    "A_FASTEST_DOOR_RECENT": 315,
                    "B_REMAINING_CORE_RECENT": 102,
                    "C_DEEP_BACKFILL": 417,
                })
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND max_results IS NOT NULL", (run_id,)).fetchone()[0], 0)
            finally:
                conn.close()

    def test_dashboard_database_mismatch_is_not_silently_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = bundle_with_database(Path(td) / "jobs.sqlite3", Path(td) / "out")
            run_now.Database(bundle).migrate()
            other = {
                "jobbot_version": "3.2.1",
                "workspace_root": str(bundle.root.resolve()),
                "resolved_database_path": str((Path(td) / "other.sqlite3").resolve()),
                "database_identity": "different-db",
                "pid": 12345,
            }
            with patch.object(run_now, "_dashboard_identity", return_value=other), patch.object(run_now, "_legacy_dashboard_detected", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "dashboard identity mismatch"):
                    run_now.ensure_dashboard(bundle, open_browser=False)

    def test_dashboard_subprocess_uses_supplied_bundle_environment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected_database = root / "expected.sqlite3"
            expected_output = root / "expected_output"
            stale_database = root / "stale.sqlite3"
            stale_output = root / "stale_output"
            bundle = bundle_with_database(expected_database, expected_output)
            run_now.Database(bundle).migrate()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            bundle.runtime["runtime"]["dashboard_port"] = port
            spawned: list[subprocess.Popen[bytes]] = []
            original_popen = run_now.subprocess.Popen

            def spawn(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                spawned.append(process)
                return process

            try:
                with patch.dict(os.environ, {
                    "JOBBOT_DATABASE_PATH": str(stale_database),
                    "JOBBOT_OUTPUT_DIR": str(stale_output),
                }, clear=False), patch.object(run_now.subprocess, "Popen", side_effect=spawn):
                    url, started = run_now.ensure_dashboard(bundle, open_browser=False)
                    identity = run_now._dashboard_identity(url)
                self.assertTrue(started)
                self.assertIsNotNone(identity)
                self.assertEqual(identity["resolved_database_path"], str(expected_database.resolve()))
                self.assertNotEqual(identity["resolved_database_path"], str(stale_database.resolve()))
                self.assertTrue((expected_output / "logs" / "dashboard.log").is_file())
                self.assertFalse((stale_output / "logs" / "dashboard.log").exists())
            finally:
                for process in spawned:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
