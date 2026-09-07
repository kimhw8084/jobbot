from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot import browser_tasks
from jobbot.bridge import rpc
from jobbot.config import PROJECT_ROOT


class BrowserTaskIntegrationTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        (root / "data").mkdir(); (root / "out").mkdir()
        return root

    def test_query_persistence_crash_resume_and_multi_query_progression(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            run_id = browser_tasks.enqueue_gate(root, "indeed", 7, 20)
            db = root / "data" / "jobs.sqlite3"
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            first = conn.execute("SELECT * FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id", (run_id,)).fetchall()
            self.assertEqual(len(first), 3)
            task_id = int(first[0]["task_id"])
            checkpoint = {"search_url": "https://www.indeed.com/jobs?q=x&start=20", "page_number": 3, "last_job_key": "abc"}
            conn.execute("UPDATE browser_search_tasks SET status='running',checkpoint_json=?,page_number=3,lease_owner='dead',lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?", (json.dumps(checkpoint), task_id))
            conn.execute("UPDATE browser_runs SET status='running' WHERE browser_run_id=?", (run_id,)); conn.commit(); conn.close()
            self.assertEqual(browser_tasks.resume_run(root, run_id), run_id)
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            resumed = conn.execute("SELECT status,checkpoint_json FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()
            self.assertEqual(resumed["status"], "queued"); self.assertEqual(json.loads(resumed["checkpoint_json"]), checkpoint)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=?", (run_id,)).fetchone()[0], 3)
            conn.close()

    def test_write_through_and_challenge_isolation(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "indeed", 7, 20)
                self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "test"})["task"]
                task_id = int(task["task_id"])
                result = {"action": "record_result", "run_id": run_id, "task_id": task_id, "source_site": "indeed", "source_job_id": "write-1", "source_url": "https://www.indeed.com/viewjob?jk=write-1"}
                self.assertFalse(rpc.handle(result)["duplicate"])
                db = root / "data" / "jobs.sqlite3"; conn = sqlite3.connect(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results").fetchone()[0], 1); conn.close()
                rpc.handle({"action": "detail_read", "run_id": run_id, "task_id": task_id, "source_site": "indeed", "source_job_id": "write-1", "source_url": "https://www.indeed.com/viewjob?jk=write-1"})
                saved = rpc.handle({"action": "record_job", "run_id": run_id, "task_id": task_id, "job": {"source_job_id": "write-1", "canonical_url": "https://www.indeed.com/viewjob?jk=write-1", "title": "Patient Enrollment Specialist", "company": "Example Health", "location": "Remote — United States", "remote_status": "remote", "employment_type": "Full-time permanent", "description": "Fully remote healthcare enrollment. Required Qualifications: 2 years relevant experience. Full-time permanent."}})
                self.assertTrue(saved["ok"])
                conn = sqlite3.connect(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT detail_read FROM search_task_results").fetchone()[0], 1); conn.close()
                paused = rpc.handle({"action": "pause_platform", "run_id": run_id, "platform": "indeed", "reason": "fixture challenge"})
                self.assertGreaterEqual(paused["tasks_paused"], 1)
                conn = sqlite3.connect(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='challenged'", (run_id,)).fetchone()[0], 3)
                self.assertTrue(conn.execute("SELECT cooldown_until FROM browser_platform_runs WHERE browser_run_id=?", (run_id,)).fetchone()[0]); conn.close()
            finally:
                rpc.BASE = previous


if __name__ == "__main__": unittest.main()
