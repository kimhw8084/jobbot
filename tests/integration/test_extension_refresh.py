from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jobbot import browser_tasks
from jobbot.bridge import rpc
from jobbot.bridge.server import BridgeServer, Handler
from jobbot.config import PROJECT_ROOT
from jobbot.extension_identity import extension_build
from jobbot.runtime_binding import DEPLOYMENT_MARKER, sync_extension


PREDECESSOR_EXTENSION_BUILD = "3.2.2-prod-ready.672cf88.10"
REPAIRED_EXTENSION_BUILD = "3.2.2-prod-ready.672cf88.18"


class ExtensionRefreshIntegrationTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        shutil.copytree(PROJECT_ROOT / "extension", root / "extension")
        (root / "data").mkdir()
        (root / "out").mkdir()
        return root

    def with_root(self, td: str):
        root = self.make_root(td)
        self.previous_deploy_dir = os.environ.get("JOBBOT_EXTENSION_DEPLOY_DIR")
        os.environ["JOBBOT_EXTENSION_DEPLOY_DIR"] = str(root / "deployment")
        self.addCleanup(self.restore_deploy_dir)
        sync_extension(root)
        previous = rpc.BASE
        rpc.BASE = root
        self.addCleanup(setattr, rpc, "BASE", previous)
        return root

    def restore_deploy_dir(self) -> None:
        if self.previous_deploy_dir is None:
            os.environ.pop("JOBBOT_EXTENSION_DEPLOY_DIR", None)
        else:
            os.environ["JOBBOT_EXTENSION_DEPLOY_DIR"] = self.previous_deploy_dir

    def runtime_identity(self, root: Path) -> dict[str, object]:
        return json.loads((root / "deployment" / "extension" / DEPLOYMENT_MARKER).read_text(encoding="utf-8"))

    def request(self, root: Path, refresh_id: str, **extra):
        return rpc.handle({
            "action": "extension_refresh", "request_id": refresh_id, "refresh_id": refresh_id,
            "expected_build": extension_build(root), **extra,
        })

    def test_loopback_authorization_is_required_for_refresh_control(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            token = secrets.token_urlsafe(32)
            server = BridgeServer(("127.0.0.1", 0), Handler, token)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"action": "extension_refresh", "expected_build": extension_build(root)}).encode()
                unauthenticated = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/rpc", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(unauthenticated, timeout=5)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(
                    json.loads(caught.exception.read().decode())["classification"],
                    "bridge_auth_or_configuration_failure",
                )
                authorized = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/rpc", data=body,
                    headers={"Content-Type": "application/json", "X-JobBot-Token": token}, method="POST",
                )
                with urllib.request.urlopen(authorized, timeout=5) as response:
                    value = json.loads(response.read().decode())
                self.assertTrue(value["ok"])
                self.assertFalse(value["refreshed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unavailable_extension_remains_pending_and_never_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            pending = self.request(root, "unavailable")
            self.assertEqual(pending["status"], "pending")
            self.assertFalse(pending["identity_confirmed"])
            reloading = rpc.handle({"action": "extension_refresh_reloading", "refresh_id": "unavailable"})
            self.assertEqual(reloading["status"], "reloading")
            failed = rpc.handle({
                "action": "extension_refresh_failed", "refresh_id": "unavailable",
                "error": "extension_unavailable_or_unreachable",
            })
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["status"], "failed")
            status = rpc.handle({"action": "extension_refresh_status", "refresh_id": "unavailable"})
            self.assertTrue(status["ok"])
            self.assertEqual(status["status"], "failed")
            self.assertFalse(status["refreshed"])
            self.assertEqual(status["diagnostics"]["classification"], "extension_absent_disabled_or_unavailable")

    def test_repaired_identity_is_distinct_and_predecessor_build_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            expected = extension_build(root)
            self.assertEqual(expected, REPAIRED_EXTENSION_BUILD)
            self.assertNotEqual(expected, PREDECESSOR_EXTENSION_BUILD)
            wrong_expected = rpc.handle({
                "action": "extension_refresh", "refresh_id": "wrong",
                "expected_build": PREDECESSOR_EXTENSION_BUILD,
            })
            self.assertEqual(wrong_expected["error"], "expected_build_mismatch")
            self.assertEqual(wrong_expected["expected_build"], expected)
            self.request(root, "stale")
            signal = rpc.handle({
                "action": "extension_build", "refresh_id": "stale", "build": PREDECESSOR_EXTENSION_BUILD,
                "expected_build": expected,
                "deployment_identity": self.runtime_identity(root),
            })
            self.assertFalse(signal["ok"])
            self.assertEqual(signal["error"], "stale_or_wrong_extension_build")
            status = rpc.handle({"action": "extension_refresh_status", "refresh_id": "stale"})
            self.assertEqual(status["status"], "failed")
            self.assertFalse(status["refreshed"])
            self.assertEqual(status["diagnostics"]["classification"], "wrong_or_stale_build")

    def test_wrong_deployment_source_is_rejected_even_when_build_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            expected = extension_build(root)
            self.request(root, "wrong-source")
            observed = self.runtime_identity(root)
            observed["source_identity"] = "sha256:wrong"
            signal = rpc.handle({
                "action": "extension_build", "refresh_id": "wrong-source", "build": expected,
                "expected_build": expected, "deployment_identity": observed,
            })
            self.assertFalse(signal["ok"])
            self.assertEqual(signal["error"], "bootstrap_or_deployment_source_mismatch")
            self.assertEqual(signal["diagnostics"]["classification"], "bootstrap_or_deployment_source_mismatch")

    def test_successful_identity_confirmation_is_idempotent_and_survives_bridge_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            expected = extension_build(root)
            first = self.request(root, "success")
            duplicate = self.request(root, "success")
            self.assertEqual(first["refresh_id"], duplicate["refresh_id"])
            self.assertFalse(duplicate["refreshed"])
            deduplicated = self.request(root, "different-request-id")
            self.assertEqual(deduplicated["refresh_id"], "success")
            self.assertTrue(deduplicated["deduplicated"])
            confirmed = rpc.handle({
                "action": "extension_build", "refresh_id": "success", "build": expected,
                "expected_build": expected,
                "deployment_identity": self.runtime_identity(root),
            })
            self.assertTrue(confirmed["ok"])
            self.assertTrue(confirmed["identity_confirmed"])
            self.assertTrue(confirmed["refreshed"])
            # rpc.handle opens a fresh SQLite connection for each request, the
            # same lifecycle used after a bridge process restart.
            after_restart = rpc.handle({"action": "extension_refresh_status", "refresh_id": "success"})
            self.assertTrue(after_restart["identity_confirmed"])
            self.assertTrue(after_restart["refreshed"])
            repeated = self.request(root, "success")
            self.assertTrue(repeated["refreshed"])
            conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM extension_refresh_requests WHERE refresh_id='success'"
                ).fetchone()[0], 1)
            finally:
                conn.close()

    def test_legacy_build_only_confirmation_cannot_prove_the_repaired_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            expected = extension_build(root)
            self.request(root, "legacy-confirmed")
            conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
            try:
                conn.execute(
                    "UPDATE extension_refresh_requests SET status='confirmed',observed_build=? WHERE refresh_id=?",
                    (expected, "legacy-confirmed"),
                )
                conn.commit()
            finally:
                conn.close()
            legacy = rpc.handle({"action": "extension_refresh_status", "refresh_id": "legacy-confirmed"})
            self.assertFalse(legacy["identity_confirmed"])
            confirmed = rpc.handle({
                "action": "extension_build", "refresh_id": "legacy-confirmed", "build": expected,
                "expected_build": expected, "deployment_identity": self.runtime_identity(root),
            })
            self.assertTrue(confirmed["identity_confirmed"])

    def test_active_run_is_never_reloaded_or_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            run_id = browser_tasks.enqueue_validation(root, ["linkedin"])
            self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
            blocked = self.request(root, "active", run_id=run_id)
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"], "active_run")
            conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM extension_refresh_requests").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()[0], "running")
            finally:
                conn.close()

    def test_refresh_confirmation_does_not_break_checkpoint_resume(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.with_root(td)
            run_id = browser_tasks.enqueue_validation(root, ["linkedin"])
            expected = extension_build(root)
            queued = self.request(root, "resume", run_id=run_id)
            self.assertEqual(queued["status"], "pending")
            self.assertEqual(rpc.handle({"action": "begin_run", "run_id": run_id})["error"], "extension_build_unconfirmed")
            self.assertTrue(rpc.handle({
                "action": "extension_build", "run_id": run_id, "refresh_id": "resume", "build": expected,
                "expected_build": expected,
                "deployment_identity": self.runtime_identity(root),
            })["ok"])
            self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
            task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "refresh-test"})["task"]
            checkpoint = {"search_url": "https://www.linkedin.com/jobs/search/?keywords=resume", "page_number": 4}
            self.assertTrue(rpc.handle({
                "action": "task_progress", "run_id": run_id, "task_id": task["task_id"],
                "results_seen": 2, "pages_visited": 4, "checkpoint": checkpoint,
            })["ok"])
            browser_tasks.resume_run(root, run_id)
            conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
            try:
                row = conn.execute("SELECT status,checkpoint_json FROM browser_search_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
                self.assertEqual(row[0], "queued")
                self.assertEqual(json.loads(row[1]), checkpoint)
                self.assertEqual(conn.execute("SELECT status FROM extension_refresh_requests WHERE refresh_id='resume'").fetchone()[0], "confirmed")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
