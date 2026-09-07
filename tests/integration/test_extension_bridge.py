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
        self.assertEqual(manifest["version"], "3.2.0")
        self.assertNotIn("nativeMessaging", manifest["permissions"])
        self.assertIn("http://127.0.0.1/*", manifest["host_permissions"])
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode: self.skipTest("Node is unavailable")
        for path in sorted((PROJECT_ROOT / "extension").glob("*.js")):
            with self.subTest(path=path.name):
                checked = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__": unittest.main()
