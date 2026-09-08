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
                saved = rpc.handle({"action": "record_job", "run_id": run_id, "task_id": task_id, "job": {"source_job_id": "write-1", "canonical_url": "https://www.indeed.com/viewjob?jk=write-1", "title": "Patient Enrollment Specialist", "company": "Example Health", "location": "United States", "employment_type": "Full-time permanent", "description": "Healthcare enrollment. Required Qualifications: 2 years relevant experience. Full-time permanent."}})
                self.assertTrue(saved["ok"])
                conn = sqlite3.connect(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT remote_gate FROM jobs").fetchone()[0], "pass")
                self.assertEqual(conn.execute("SELECT detail_read FROM search_task_results").fetchone()[0], 1); conn.close()
                paused = rpc.handle({"action": "pause_platform", "run_id": run_id, "task_id": task_id, "platform": "indeed", "reason": "fixture challenge"})
                self.assertGreaterEqual(paused["tasks_paused"], 1)
                conn = sqlite3.connect(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='challenged'", (run_id,)).fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='deferred_by_platform'", (run_id,)).fetchone()[0], 2)
                self.assertTrue(conn.execute("SELECT cooldown_until FROM browser_platform_runs WHERE browser_run_id=?", (run_id,)).fetchone()[0]); conn.close()
            finally:
                rpc.BASE = previous

    def test_rich_card_queue_stop_resume_and_cross_platform_isolation(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_production(root, "fast")
                rpc.handle({"action": "begin_run", "run_id": run_id})
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "extension-run-test"})["task"]
                task_id = int(task["task_id"])
                for index in range(10):
                    card = {
                        "title": f"Patient Enrollment Specialist {index}", "company": "Example Health",
                        "location": "Remote — United States", "posted_text": f"{index + 1} hours ago",
                        "posted_age_days": 0, "source_job_id": f"card-{index}",
                        "url": f"https://www.linkedin.com/jobs/view/{9000 + index}/",
                    }
                    payload = {"action": "record_result", "run_id": run_id, "task_id": task_id,
                               "source_site": "linkedin", "source_job_id": card["source_job_id"],
                               "source_url": card["url"], "card": card, "posted_age_days": 0}
                    self.assertFalse(rpc.handle(payload)["duplicate"])
                    self.assertTrue(rpc.handle(payload)["duplicate"])
                for index in range(5):
                    claimed = rpc.handle({"action": "next_pending_detail", "run_id": run_id, "task_id": task_id, "worker_id": "extension-run-test"})["detail"]
                    self.assertEqual(claimed["detail_status"], "RUNNING")
                    result_id = int(claimed["result_id"])
                    rpc.handle({"action": "detail_read", "run_id": run_id, "task_id": task_id, "result_id": result_id,
                                "source_site": "linkedin", "source_job_id": claimed["source_job_id"], "source_url": claimed["source_url"]})
                    saved = rpc.handle({"action": "record_job", "run_id": run_id, "task_id": task_id, "result_id": result_id,
                        "job": {"source_job_id": claimed["source_job_id"], "canonical_url": claimed["source_url"],
                                "title": claimed["title_hint"], "company": claimed["company_hint"],
                                "location": claimed["location_hint"], "employment_type": "Full-time permanent",
                                "description": "Fully remote healthcare enrollment. Required Qualifications: 2 years relevant experience."}})
                    self.assertTrue(saved["ok"])
                self.assertTrue(rpc.handle({
                    "action": "task_progress", "run_id": run_id, "task_id": task_id,
                    "results_seen": 10, "pages_visited": 1,
                    "checkpoint": {"search_url": "https://www.linkedin.com/jobs/search/", "page_number": 1,
                                    "card_stats": {"extracted_cards": 10, "persistence_attempted": 10,
                                                   "persistence_succeeded": 10, "persistence_failed": 0,
                                                   "duplicate_cards": 0, "pending_details": 5, "details_failed": 0}},
                })["ok"])
                self.assertEqual(browser_tasks.request_stop(root, run_id), 0)
                self.assertTrue(rpc.handle({"action": "should_stop", "run_id": run_id})["stop"])
                rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": task_id,
                            "status": "stopped", "reason": "stop requested"})
                browser_tasks.resume_run(root, run_id)
                db = root / "data" / "jobs.sqlite3"; conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE browser_run_id=?", (run_id,)).fetchone()[0], 10)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE browser_run_id=? AND detail_status='COMPLETE'", (run_id,)).fetchone()[0], 5)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE browser_run_id=? AND detail_status='PENDING'", (run_id,)).fetchone()[0], 5)
                rich = conn.execute("SELECT title_hint,company_hint,card_json FROM search_task_results WHERE browser_run_id=? LIMIT 1", (run_id,)).fetchone()
                self.assertTrue(rich["title_hint"]); self.assertEqual(rich["company_hint"], "Example Health"); self.assertIn("posted_text", rich["card_json"])
                counters = conn.execute("SELECT cards_extracted,cards_persistence_succeeded,pending_details FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()
                self.assertEqual(tuple(counters), (10, 10, 5))
                conn.close()

                # A challenge on the current LinkedIn task defers untouched LinkedIn
                # work but leaves Indeed available to the same run.
                rpc.handle({"action": "begin_run", "run_id": run_id})
                current = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "second-worker"})["task"]
                rpc.handle({"action": "pause_platform", "run_id": run_id, "task_id": current["task_id"], "platform": "linkedin", "reason": "fixture challenge"})
                next_task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "second-worker"})["task"]
                self.assertEqual(next_task["platform"], "indeed")
                rpc.handle({"action": "platform_auth_result", "run_id": run_id, "task_id": next_task["task_id"],
                            "platform": "indeed", "authenticated": False, "reason": "fixture auth required"})
                glassdoor_task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "second-worker"})["task"]
                self.assertEqual(glassdoor_task["platform"], "glassdoor")
                rpc.handle({"action": "platform_auth_result", "run_id": run_id, "task_id": glassdoor_task["task_id"],
                            "platform": "glassdoor", "authenticated": False, "reason": "fixture auth required"})
                self.assertTrue(rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "second-worker"})["done"])
                self.assertEqual(rpc.handle({"action": "finish_run", "run_id": run_id})["status"], "partial")
                conn = sqlite3.connect(db)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND platform='linkedin' AND status='deferred_by_platform'", (run_id,)).fetchone()[0], 0)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND platform='indeed' AND status='deferred_by_platform'", (run_id,)).fetchone()[0], 0)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND platform='glassdoor' AND status='deferred_by_platform'", (run_id,)).fetchone()[0], 0)
                conn.close()
            finally:
                rpc.BASE = previous

    def test_resume_does_not_repeat_completed_acceptance_cap(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "linkedin", 7, 20)
                db = root / "data" / "jobs.sqlite3"
                conn = sqlite3.connect(db)
                tasks = conn.execute(
                    "SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id",
                    (run_id,),
                ).fetchall()
                conn.execute(
                    "UPDATE browser_search_tasks SET status='incomplete',safety_stop_reason='Acceptance limit reached (20); production has no count limit' WHERE task_id=?",
                    (tasks[0][0],),
                )
                conn.execute(
                    "UPDATE browser_search_tasks SET status='incomplete',safety_stop_reason='manual_emergency_stop' WHERE task_id=?",
                    (tasks[1][0],),
                )
                conn.execute(
                    "UPDATE browser_runs SET status='stopped',stop_requested=1,tasks_incomplete=2 WHERE browser_run_id=?",
                    (run_id,),
                )
                conn.commit()
                conn.close()
                browser_tasks.resume_run(root, run_id)
                conn = sqlite3.connect(db)
                try:
                    states = [
                        row[0] for row in conn.execute(
                            "SELECT status FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id",
                            (run_id,),
                        )
                    ]
                    self.assertEqual(states, ["incomplete", "queued", "queued"])
                finally:
                    conn.close()
                status = rpc.handle({"action": "run_status", "run_id": run_id})
                self.assertEqual(status["run"]["tasks_incomplete"], 1)
                self.assertEqual(status["platforms"][0]["tasks_incomplete"], 1)
            finally:
                rpc.BASE = previous


if __name__ == "__main__": unittest.main()
