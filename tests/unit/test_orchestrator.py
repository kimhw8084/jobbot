from __future__ import annotations

import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from jobbot import orchestrator
from tests.helpers import bundle_with_database


class OrchestratorMacLauncherTests(unittest.TestCase):
    def test_macos_delivers_url_as_launchservices_operand(self) -> None:
        url = "chrome-extension://example/dashboard.html?run_id=41&bridge_token=synthetic"
        with patch.object(orchestrator.sys, "platform", "darwin"), \
             patch.object(orchestrator.subprocess, "run") as run, \
             patch.object(orchestrator.subprocess, "Popen") as popen:
            orchestrator._open_chrome(url)

        command = run.call_args.args[0]
        self.assertEqual(command, ["open", "-g", "-a", "Google Chrome", url])
        self.assertEqual(run.call_args.kwargs["check"], True)
        self.assertNotIn("--args", command)
        self.assertNotIn("-n", command)
        self.assertNotIn("--new-window", command)
        self.assertFalse(popen.called)

    def test_macos_open_failure_surfaces_without_rendezvous_url(self) -> None:
        url = "chrome-extension://example/dashboard.html?bridge_token=synthetic"
        failure = subprocess.CalledProcessError(72, ["open"])
        with patch.object(orchestrator.sys, "platform", "darwin"), \
             patch.object(orchestrator.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Google Chrome URL delivery failed") as raised:
                orchestrator._open_chrome(url)

        self.assertNotIn("bridge_token", str(raised.exception))
        self.assertNotIn(url, str(raised.exception))

    def test_live_startup_builds_rendezvous_url_and_calls_delivery_primitive(self) -> None:
        class FakeBridgeProcess:
            def __init__(self, args: list[str]) -> None:
                ready = Path(args[args.index("--ready-file") + 1])
                ready.touch()
                self.returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

        with tempfile.TemporaryDirectory() as td:
            bundle = bundle_with_database(Path(td) / "validation.sqlite3", Path(td) / "out")

            def spawn(args: list[str], **_kwargs: object) -> FakeBridgeProcess:
                return FakeBridgeProcess(args)

            with patch.object(orchestrator.subprocess, "Popen", side_effect=spawn), \
                 patch.object(orchestrator, "_health", return_value={"ok": True}), \
                 patch.object(orchestrator, "_run_status", return_value="queued"), \
                 patch.object(orchestrator, "_open_chrome") as open_chrome, \
                 patch.object(orchestrator, "_free_high_port", return_value=52625), \
                 patch.object(orchestrator.secrets, "token_urlsafe", return_value="synthetic-token"):
                outcome = orchestrator.launch_browser_run(
                    bundle,
                    41,
                    wait=False,
                    open_browser=True,
                    dashboard_url="http://127.0.0.1:8765/",
                )

        self.assertEqual(outcome.status, "queued")
        self.assertEqual(open_chrome.call_count, 1)
        rendezvous = open_chrome.call_args.args[0]
        parsed = urllib.parse.urlparse(rendezvous)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "chrome-extension")
        self.assertEqual(query["autorun"], ["1"])
        self.assertEqual(query["run_id"], ["41"])
        self.assertEqual(query["bridge_port"], ["52625"])
        self.assertEqual(query["bridge_token"], ["synthetic-token"])
        self.assertEqual(query["expected_build"], ["3.2.3-static-hardening.2"])
        self.assertEqual(query["expected_version"], ["3.2.3"])
        self.assertEqual(
            query["expected_runtime_digest"],
            ["957762fca849fb32d353fed471016997eb0a808271f3c03b6b77bd11dae5cdc7"],
        )
        self.assertEqual(query["dashboard_url"], ["http://127.0.0.1:8765/"])


if __name__ == "__main__":
    unittest.main()
