from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from jobbot.bridge.server import self_test
from jobbot.config import PROJECT_ROOT


class ExtensionBridgeTests(unittest.TestCase):
    def test_loopback_rpc_token_auth(self) -> None:
        self.assertEqual(self_test(), 0)

    def test_manifest_and_javascript_syntax(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "3.2.1")
        self.assertNotIn("nativeMessaging", manifest["permissions"])
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
                checked = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
        dashboard_js = subprocess.run(["node", "--check", str(PROJECT_ROOT / "src/jobbot/web/dashboard.js")], capture_output=True, text=True)
        self.assertEqual(dashboard_js.returncode, 0, dashboard_js.stderr)
        scope = subprocess.run(["node", str(PROJECT_ROOT / "tests/extension_linkedin_scope_test.js")], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(scope.returncode, 0, scope.stderr or scope.stdout)
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
        self.assertNotIn("const HEARTBEAT_MS", worker)
        self.assertNotIn("const WATCHDOG_MS", worker)


if __name__ == "__main__": unittest.main()
