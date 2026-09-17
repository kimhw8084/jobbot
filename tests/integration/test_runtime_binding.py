from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import unittest
from pathlib import Path
from unittest.mock import patch

from jobbot import browser_tasks
from jobbot import orchestrator
from jobbot import runtime_binding
from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.extension_identity import extension_build
from jobbot.orchestrator import refresh_extension
from jobbot.runtime_binding import (
    CHROME_PROFILE_BINDING,
    DEPLOYMENT_MARKER,
    bind_chrome_profile,
    chrome_open_command,
    chrome_profile_binding_path,
    chrome_target_diagnostics,
    deployment_diagnostics,
    runtime_binding_diagnostics,
    stable_extension_root,
    sync_extension,
)


class RuntimeBindingIntegrationTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        shutil.copytree(PROJECT_ROOT / "extension", root / "extension")
        (root / "data").mkdir()
        (root / "out").mkdir()
        return root

    def runtime_env(self, root: Path) -> dict[str, str]:
        return {"JOBBOT_EXTENSION_DEPLOY_DIR": str(root / "machine-local")}

    def test_exact_source_identity_is_deployed_to_stable_path_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            with patch.dict(os.environ, self.runtime_env(root), clear=False):
                first = sync_extension(root)
                marker_path = stable_extension_root() / DEPLOYMENT_MARKER
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                marker_before = marker_path.read_text(encoding="utf-8")
                second = sync_extension(root)
                self.assertEqual(first, second)
                self.assertEqual(marker_before, marker_path.read_text(encoding="utf-8"))
                self.assertNotEqual(first.extension_root, root / "extension")
                self.assertEqual(marker["extension_id"], (root / "config" / "EXTENSION_ID.txt").read_text().strip())
                self.assertEqual(marker["version_name"], extension_build(root))
                self.assertEqual(marker["source_identity"], first.source_identity)
                self.assertEqual(deployment_diagnostics(root)["state"], "current")

    def test_source_change_and_wrong_marker_are_repaired_without_ephemeral_binding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            with patch.dict(os.environ, self.runtime_env(root), clear=False):
                info = sync_extension(root)
                marker_path = stable_extension_root() / DEPLOYMENT_MARKER
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                marker["source_identity"] = "sha256:wrong"
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
                self.assertFalse(deployment_diagnostics(root)["ok"])
                repaired = sync_extension(root)
                self.assertEqual(repaired.source_identity, info.source_identity)
                with (root / "extension" / "service_worker.js").open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                changed = sync_extension(root)
                self.assertNotEqual(changed.source_identity, info.source_identity)
                self.assertEqual(deployment_diagnostics(root)["deployed_identity"], changed.source_identity)

    def test_profile_targeting_is_explicit_and_persists_across_chrome_restart_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            with patch.dict(os.environ, self.runtime_env(root), clear=False):
                sync_extension(root)
                unbound = chrome_target_diagnostics()
                self.assertFalse(unbound["ok"])
                self.assertEqual(unbound["classification"], "wrong_or_untargeted_chrome_profile_or_instance")
                with self.assertRaises(ValueError):
                    bind_chrome_profile("../Default")
                binding = bind_chrome_profile("Profile 1")
                self.assertEqual(binding["extension_root"], str(stable_extension_root()))
                self.assertEqual(chrome_target_diagnostics()["profile_directory"], "Profile 1")
                command = chrome_open_command("chrome-extension://jfdlmelgonjhgnabpbipjefgamedpgfb/dashboard.html", "/ordinary/chrome")
                self.assertIn("--profile-directory=Profile 1", command)
                self.assertNotIn(str(root), " ".join(command))
                self.assertTrue(chrome_profile_binding_path().name == CHROME_PROFILE_BINDING)
                # A Chrome restart does not change the persisted binding file.
                self.assertEqual(chrome_target_diagnostics()["state"], "bound")

    def test_runtime_diagnostics_distinguish_bootstrap_and_profile_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            with patch.dict(os.environ, self.runtime_env(root), clear=False):
                before = runtime_binding_diagnostics(root)
                self.assertFalse(before["ok"])
                self.assertEqual(before["classification"], "bootstrap_or_deployment_source_mismatch")
                sync_extension(root)
                unbound = runtime_binding_diagnostics(root)
                self.assertFalse(unbound["ok"])
                self.assertEqual(unbound["classification"], "wrong_or_untargeted_chrome_profile_or_instance")
                bind_chrome_profile("Default")
                ready = runtime_binding_diagnostics(root)
                self.assertTrue(ready["ok"])
                self.assertEqual(ready["classification"], "runtime_binding_ready")

    def test_maintenance_refresh_requires_bootstrap_and_reports_structured_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            bundle = load_bundle(root)
            with patch.dict(os.environ, self.runtime_env(root), clear=False):
                result = refresh_extension(bundle, timeout_seconds=1, open_browser=False)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "chrome_profile_binding_required")
                self.assertEqual(result["diagnostics"]["classification"], "wrong_or_untargeted_chrome_profile_or_instance")
                self.assertEqual(result["deployment"]["version_name"], extension_build(root))

    def _capture_launcher_url(self, root: Path, action):
        bundle = load_bundle(root)
        opened: list[str] = []

        class FakeProcess:
            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

        def fake_popen(command, **_kwargs):
            ready_path = Path(command[command.index("--ready-file") + 1])
            ready_path.write_text(json.dumps({"port": 43123}), encoding="utf-8")
            return FakeProcess()

        with patch.dict(os.environ, self.runtime_env(root), clear=False), \
                patch.object(runtime_binding, "_source_head", return_value="test-head"), \
                patch.object(orchestrator, "chrome_target_diagnostics", return_value={"ok": True}), \
                patch.object(orchestrator.subprocess, "Popen", side_effect=fake_popen), \
                patch.object(orchestrator, "_health", return_value={"ok": True}), \
                patch.object(orchestrator, "_request_extension_refresh", return_value={"ok": True}), \
                patch.object(orchestrator, "_bridge_rpc", return_value={"ok": True, "status": "confirmed", "identity_confirmed": True}), \
                patch.object(orchestrator, "_run_status", return_value="missing"), \
                patch.object(orchestrator, "_open_chrome", side_effect=opened.append):
            result = action(bundle)
        return result, opened

    def test_maintenance_refresh_opens_autorun_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            result, opened = self._capture_launcher_url(
                root, lambda bundle: orchestrator.refresh_extension(bundle, timeout_seconds=1)
            )

            self.assertTrue(result["ok"])
            self.assertEqual(len(opened), 1)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)
            self.assertEqual(query["maintenance"], ["1"])
            self.assertEqual(query["autorun"], ["1"])

    def test_run_and_resume_launcher_keep_autorun_dashboard_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            outcome, opened = self._capture_launcher_url(
                root, lambda bundle: orchestrator.launch_browser_run(bundle, 42, wait=False)
            )

            self.assertEqual(outcome.status, "missing")
            self.assertEqual(len(opened), 1)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)
            self.assertEqual(query["autorun"], ["1"])
            self.assertNotIn("maintenance", query)

    def test_maintenance_refresh_does_not_replace_deployment_during_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            bundle = load_bundle(root)
            with patch.dict(os.environ, self.runtime_env(root), clear=False):
                info = sync_extension(root)
                run_id = browser_tasks.enqueue_validation(root, ["linkedin"])
                connection = sqlite3.connect(bundle.database_path)
                try:
                    connection.execute("UPDATE browser_runs SET status='running' WHERE browser_run_id=?", (run_id,))
                    connection.commit()
                finally:
                    connection.close()
                result = refresh_extension(bundle, timeout_seconds=1, open_browser=False)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "active_run")
                self.assertEqual(result["active_run_id"], run_id)
                self.assertEqual(result["diagnostics"]["classification"], "active_run")
                self.assertEqual(deployment_diagnostics(root)["deployed_identity"], info.source_identity)


if __name__ == "__main__":
    unittest.main()
