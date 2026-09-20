from __future__ import annotations

import copy
import json
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
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.dashboard import create_server
from jobbot.db import Database


class Chg146R2ControlIntegrationTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        shutil.copytree(PROJECT_ROOT / "src", root / "src")
        (root / "data").mkdir()
        (root / "out").mkdir()
        return root

    def bundle(self, root: Path) -> ConfigBundle:
        original = load_bundle(root)
        runtime = copy.deepcopy(original.runtime)
        runtime["runtime"]["database_path"] = str(root / "data" / "jobs.sqlite3")
        runtime["runtime"]["output_dir"] = str(root / "out")
        runtime["runtime"]["crawl_observations_path"] = str(root / "data" / "crawl_observations.sqlite3")
        return ConfigBundle(root, original.strategy, original.candidate, runtime)

    def post(self, base: str, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def start_server(self, root: Path):
        server = create_server(self.bundle(root), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        return server

    def test_http_global_contract_is_durable_and_workers_observe_stop_latch(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin", "indeed", "glassdoor"], max_results=1)
                self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
                active = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "r2-worker"})["task"]
                server = self.start_server(root)
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    html = response.read().decode()
                with urllib.request.urlopen(base + "/static/dashboard.js", timeout=5) as response:
                    dashboard_js = response.read().decode()
                self.assertIn("Stop all after current", html)
                self.assertIn("Resume ready platforms", html)
                self.assertIn("Emergency stop", html)
                self.assertIn("newControlRequestId", dashboard_js)
                self.assertIn("control_request_id:requestId", dashboard_js)
                self.assertNotIn("dashboard:${state.runId}:global", dashboard_js)

                status, stop = self.post(base, "/api/run/control", {"action": "stop_all", "run_id": run_id, "control_request_id": "r2-stop-1"})
                self.assertEqual(status, 200)
                self.assertEqual(stop["canonical_action"], "stop_all")
                self.assertTrue(rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "r2-worker"})["stop_after_current"])

                status, resume = self.post(base, "/api/run/control", {"action": "resume_ready_platforms", "run_id": run_id, "control_request_id": "r2-resume-1"})
                self.assertEqual(status, 200)
                self.assertEqual(resume["canonical_action"], "resume_ready_platforms")
                reactivated = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "r2-worker"})["task"]
                self.assertEqual(reactivated["task_id"], active["task_id"])
                status, emergency = self.post(base, "/api/run/control", {"action": "emergency_stop", "run_id": run_id, "control_request_id": "r2-emergency-1"})
                self.assertEqual(status, 200)
                self.assertEqual(emergency["canonical_action"], "emergency_stop")

                duplicate_status, duplicate = self.post(base, "/api/run/control", {"action": "emergency_stop", "run_id": run_id, "control_request_id": "r2-emergency-1"})
                self.assertEqual(duplicate_status, 200)
                self.assertTrue(duplicate["deduplicated"])
                conflict_status, conflict = self.post(base, "/api/run/control", {"action": "stop_all", "run_id": run_id, "control_request_id": "r2-emergency-1"})
                self.assertEqual(conflict_status, 400)
                self.assertIn("conflicts", conflict["error"])

                conn = Database(self.bundle(root)).connect()
                try:
                    rows = conn.execute("SELECT request_id,action,status FROM control_requests WHERE browser_run_id=? ORDER BY control_id", (run_id,)).fetchall()
                    self.assertEqual([(row[0], row[1]) for row in rows], [
                        ("r2-stop-1", "stop_all"), ("r2-resume-1", "resume_ready_platforms"), ("r2-emergency-1", "emergency_stop"),
                    ])
                    self.assertEqual(conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()[0], "stopped")
                    self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (active["task_id"],)).fetchone()[0], "incomplete")
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_platform_control_instances_acknowledge_independently_and_resume_preserves_challenge(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin", "indeed", "glassdoor"], max_results=1)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                tasks = {
                    platform: rpc.handle({"action": "next_task", "run_id": run_id, "platform": platform, "worker_id": f"r2-{platform}"})["task"]
                    for platform in ("linkedin", "indeed", "glassdoor")
                }
                rpc.handle({"action": "pause_platform", "run_id": run_id, "platform": "indeed", "task_id": tasks["indeed"]["task_id"], "reason": "human challenge fixture"})
                rpc.handle({"action": "worker_runtime", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed", "worker_status": "challenged", "owned_window": True, "window_id": 41, "search_tab_id": 42, "search_tab_url": "https://www.indeed.com/jobs?q=patient"})
                server = self.start_server(root)
                base = f"http://127.0.0.1:{server.server_port}"

                status, global_resume = self.post(base, "/api/run/control", {"action": "resume_ready_platforms", "run_id": run_id, "control_request_id": "r2-global-resume"})
                self.assertEqual(status, 200)
                self.assertEqual(global_resume["canonical_action"], "resume_ready_platforms")
                global_delivery = rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed"})
                self.assertEqual(global_delivery["control"]["request_id"], "r2-global-resume")
                rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed", "request_id": "r2-global-resume"})
                conn = Database(self.bundle(root)).connect()
                try:
                    challenge = conn.execute("SELECT readiness_state,challenge_reason FROM browser_platform_runs WHERE browser_run_id=? AND platform='indeed'", (run_id,)).fetchone()
                    self.assertEqual(tuple(challenge), ("challenged_cooldown", "human challenge fixture"))
                finally:
                    conn.close()

                status, focus = self.post(base, "/api/platform/control", {"run_id": run_id, "platform": "indeed", "action": "focus_window", "control_request_id": "r2-focus"})
                self.assertEqual(status, 200)
                delivery = rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed"})
                self.assertEqual(delivery["control"]["request_id"], focus["request_id"])
                acknowledged = rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed", "request_id": focus["request_id"], "result": {"focused": True}})
                self.assertEqual(acknowledged["control"]["status"], "ACKNOWLEDGED")

                first_status, first = self.post(base, "/api/platform/control", {"run_id": run_id, "platform": "indeed", "action": "recheck", "control_request_id": "r2-recheck-a"})
                second_status, second = self.post(base, "/api/platform/control", {"run_id": run_id, "platform": "indeed", "action": "recheck", "control_request_id": "r2-recheck-b"})
                self.assertEqual((first_status, second_status), (200, 200))
                self.assertNotEqual(first["request_id"], second["request_id"])
                for request_id in (first["request_id"], second["request_id"]):
                    delivered = rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed"})
                    self.assertEqual(delivered["control"]["request_id"], request_id)
                    rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "r2-indeed", "request_id": request_id, "result": {"recheck": True}})
                stale = rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "old-worker", "request_id": first["request_id"], "result": {"recheck": False}})
                self.assertTrue(stale["deduplicated"])
                conn = Database(self.bundle(root)).connect()
                try:
                    rows = conn.execute("SELECT request_id,status FROM control_requests WHERE browser_run_id=? AND platform='indeed' ORDER BY control_id", (run_id,)).fetchall()
                    self.assertEqual([(row[0], row[1]) for row in rows], [("r2-focus", "ACKNOWLEDGED"), ("r2-recheck-a", "ACKNOWLEDGED"), ("r2-recheck-b", "ACKNOWLEDGED")])
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND platform='indeed' AND status='exhausted'", (run_id,)).fetchone()[0], 0)
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous


if __name__ == "__main__":
    unittest.main()
