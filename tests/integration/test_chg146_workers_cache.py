from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot import browser_tasks, crawl_observations
from jobbot.bridge import rpc
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.dashboard import active_run, control_platform
from jobbot.diagnostics import instrumentation
from jobbot.db import Database


class Chg146WorkersCacheTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        (root / "data").mkdir(); (root / "out").mkdir()
        return root

    def bundle(self, root: Path) -> ConfigBundle:
        original = load_bundle(root)
        runtime = copy.deepcopy(original.runtime)
        runtime["runtime"]["database_path"] = str(root / "data" / "jobs.sqlite3")
        runtime["runtime"]["output_dir"] = str(root / "out")
        runtime["runtime"]["crawl_observations_path"] = str(root / "data" / "crawl_observations.sqlite3")
        return ConfigBundle(root, original.strategy, original.candidate, runtime)

    def test_one_serial_lease_per_platform_and_replay_safe_controls(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin", "indeed", "glassdoor"], max_results=1)
                self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
                leased = {}
                for platform in ("linkedin", "indeed", "glassdoor"):
                    response = rpc.handle({"action": "next_task", "run_id": run_id, "platform": platform, "worker_id": f"worker-{platform}"})
                    self.assertTrue(response.get("task"), platform)
                    leased[platform] = int(response["task"]["task_id"])
                duplicate = rpc.handle({"action": "next_task", "run_id": run_id, "platform": "linkedin", "worker_id": "second-linkedin-worker"})
                self.assertTrue(duplicate["busy"])
                self.assertEqual(duplicate["active_task_id"], leased["linkedin"])
                for platform, task_id in leased.items():
                    self.assertTrue(rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": task_id, "status": "exhausted", "reason": "CHG-146 lease fixture", "exhausted": True})["ok"])

                request = {"action": "request_control", "run_id": run_id, "platform": "indeed", "control_action": "stop_after_current", "control_request_id": "control-146"}
                first = rpc.handle(request); second = rpc.handle(request)
                self.assertFalse(first["deduplicated"]); self.assertTrue(second["deduplicated"])
                delivery = rpc.handle({"action": "consume_control", "run_id": run_id, "platform": "indeed", "worker_id": "worker-indeed"})
                self.assertEqual(delivery["control"]["request_id"], "control-146")
                ack = rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "worker_id": "worker-indeed", "request_id": "control-146", "status": "ACKNOWLEDGED", "result": {"applied": True}})
                self.assertEqual(ack["control"]["status"], "ACKNOWLEDGED")
                self.assertTrue(rpc.handle({"action": "ack_control", "run_id": run_id, "platform": "indeed", "request_id": "control-146"})["deduplicated"])

                second_run = browser_tasks.enqueue_validation(root, ["indeed"], max_results=1)
                conflict = rpc.handle({**request, "run_id": second_run})
                self.assertEqual(conflict["error"], "control_request_conflict")
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                rows = conn.execute("SELECT platform,COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? GROUP BY platform", (run_id,)).fetchall()
                self.assertEqual({platform for platform, _ in rows}, {"linkedin", "indeed", "glassdoor"})
                conn.close()
            finally:
                rpc.BASE = previous

    def test_instrumentation_compares_pane_candidate_to_legacy_detail_tabs(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin", "indeed", "glassdoor"], max_results=1)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                for platform in ("linkedin", "indeed", "glassdoor"):
                    task = rpc.handle({"action": "next_task", "run_id": run_id, "platform": platform, "worker_id": f"instrument-{platform}"})["task"]
                    rpc.handle({"action": "browser_event", "run_id": run_id, "task_id": task["task_id"], "event_type": "worker_window_created", "message": "fixture", "tab_count": 1})
                    rpc.handle({"action": "browser_event", "run_id": run_id, "task_id": task["task_id"], "event_type": "navigation", "message": "fixture search"})
                    rpc.handle({"action": "browser_event", "run_id": run_id, "task_id": task["task_id"], "event_type": "pane_selection", "message": "fixture pane"})
                    rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": task["task_id"], "status": "exhausted", "reason": "fixture", "exhausted": True})
                conn = Database(self.bundle(root)).connect()
                try:
                    metrics = instrumentation(conn, run_id)
                finally:
                    conn.close()
                self.assertEqual(metrics["candidate"]["detail_page_navigations"], 0)
                self.assertEqual(metrics["candidate"]["detail_pane_selections"], 3)
                self.assertEqual(metrics["candidate"]["windows_created"], 3)
                self.assertTrue(metrics["comparison"]["one_search_tab_per_platform"])
                self.assertEqual(metrics["current_main_model"]["detail_page_navigations"], 3)
            finally:
                rpc.BASE = previous

    def test_observation_cache_is_optional_safe_and_never_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); bundle = self.bundle(root); Database(bundle).migrate()
            card = {"posted_text": "1 day ago", "posted_age_days": 1}
            job = {"source_job_id": "cache-1", "canonical_url": "https://www.indeed.com/viewjob?jk=cache-1", "title": "Patient Access Specialist", "company": "Access Co", "location": "Remote — United States", "description": "Substantive detail evidence for patient access, documentation accuracy, privacy-safe communication, insurance verification, and healthcare operations. " * 4}
            evidence = {"detail_acquisition": {"mode": "search_pane", "surface": "embedded_search_pane"}}
            self.assertTrue(crawl_observations.publish(bundle, platform="indeed", source_job_id="cache-1", source_url=job["canonical_url"], card=card, title=job["title"], company=job["company"], location=job["location"], job=job, evidence=evidence, source_build="build-146"))
            current_hash = crawl_observations.card_hash(card, source_job_id="cache-1", source_url=job["canonical_url"], title=job["title"], company=job["company"], location=job["location"])
            cached = crawl_observations.lookup(bundle, platform="indeed", source_job_id="cache-1", source_url=job["canonical_url"], current_card_hash=current_hash, source_build="build-146")
            self.assertEqual(cached["detail_acquisition"]["mode"], "cache")
            self.assertEqual(cached["provenance"], "crawl_observation_cache")
            changed_hash = crawl_observations.card_hash({**card, "posted_text": "2 days ago"}, source_job_id="cache-1", source_url=job["canonical_url"], title=job["title"], company=job["company"], location=job["location"])
            self.assertIsNone(crawl_observations.lookup(bundle, platform="indeed", source_job_id="cache-1", source_url=job["canonical_url"], current_card_hash=changed_hash, source_build="build-146"))
            self.assertFalse(crawl_observations.publish(bundle, platform="indeed", source_job_id="unsafe", source_url="https://www.indeed.com/viewjob?jk=unsafe", card={"cookie": "never"}, title="Unsafe", company="", location="", job=job, evidence=evidence, source_build="build-146"))
            self.assertEqual(crawl_observations.stats(bundle)["complete"], 1)

    def test_dashboard_exposes_platform_attention_and_persists_control(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td); rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_validation(root, ["linkedin", "indeed", "glassdoor"], max_results=1)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                rpc.handle({"action": "platform_readiness", "run_id": run_id, "platform": "indeed", "status": "challenged_cooldown", "reason": "fixture challenge"})
                conn = Database(self.bundle(root)).connect()
                try:
                    result = active_run(conn)
                    self.assertEqual({row["platform"] for row in result["platforms"]}, {"linkedin", "indeed", "glassdoor"})
                    self.assertEqual(result["attention"][0]["platform"], "indeed")
                    self.assertTrue(result["attention"][0]["checkpoint_preserved"])
                    control = control_platform(conn, {"run_id": run_id, "platform": "indeed", "action": "recheck", "control_request_id": "dashboard-recheck-146"})
                    self.assertEqual(control["status"], "PENDING")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM control_requests WHERE request_id='dashboard-recheck-146'").fetchone()[0], 1)
                finally:
                    conn.close()
            finally:
                rpc.BASE = previous


if __name__ == "__main__":
    unittest.main()
