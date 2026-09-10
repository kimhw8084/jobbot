from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from jobbot import browser_tasks
from jobbot.bridge import rpc
from jobbot.config import PROJECT_ROOT


class RpcIdempotencyTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        (root / "data").mkdir(); (root / "out").mkdir()
        return root

    def test_duplicate_mutations_replay_across_reopen_and_conflict(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin"])
                begin = {"action": "begin_run", "run_id": run_id, "request_id": "begin-1"}
                first = rpc.handle(begin); second = rpc.handle(begin)
                self.assertTrue(first["ok"]); self.assertTrue(second["replayed"])
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "idempotent", "request_id": "claim-1"})["task"]
                task_again = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "idempotent", "request_id": "claim-1"})
                self.assertTrue(task_again["replayed"]); self.assertEqual(task_again["task"]["task_id"], task["task_id"])
                card = {"action": "record_result", "request_id": "result-1", "run_id": run_id, "task_id": task["task_id"],
                        "source_site": "linkedin", "source_job_id": "rpc-card", "source_url": "https://www.linkedin.com/jobs/view/123456"}
                saved = rpc.handle(card); replay = rpc.handle(card)
                self.assertTrue(saved["ok"]); self.assertTrue(replay["replayed"])
                collision = rpc.handle({**card, "source_job_id": "different"})
                self.assertEqual(collision["error"], "idempotency_collision")
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rpc_receipts").fetchone()[0], 3)
                receipt_text = " ".join(row[0] + row[1] for row in conn.execute("SELECT payload_hash,response_json FROM rpc_receipts"))
                self.assertNotIn("bridge-token-secret", receipt_text)
                conn.close()
                self.assertTrue(rpc.handle(begin)["replayed"])
            finally:
                rpc.BASE = previous

    def test_concurrent_identical_delivery_executes_one_logical_mutation(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin"])
                request = {"action": "begin_run", "run_id": run_id, "request_id": "concurrent-begin"}
                values: list[dict[str, object]] = []
                def invoke() -> None:
                    values.append(rpc.handle(request))
                threads = [threading.Thread(target=invoke) for _ in range(2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=15)
                self.assertEqual(len(values), 2)
                self.assertEqual(sum(bool(value.get("replayed")) for value in values), 1)
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_events WHERE event_type='run_started'").fetchone()[0], 1)
                conn.close()
            finally:
                rpc.BASE = previous

    def test_task_active_time_replay_and_yield_backfill_are_once_only(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin"])
                rpc.handle({"action": "begin_run", "run_id": run_id, "request_id": "yield-begin"})
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "yield", "request_id": "yield-claim"})["task"]
                task_id = int(task["task_id"])
                rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": task_id, "status": "exhausted", "exhausted": True, "request_id": "yield-complete"})
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                self.assertIsNone(conn.execute("SELECT yield_recorded_at FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()[0])
                conn.close()
                timing = {"action": "browser_event", "run_id": run_id, "task_id": task_id, "event_type": "task_active_time", "message": "final", "payload": {"metric_scope": "task_attempt", "task_active_browser_ms": 7777}, "request_id": "yield-time"}
                rpc.handle(timing); rpc.handle(timing)
                rpc.handle({"action": "finish_run", "run_id": run_id, "request_id": "yield-finish"})
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                self.assertEqual(conn.execute("SELECT task_active_browser_ms FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()[0], 7777)
                self.assertEqual(conn.execute("SELECT COUNT(*),MAX(task_active_browser_ms) FROM query_yield_stats").fetchone(), (1, 7777))
                self.assertFalse(conn.execute("SELECT yield_recorded_at IS NULL FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()[0])
                conn.close()
            finally:
                rpc.BASE = previous


if __name__ == "__main__":
    unittest.main()
