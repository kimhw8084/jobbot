from __future__ import annotations

import os
import copy
import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from jobbot import browser_tasks
from jobbot.cli import command_acceptance, command_run, parser
from jobbot.config import PROJECT_ROOT
from jobbot.dashboard import create_server
from jobbot import run_now
from jobbot.run_now import preflight
from jobbot.run_now import assert_production_release
from jobbot.config import ConfigBundle, load_bundle

from tests.helpers import bundle_with_database


class RunNowIntegrationTests(unittest.TestCase):
    def test_production_release_guard_rejects_tracked_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = load_bundle(PROJECT_ROOT)
            runtime = copy.deepcopy(original.runtime)
            runtime["runtime"]["database_path"] = "data/jobs.sqlite3"
            runtime["runtime"]["output_dir"] = "out"
            runtime["runtime"]["dashboard_port"] = 8765
            bundle = ConfigBundle(root, original.strategy, original.candidate, runtime)
            (root / "data").mkdir()
            (root / "out" / "production-validation").mkdir(parents=True)
            (root / "extension").mkdir()
            (root / "data" / "jobs.sqlite3").touch()
            (root / "out" / "production-validation" / "latest.json").write_text(
                json.dumps({"PROD_READY": True, "internal_failures": [], "head": "abc", "extension_build": "build"}),
                encoding="utf-8",
            )
            (root / "extension" / "manifest.json").write_text(json.dumps({"version_name": "build"}), encoding="utf-8")
            dirty = subprocess.CompletedProcess([], 0, stdout=" M src/jobbot/run_now.py\n", stderr="")
            with patch("jobbot.run_now.subprocess.check_output", return_value="abc\n"), \
                 patch("jobbot.run_now.subprocess.run", return_value=dirty), \
                 patch("jobbot.run_now.Database.integrity_check", return_value="ok"):
                with self.assertRaisesRegex(RuntimeError, "tracked working tree is dirty"):
                    assert_production_release(bundle)

    def test_production_run_subcommand_cannot_bypass_release_guard(self) -> None:
        bundle = bundle_with_database(PROJECT_ROOT / "data" / "jobs.sqlite3", PROJECT_ROOT / "out")
        args = parser().parse_args(["run", "--mode", "fast", "--enqueue-only"])
        with patch("jobbot.cli._bundle", return_value=bundle), \
             patch("jobbot.cli.assert_production_release") as guard, \
             patch("jobbot.cli.compile_and_write"), \
             patch("jobbot.cli.enqueue", return_value=123):
            self.assertEqual(command_run(args), 0)
        guard.assert_called_once_with(bundle)
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
        self.assertIn("open_dashboard_workspace", (bundle.root / "src/jobbot/run_now.py").read_text(encoding="utf-8"))

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
            spawned: list[tuple[object, object, object]] = []
            def spawn(*args, **kwargs):
                captured_env = kwargs["env"]
                server = create_server(bundle, host="127.0.0.1", port=port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                class FakeProcess:
                    def terminate(self):
                        server.shutdown()

                    def wait(self, timeout=None):
                        server.server_close()
                        thread.join(timeout)

                process = FakeProcess()
                spawned.append((process, captured_env, args))
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
                _process, child_env, _args = spawned[0]
                self.assertEqual(child_env["JOBBOT_DATABASE_PATH"], str(expected_database.resolve()))
                self.assertEqual(child_env["JOBBOT_OUTPUT_DIR"], str(expected_output.resolve()))
                self.assertEqual(child_env["JOBBOT_DASHBOARD_PORT"], str(port))
                self.assertTrue((expected_output / "logs" / "dashboard.log").is_file())
                self.assertFalse((stale_output / "logs" / "dashboard.log").exists())
            finally:
                for process, _child_env, _args in spawned:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
