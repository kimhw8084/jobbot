from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from jobbot import browser_tasks
from jobbot.bridge import rpc
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.dashboard import active_run, control_platform, control_run, create_server
from jobbot.db import Database


class Chg159DashboardTests(unittest.TestCase):
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

    @contextmanager
    def server(self, root: Path):
        server = create_server(self.bundle(root), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def get(self, base: str, path: str) -> dict[str, object]:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return json.loads(response.read())

    def post(self, base: str, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def fixture_run(self, root: Path) -> tuple[int, dict[str, int]]:
        previous = rpc.BASE
        rpc.BASE = root
        run_id = browser_tasks.enqueue_validation(root, ["linkedin", "indeed", "glassdoor"], max_results=1)
        rpc.handle({"action": "begin_run", "run_id": run_id})
        tasks = {
            platform: int(rpc.handle({"action": "next_task", "run_id": run_id, "platform": platform, "worker_id": f"chg159-{platform}"})["task"]["task_id"])
            for platform in ("linkedin", "indeed", "glassdoor")
        }
        rpc.handle({"action": "worker_runtime", "run_id": run_id, "platform": "linkedin", "worker_id": "chg159-linkedin", "worker_status": "running", "owned_window": True, "window_id": 11, "search_tab_id": 12, "search_tab_url": "https://www.linkedin.com/jobs/search/?keywords=patient"})
        rpc.handle({"action": "pause_platform", "run_id": run_id, "platform": "indeed", "task_id": tasks["indeed"], "reason": "CAPTCHA verification is required"})
        rpc.handle({"action": "worker_runtime", "run_id": run_id, "platform": "indeed", "worker_id": "chg159-indeed", "worker_status": "challenged", "owned_window": True, "window_id": 21, "search_tab_id": 22, "search_tab_url": "https://www.indeed.com/jobs?q=patient", "message": "human inspection required"})
        rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": tasks["glassdoor"], "status": "exhausted", "reason": "fixture complete", "exhausted": True})
        rpc.handle({"action": "worker_runtime", "run_id": run_id, "platform": "glassdoor", "worker_id": "chg159-glassdoor", "worker_status": "terminal", "owned_window": False, "chrome_available": False})
        rpc.BASE = previous
        return run_id, tasks

    def test_waiting_for_human_is_durable_and_semantically_separate(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, tasks = self.fixture_run(root)
                conn = Database(self.bundle(root)).connect()
                try:
                    value = active_run(conn)
                    by_platform = {item["platform"]: item for item in value["platforms"]}
                    self.assertEqual(by_platform["indeed"]["interaction_state"], "WAITING_FOR_HUMAN")
                    self.assertEqual(by_platform["indeed"]["state_owner"], "You")
                    self.assertEqual(by_platform["indeed"]["human_wait_reason"], "CAPTCHA verification is required")
                    self.assertTrue(by_platform["indeed"]["checkpoint_preserved"])
                    self.assertEqual(by_platform["indeed"]["checkpoint"]["task_id"], tasks["indeed"])
                    self.assertEqual(by_platform["indeed"]["window_id"], 21)
                    self.assertEqual(by_platform["indeed"]["search_tab_id"], 22)
                    self.assertEqual(value["run_state_summary"]["label"], "1 continuing · 1 waiting for you · 1 complete")
                    self.assertEqual(by_platform["glassdoor"]["interaction_state"], "COMPLETE")
                    self.assertNotEqual(by_platform["linkedin"]["interaction_state"], "WAITING_FOR_HUMAN")
                finally:
                    conn.close()

                # A new read connection sees the same human wait; polling has no
                # control side effect and does not release the checkpoint.
                conn = Database(self.bundle(root)).connect()
                try:
                    before = conn.execute("SELECT COUNT(*) FROM control_requests").fetchone()[0]
                    reloaded = active_run(conn)
                    after = conn.execute("SELECT COUNT(*) FROM control_requests").fetchone()[0]
                    self.assertEqual(before, after)
                    self.assertEqual(reloaded["platforms"][1]["interaction_state"], "WAITING_FOR_HUMAN")
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_global_resume_and_plain_resume_cannot_clear_human_wait(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, tasks = self.fixture_run(root)
                conn = Database(self.bundle(root)).connect()
                try:
                    with self.assertRaisesRegex(ValueError, "explicit recheck"):
                        control_platform(conn, {"run_id": run_id, "platform": "indeed", "action": "resume_platform", "control_request_id": "chg159-plain-resume"})
                finally:
                    conn.close()
                with mock.patch("jobbot.dashboard.subprocess.Popen"):
                    result = control_run(conn := Database(self.bundle(root)).connect(), "resume_ready_platforms", run_id, "chg159-global-resume")
                    conn.close()
                self.assertTrue(result["launcher_required"])
                self.assertEqual(result["runnable_platforms"], ["linkedin"])
                self.assertEqual(result["runnable_task_count"], 1)
                self.assertIn("human-gated lanes remain waiting", result["message"])
                conn = Database(self.bundle(root)).connect()
                try:
                    self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (tasks["linkedin"],)).fetchone()[0], "queued")
                    row = conn.execute("SELECT interaction_state,readiness_state,challenge_reason FROM browser_platform_runs WHERE browser_run_id=? AND platform='indeed'", (run_id,)).fetchone()
                    self.assertEqual(tuple(row), ("WAITING_FOR_HUMAN", "challenged_cooldown", "CAPTCHA verification is required"))
                    self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (tasks["indeed"],)).fetchone()[0], "challenged")
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_all_human_global_resume_is_a_durable_noop_without_launcher(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, tasks = self.fixture_run(root)
                rpc.BASE = root
                for platform in ("linkedin", "glassdoor"):
                    rpc.handle({"action": "pause_platform", "run_id": run_id, "platform": platform, "task_id": tasks[platform], "reason": f"{platform} human gate"})
                conn = Database(self.bundle(root)).connect()
                before = {
                    "run": tuple(conn.execute("SELECT status,stop_requested,last_error FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()),
                    "tasks": [tuple(row) for row in conn.execute("SELECT platform,status,challenge_reason FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id", (run_id,))],
                }
                conn.close()
                with self.server(root) as server, mock.patch("jobbot.dashboard.subprocess.Popen") as popen:
                    status, result = self.post(f"http://127.0.0.1:{server.server_port}", "/api/run/control", {"action": "resume_ready_platforms", "run_id": run_id, "control_request_id": "chg159-all-human"})
                self.assertEqual(status, 200)
                self.assertFalse(result["launcher_required"])
                self.assertEqual(result["runnable_platforms"], [])
                self.assertEqual(result["runnable_task_count"], 0)
                popen.assert_not_called()
                conn = Database(self.bundle(root)).connect()
                try:
                    self.assertEqual(before["run"], tuple(conn.execute("SELECT status,stop_requested,last_error FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()))
                    self.assertEqual(before["tasks"], [tuple(row) for row in conn.execute("SELECT platform,status,challenge_reason FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id", (run_id,))])
                    self.assertEqual(conn.execute("SELECT status FROM control_requests WHERE request_id='chg159-all-human'").fetchone()[0], "ACKNOWLEDGED")
                    self.assertIsNone(rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "chg159-indeed"})["control"])
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_system_retryable_and_unverified_do_not_become_human_wait(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, tasks = self.fixture_run(root)
                rpc.BASE = root
                rpc.handle({
                    "action": "platform_readiness", "run_id": run_id, "platform": "linkedin",
                    "task_id": tasks["linkedin"], "status": "retryable", "reason": "search surface unavailable",
                })
                rpc.handle({
                    "action": "platform_readiness", "run_id": run_id, "platform": "glassdoor",
                    "status": "unverified", "reason": "surface state could not be verified",
                })
                conn = Database(self.bundle(root)).connect()
                try:
                    states = {item["platform"]: item for item in active_run(conn)["platforms"]}
                    self.assertEqual(states["linkedin"]["interaction_state"], "SYSTEM_RETRYABLE")
                    self.assertEqual(states["linkedin"]["state_owner"], "JobBot")
                    self.assertEqual(states["linkedin"]["human_wait_reason"], "")
                    self.assertEqual(states["glassdoor"]["interaction_state"], "SYSTEM_UNVERIFIED")
                    self.assertEqual(states["glassdoor"]["state_owner"], "JobBot")
                    self.assertTrue(states["linkedin"]["recovery_available"])
                    self.assertEqual(states["glassdoor"]["recovery_action"], "retry_system_state")
                finally:
                    conn.close()
                conn = Database(self.bundle(root)).connect()
                try:
                    retry = control_platform(conn, {"run_id": run_id, "platform": "linkedin", "action": "retry_system_state", "control_request_id": "chg159-system-retry"})
                    self.assertEqual(retry["action"], "retry_system_state")
                    self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (tasks["linkedin"],)).fetchone()[0], "queued")
                    self.assertEqual(conn.execute("SELECT interaction_state FROM browser_platform_runs WHERE browser_run_id=? AND platform='linkedin'", (run_id,)).fetchone()[0], "RECHECKING")
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_stopped_resume_requeues_only_safe_incomplete_work(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, tasks = self.fixture_run(root)
                rpc.BASE = root
                conn = Database(self.bundle(root)).connect()
                try:
                    conn.execute("UPDATE browser_platform_runs SET worker_status='stopped',interaction_state='STOPPED',emergency_stop=1 WHERE browser_run_id=? AND platform='linkedin'", (run_id,))
                    conn.execute("UPDATE browser_search_tasks SET status='incomplete',safety_stop_reason='manual emergency stop',last_error='stopped fixture' WHERE task_id=?", (tasks["linkedin"],))
                    terminal_id = conn.execute("""INSERT INTO browser_search_tasks(
                        browser_run_id,platform,query_text,window_days,search_url,status,created_at,
                        safety_stop_reason,exhausted
                    ) VALUES(?,?,?,?,?,?,?,?,?)""", (run_id, "linkedin", "terminal fixture", 7, "https://www.linkedin.com/jobs/search/", "incomplete", "2026-09-20T00:00:00+00:00", "test-limit reached", 0)).lastrowid
                    conn.commit()
                    result = control_platform(conn, {"run_id": run_id, "platform": "linkedin", "action": "resume_platform", "control_request_id": "chg159-stopped-resume"})
                    self.assertEqual(result["action"], "resume_platform")
                    self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (tasks["linkedin"],)).fetchone()[0], "queued")
                    self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (terminal_id,)).fetchone()[0], "incomplete")
                    self.assertEqual(conn.execute("SELECT interaction_state,emergency_stop FROM browser_platform_runs WHERE browser_run_id=? AND platform='linkedin'", (run_id,)).fetchone()[0:2], ("RECHECKING", 0))
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_states_without_recovery_contract_do_not_render_retry_button(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, _ = self.fixture_run(root)
                rpc.BASE = root
                conn = Database(self.bundle(root)).connect()
                try:
                    conn.execute("UPDATE browser_platform_runs SET worker_status='stale',interaction_state='STALE' WHERE browser_run_id=? AND platform='linkedin'", (run_id,))
                    conn.commit()
                    stale = next(item for item in active_run(conn)["platforms"] if item["platform"] == "linkedin")
                    self.assertFalse(stale["recovery_available"])
                    self.assertEqual(stale["state_owner"], "JobBot")
                finally:
                    conn.close()
                with self.server(root) as server:
                    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/static/dashboard.js", timeout=5) as response:
                        script = response.read().decode()
                self.assertIn("Re-evaluate system state", script)
                self.assertNotIn("Retry system state", script)
            finally:
                rpc.BASE = previous

    def test_recheck_instances_are_unique_and_stale_ack_is_rejected(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            try:
                run_id, _ = self.fixture_run(root)
                rpc.BASE = root
                conn = Database(self.bundle(root)).connect()
                first = control_platform(conn, {"run_id": run_id, "platform": "indeed", "action": "recheck", "control_request_id": "chg159-recheck-a"})
                second = control_platform(conn, {"run_id": run_id, "platform": "indeed", "action": "recheck", "control_request_id": "chg159-recheck-b"})
                exact = control_platform(conn, {"run_id": run_id, "platform": "indeed", "action": "recheck", "control_request_id": "chg159-recheck-b"})
                self.assertNotEqual(first["request_id"], second["request_id"])
                self.assertTrue(exact["deduplicated"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM control_requests WHERE platform='indeed' AND action='recheck'",).fetchone()[0], 2)
                conn.close()

                for request_id in ("chg159-recheck-a", "chg159-recheck-b"):
                    delivered_recheck = rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "chg159-indeed"})
                    self.assertEqual(delivered_recheck["control"]["request_id"], request_id)
                    rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "chg159-indeed", "request_id": request_id})
                current = rpc.handle({"action": "request_control", "run_id": run_id, "platform": "indeed", "control_action": "focus_window", "control_request_id": "chg159-focus"})
                self.assertTrue(current["ok"])
                delivered = rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "chg159-indeed"})
                self.assertEqual(delivered["control"]["request_id"], "chg159-focus")
                conn = Database(self.bundle(root)).connect()
                conn.execute("UPDATE browser_platform_runs SET worker_generation=worker_generation+1 WHERE browser_run_id=? AND platform='indeed'", (run_id,)); conn.commit()
                conn.close()
                stale = rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "chg159-indeed", "worker_generation": 1, "request_id": "chg159-focus", "result": {"focused": True}})
                self.assertEqual(stale["error"], "stale_control_response")
            finally:
                rpc.BASE = previous

    def test_dashboard_contract_exposes_human_wait_copy_and_safe_controls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            with self.server(root) as server:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    page = response.read().decode()
                with urllib.request.urlopen(base + "/static/dashboard.js", timeout=5) as response:
                    script = response.read().decode()
                self.assertIn("Waiting for you", page)
                self.assertIn("Resume runnable platforms", page)
                self.assertIn('role="dialog" aria-modal="true" aria-labelledby="detailTitle"', page)
                self.assertIn("I've resolved it — Recheck", script)
                self.assertIn("WAITING_FOR_HUMAN", script)
                self.assertIn("No automatic site retries", script)
                self.assertIn("control_request_id:requestId", script)
                self.assertNotIn("Recheck after clearance", script)


if __name__ == "__main__":
    unittest.main()
