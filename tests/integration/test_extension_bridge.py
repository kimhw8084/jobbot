from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from jobbot.bridge.server import self_test
from jobbot.config import PROJECT_ROOT
from jobbot.extension_identity import expected_identity, metadata


class ExtensionBridgeTests(unittest.TestCase):
    def test_loopback_rpc_token_auth(self) -> None:
        self.assertEqual(self_test(), 0)

    def test_manifest_and_javascript_syntax(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "3.2.3")
        self.assertEqual(manifest["version_name"], "3.2.3-static-hardening.1")
        identity = expected_identity(PROJECT_ROOT)
        build_meta = metadata(PROJECT_ROOT)
        self.assertTrue(identity["runtime_digest"])
        self.assertEqual(build_meta["extension_build"], identity["extension_build"])
        self.assertEqual(build_meta["runtime_digest"], identity["runtime_digest"])
        parent = subprocess.run(["git", "rev-parse", "HEAD^"], cwd=PROJECT_ROOT, capture_output=True, text=True)
        if parent.returncode == 0:
            changed = subprocess.run(
                ["git", "diff", "--name-only", parent.stdout.strip(), "--", "extension"],
                cwd=PROJECT_ROOT, capture_output=True, text=True,
            ).stdout.splitlines()
            runtime_changed = [path for path in changed if path != "extension/manifest.json"]
            if runtime_changed:
                self.assertIn("extension/manifest.json", changed,
                              "runtime extension bytes changed without a new manifest build identity")
        self.assertNotIn("nativeMessaging", manifest["permissions"])
        self.assertIn("windows", manifest["permissions"])
        self.assertIn("http://127.0.0.1/*", manifest["host_permissions"])
        referenced_scripts = []
        for content_script in manifest.get("content_scripts", []):
            for relative in content_script.get("js", []):
                referenced_scripts.append(relative)
                self.assertTrue((PROJECT_ROOT / "extension" / relative).is_file(), relative)
        service_worker = manifest.get("background", {}).get("service_worker")
        if service_worker:
            self.assertTrue((PROJECT_ROOT / "extension" / service_worker).is_file(), service_worker)
        for resource_group in manifest.get("web_accessible_resources", []):
            for relative in resource_group.get("resources", []):
                self.assertTrue((PROJECT_ROOT / "extension" / relative).is_file(), relative)
        self.assertNotIn("linkedin-inject.js", json.dumps(manifest))
        self.assertFalse((PROJECT_ROOT / "extension" / "linkedin-inject.js").exists())
        self.assertEqual(set(referenced_scripts), {"selectors.js", "common.js", "linkedin.js", "indeed.js", "glassdoor.js"})
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode: self.skipTest("Node is unavailable")
        for path in sorted((PROJECT_ROOT / "extension").glob("*.js")):
            with self.subTest(path=path.name):
                self.assertNotIn(manifest["version_name"], path.read_text(encoding="utf-8"), "extension JavaScript must read version_name from manifest")
                checked = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
        dashboard_js = subprocess.run(["node", "--check", str(PROJECT_ROOT / "src/jobbot/web/dashboard.js")], capture_output=True, text=True)
        self.assertEqual(dashboard_js.returncode, 0, dashboard_js.stderr)
        scope = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_linkedin_scope_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(scope.returncode, 0, scope.stderr or scope.stdout)
        primary_scope = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_primary_scope_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(primary_scope.returncode, 0, primary_scope.stderr or primary_scope.stdout)
        workspace = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_workspace_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(workspace.returncode, 0, workspace.stderr or workspace.stdout)
        dashboard = (PROJECT_ROOT / "extension" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('src="dashboard.js"', dashboard)
        self.assertNotIn('src="start.js"', dashboard)
        self.assertTrue((PROJECT_ROOT / "extension" / "dashboard.js").is_file())
        worker = (PROJECT_ROOT / "extension" / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn("function requireRpcOk", worker)
        self.assertIn("requiredRequest('record_result'", worker)
        self.assertNotIn("if(rec.ok)", worker)
        self.assertIn("active:false", worker)
        self.assertIn("jobbot_bridge_state", worker)
        self.assertIn("runtime_config", worker)
        self.assertIn("function normalizeSearchUrl", worker)
        self.assertIn("searchParams.delete('currentJobId')", worker)
        self.assertIn("async function ensureWorkspace", worker)
        self.assertIn("async function createOwnedTab", worker)
        self.assertIn("state:'normal'", worker)
        self.assertIn("focused:false", worker)
        self.assertIn("function keepBackgroundTab", worker)
        self.assertNotIn("chrome.tabs.create({url,active:false})", worker)
        self.assertIn("windowId:workspace.window_id", worker)
        self.assertIn("function adoptionWindowSafe", worker)
        self.assertIn("controller_original_window_had_non_jobbot_tabs", worker)
        self.assertIn("function activateDashboardTab", worker)
        self.assertIn("chrome.tabs.update(tabId,{active:true})", worker)
        self.assertIn("role_tab_window_ids", worker)
        self.assertIn("const stopAfterCards=await requiredRequest('should_stop'", worker)
        self.assertIn("const stopBeforePagination=await requiredRequest('should_stop'", worker)
        self.assertNotIn("chrome.tabs.update(searchTab.id,{active:true})", worker)
        self.assertNotIn("chrome.tabs.update(detailTab.id,{active:true})", worker)
        self.assertNotIn("chrome.windows.update", worker)
        self.assertNotIn("windows.update", worker)
        self.assertIn("ownership_violations", worker)
        self.assertIn("active:true", worker)  # dashboard-only activation is explicit
        self.assertNotIn("tabs.create({url", worker)
        self.assertNotIn("tabs.create({ url", worker)
        self.assertIn("another browser run is still active", worker)
        self.assertIn("active_run_id:active", worker)
        dashboard_worker = (PROJECT_ROOT / "extension" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("loadedManifest.version_name", dashboard_worker)
        self.assertIn("expected_build", dashboard_worker)
        self.assertNotIn("const HEARTBEAT_MS", worker)
        self.assertNotIn("const WATCHDOG_MS", worker)


if __name__ == "__main__": unittest.main()
