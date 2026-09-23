from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from jobbot.bridge.server import self_test
from jobbot.config import PROJECT_ROOT
from jobbot.extension_identity import extension_build


class ExtensionBridgeTests(unittest.TestCase):
    def test_loopback_rpc_token_auth(self) -> None:
        self.assertEqual(self_test(), 0)

    def test_manifest_and_javascript_syntax(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "3.2.1")
        self.assertEqual(manifest["version_name"], extension_build(PROJECT_ROOT))
        self.assertNotIn("nativeMessaging", manifest["permissions"])
        self.assertIn("windows", manifest["permissions"])
        self.assertIn("http://127.0.0.1/*", manifest["host_permissions"])
        bootstrap_platforms = {"linkedin", "indeed", "glassdoor"}
        early_bootstraps = [item for item in manifest.get("content_scripts", []) if item.get("js") == ["receiver_bootstrap.js"] and item.get("run_at") == "document_start"]
        self.assertEqual(len(early_bootstraps), 3)
        for platform in bootstrap_platforms:
            matching = [item for item in early_bootstraps if f"https://{platform}.com/*" in item.get("matches", []) and f"https://*.{platform}.com/*" in item.get("matches", [])]
            self.assertEqual(len(matching), 1, f"missing document_start bootstrap for {platform}")
            idle_receiver = [item for item in manifest.get("content_scripts", []) if item.get("run_at") == "document_idle" and f"{platform}.js" in item.get("js", [])]
            self.assertEqual(len(idle_receiver), 1, f"document_idle business receiver changed for {platform}")
        self.assertEqual({relative for item in early_bootstraps for relative in item.get("js", [])}, {"receiver_bootstrap.js"})
        self.assertFalse(any(item.get("run_at") == "document_start" and "linkedin.js" in item.get("js", []) for item in manifest.get("content_scripts", [])))
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
        self.assertEqual(set(referenced_scripts), {"receiver_bootstrap.js", "selectors.js", "common.js", "linkedin.js", "indeed.js", "glassdoor.js"})
        extension_sources = "\n".join(path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "extension").glob("*.js"))
        self.assertNotIn("chrome.scripting", extension_sources)
        self.assertNotIn("executeScript", extension_sources)
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode: self.skipTest("Node is unavailable")
        for path in sorted((PROJECT_ROOT / "extension").glob("*.js")):
            with self.subTest(path=path.name):
                self.assertNotIn(manifest["version_name"], path.read_text(encoding="utf-8"))
                checked = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
        dashboard_js = subprocess.run(["node", "--check", str(PROJECT_ROOT / "src/jobbot/web/dashboard.js")], capture_output=True, text=True)
        self.assertEqual(dashboard_js.returncode, 0, dashboard_js.stderr)
        scope = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_linkedin_scope_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(scope.returncode, 0, scope.stderr or scope.stdout)
        auth_return = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_auth_return_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(auth_return.returncode, 0, auth_return.stderr or auth_return.stdout)
        bootstrap_compatibility = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_bootstrap_compatibility_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(bootstrap_compatibility.returncode, 0, bootstrap_compatibility.stderr or bootstrap_compatibility.stdout)
        startup_lifecycle = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_startup_lifecycle_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(startup_lifecycle.returncode, 0, startup_lifecycle.stderr or startup_lifecycle.stdout)
        worker_scope = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_scope_recovery_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(worker_scope.returncode, 0, worker_scope.stderr or worker_scope.stdout)
        target_lifecycle = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_target_lifecycle_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(target_lifecycle.returncode, 0, target_lifecycle.stderr or target_lifecycle.stdout)
        receiver_recovery = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_receiver_recovery_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(receiver_recovery.returncode, 0, receiver_recovery.stderr or receiver_recovery.stdout)
        receiver_bootstrap = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_receiver_bootstrap_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(receiver_bootstrap.returncode, 0, receiver_bootstrap.stderr or receiver_bootstrap.stdout)
        delayed_receiver = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_chg166_delayed_receiver_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(delayed_receiver.returncode, 0, delayed_receiver.stderr or delayed_receiver.stdout)
        receiver_regeneration = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_receiver_regeneration_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(receiver_regeneration.returncode, 0, receiver_regeneration.stderr or receiver_regeneration.stdout)
        receiver_isolation = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_service_worker_receiver_isolation_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(receiver_isolation.returncode, 0, receiver_isolation.stderr or receiver_isolation.stdout)
        primary_scope = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_primary_scope_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(primary_scope.returncode, 0, primary_scope.stderr or primary_scope.stdout)
        dashboard = (PROJECT_ROOT / "extension" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('src="dashboard.js"', dashboard)
        self.assertNotIn('src="start.js"', dashboard)
        self.assertTrue((PROJECT_ROOT / "extension" / "dashboard.js").is_file())
        worker = (PROJECT_ROOT / "extension" / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn("function requireRpcOk", worker)
        self.assertIn("requiredRequest('record_result'", worker)
        self.assertNotIn("if(rec.ok)", worker)
        self.assertNotIn("active:true", worker)
        self.assertIn("active:false", worker)
        self.assertIn("jobbot_bridge_state", worker)
        self.assertIn("runtime_config", worker)
        self.assertIn("function normalizeSearchUrl", worker)
        self.assertIn("searchParams.delete('currentJobId')", worker)
        self.assertIn("function createBackgroundTarget", worker)
        self.assertIn("state:requestedMode==='minimized_owned'?'minimized':'normal'", worker)
        self.assertIn("minimized_owned", worker)
        self.assertIn("normal_owned", worker)
        self.assertIn("inactive_existing", worker)
        self.assertIn("focused:false", worker)
        self.assertIn("JOBBOT_INSPECT_AUTH", worker)
        self.assertIn("JOBBOT_INSPECT_SEARCH_EVENTUALLY", worker)
        self.assertIn("target_diagnostic_matrix", worker)
        self.assertIn("function keepBackgroundTab", worker)
        self.assertIn("function closeBackgroundTarget", worker)
        self.assertIn("await keepBackgroundTab(searchTab.id,searchTarget.window_id)", worker)
        self.assertIn("windowId:searchTarget.window_id", worker)
        self.assertIn("another browser run is still active", worker)
        self.assertIn("active_run_id:active", worker)
        self.assertIn("JOBBOT_REFRESH_EXTENSION", worker)
        self.assertIn("extension_refresh", worker)
        self.assertIn("extension_build", worker)
        self.assertIn("deployment_identity.json", worker)
        self.assertIn("deployment_identity", worker)
        self.assertIn("chrome.runtime.reload", worker)
        self.assertIn("JOBBOT_INSPECT_SEARCH_PANE", worker)
        self.assertIn("receiver_recovery", worker)
        self.assertIn("chrome.tabs.reload", worker)
        self.assertIn("POST_RELOAD_RECEIVER_READY_TIMEOUT_MS", worker)
        self.assertIn("target_regeneration_receiver_deadline_exhausted", worker)
        self.assertIn("receiver_attachment_min_sequence", worker)
        self.assertIn("Promise.allSettled([...workerPromises.values()])", worker)
        self.assertNotIn("chrome.scripting", worker)
        self.assertNotIn("executeScript", worker)
        self.assertIn("JOBBOT_INSPECT_SEARCH_EVENTUALLY", worker)
        self.assertIn("worker_count:3", worker)
        self.assertIn("search_pane_evidence", worker)
        self.assertIn("workerPromises", worker)
        self.assertNotIn("activeTaskId", worker)
        self.assertNotIn("runPromise", worker)
        self.assertIn("search_pane_evidence", worker)
        dashboard_worker = (PROJECT_ROOT / "extension" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("chrome.runtime.getManifest()", dashboard_worker)
        self.assertIn("query.get('expected_build')", dashboard_worker)
        self.assertIn("loadedManifest.version_name", dashboard_worker)
        self.assertIn("maintenanceMode", dashboard_worker)
        self.assertIn("refresh_id", dashboard_worker)
        self.assertNotIn("const HEARTBEAT_MS", worker)
        self.assertNotIn("const WATCHDOG_MS", worker)


if __name__ == "__main__": unittest.main()
