from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot import browser_tasks, dashboard
from jobbot.bridge import rpc
from jobbot.config import PROJECT_ROOT
from jobbot.db import Database
from jobbot.migrations import all_migrations
from tests.helpers import bundle_with_database


class Chg112ProductionIntegrityTests(unittest.TestCase):
    def make_root(self, td: str) -> Path:
        root = Path(td)
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        (root / "data").mkdir()
        (root / "out").mkdir()
        return root

    def test_identity_content_and_evidence_states_are_separate(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "linkedin", 7, 20)
                self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "chg112"})["task"]
                task_id = int(task["task_id"])
                first = rpc.handle({
                    "action": "record_result", "run_id": run_id, "task_id": task_id,
                    "source_site": "linkedin", "source_job_id": "identity-only",
                    "source_url": "https://www.linkedin.com/jobs/view/4467541678/",
                    "card": {"title": "CRM Senior Specialist - Remote Work"},
                })
                self.assertFalse(first["duplicate"])
                result_id = int(first["result_id"])
                title_only = rpc.handle({
                    "action": "record_job", "run_id": run_id, "task_id": task_id, "result_id": result_id,
                    "detail_evidence": {"page_type": "job", "page_url": "https://www.linkedin.com/jobs/view/4467541678/"},
                    "job": {"source_job_id": "identity-only", "canonical_url": "https://www.linkedin.com/jobs/view/4467541678/", "title": "CRM Senior Specialist - Remote Work"},
                })
                self.assertFalse(title_only["ok"])
                self.assertEqual(title_only["error"], "content_incomplete")

                error_result = rpc.handle({
                    "action": "record_result", "run_id": run_id, "task_id": task_id,
                    "source_site": "linkedin", "source_job_id": "tunnel-error",
                    "source_url": "https://www.linkedin.com/jobs/view/4467541679/",
                    "card": {"title": "CRM Senior Specialist - Remote Work"},
                })
                unsafe = rpc.handle({
                    "action": "record_job", "run_id": run_id, "task_id": task_id,
                    "result_id": int(error_result["result_id"]),
                    "detail_evidence": {"page_type": "error", "surface_reason": "Tunnel Connection Failed", "page_url": "https://www.linkedin.com/jobs/view/4467541679/"},
                    "job": {"source_job_id": "tunnel-error", "canonical_url": "https://www.linkedin.com/jobs/view/4467541679/", "title": "Tunnel Connection Failed", "company": "", "description": "Should never be persisted."},
                })
                self.assertEqual(unsafe["error"], "unsafe_detail_surface")

                description = (
                    "Fully remote healthcare operations work. Required Qualifications: two years of experience. "
                    "The specialist documents patient access workflows, resolves enrollment issues, protects privacy, "
                    "and collaborates with clinical and operations teams. "
                ) * 4
                enriched = rpc.handle({
                    "action": "record_job", "run_id": run_id, "task_id": task_id, "result_id": result_id,
                    "detail_evidence": {"page_type": "job", "page_url": "https://www.linkedin.com/jobs/view/4467541678/"},
                    "job": {
                        "source_job_id": "identity-only", "canonical_url": "https://www.linkedin.com/jobs/view/4467541678/",
                        "title": "CRM Senior Specialist - Remote Work", "company": "BairesDev", "location": "",
                        "remote_status": "remote", "apply_url": "https://www.linkedin.com/jobs/view/4467541678/",
                    "description": description.replace("Fully remote", "Healthcare"),
                    },
                })
                self.assertTrue(enriched["ok"])
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                conn.row_factory = sqlite3.Row
                job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (enriched["job_id"],)).fetchone()
                result = conn.execute("SELECT * FROM search_task_results WHERE result_id=?", (result_id,)).fetchone()
                error_row = conn.execute("SELECT * FROM search_task_results WHERE result_id=?", (error_result["result_id"],)).fetchone()
                self.assertEqual(result["identity_status"], "PERSISTED")
                self.assertEqual(result["content_state"], "COMPLETE")
                self.assertEqual(result["detail_status"], "COMPLETE")
                self.assertEqual(job["content_state"], "COMPLETE")
                self.assertEqual(job["enrichment_status"], "ENRICHED")
                self.assertEqual(job["location_raw"], "")
                self.assertEqual(job["location_evidence_state"], "UNKNOWN")
                self.assertEqual(job["remote_evidence_state"], "UNKNOWN")
                self.assertEqual(job["remote_gate"], "review")
                self.assertEqual(job["apply_url"], "")
                self.assertEqual(job["apply_destination_state"], "UNKNOWN")
                self.assertEqual(error_row["detail_status"], "EXTERNAL_BLOCKED")
                self.assertIsNone(error_row["canonical_job_id"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (enriched["job_id"],)).fetchone()[0], 1)
                metrics = dashboard.summary(conn)
                self.assertEqual(metrics["identity_captured"], 2)
                self.assertEqual(metrics["descriptions_complete"], 1)
                self.assertEqual(metrics["remote_confirmed"], 0)
                self.assertEqual(metrics["observed_application_destinations"], 0)
                conn.close()
            finally:
                rpc.BASE = previous

    def test_recall_first_persists_negatives_and_all_mode_releases_them(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "linkedin", 7, 20)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "recall"})["task"]
                positive = rpc.handle({
                    "action": "record_result", "run_id": run_id, "task_id": task["task_id"], "source_site": "linkedin",
                    "source_job_id": "positive", "source_url": "https://www.linkedin.com/jobs/view/7001/",
                    "card": {"title": "Patient Enrollment Specialist"},
                })
                negative = rpc.handle({
                    "action": "record_result", "run_id": run_id, "task_id": task["task_id"], "source_site": "linkedin",
                    "source_job_id": "negative", "source_url": "https://www.linkedin.com/jobs/view/7002/",
                    "card": {"title": "Software Engineer"},
                })
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                conn.row_factory = sqlite3.Row
                negative_row = conn.execute("SELECT * FROM search_task_results WHERE result_id=?", (negative["result_id"],)).fetchone()
                self.assertEqual(negative_row["identity_status"], "PERSISTED")
                self.assertEqual(negative_row["detail_status"], "DEFERRED_RECALL")
                self.assertEqual(negative_row["recall_selected"], 0)
                self.assertIn(negative_row["enrichment_priority"], (1, 2))
                conn.close()
                claimed = rpc.handle({"action": "next_pending_detail", "run_id": run_id, "task_id": task["task_id"], "worker_id": "recall"})
                self.assertEqual(claimed["detail"]["result_id"], positive["result_id"])
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                conn.execute("UPDATE browser_runs SET enrichment_mode='all' WHERE browser_run_id=?", (run_id,))
                conn.commit(); conn.close()
                released = rpc.handle({"action": "next_pending_detail", "run_id": run_id, "task_id": task["task_id"], "worker_id": "recall-second"})
                self.assertEqual(released["detail"]["result_id"], negative["result_id"])
            finally:
                rpc.BASE = previous

    def test_stop_after_current_blocks_acquisition_and_resume_requeues_work(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "linkedin", 7, 20)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                active = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "stop"})["task"]
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                self.assertEqual(dashboard.control_run(conn, "stop", run_id)["action"], "stop")
                conn.close()
                blocked = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "stop"})
                self.assertTrue(blocked["stop_after_current"])
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='queued'", (run_id,)).fetchone()[0], 2)
                conn.close()
                rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": active["task_id"], "status": "stopped", "reason": "stop after current"})
                browser_tasks.resume_run(root, run_id)
                resumed = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "stop"})
                self.assertTrue(resumed.get("task"))
                self.assertEqual(resumed["task"]["status"], "running")
            finally:
                rpc.BASE = previous

    def test_user_invoked_re_enrichment_requeues_partial_content_without_history_loss(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "linkedin", 7, 20)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "re-enrich"})["task"]
                discovery = rpc.handle({
                    "action": "record_result", "run_id": run_id, "task_id": task["task_id"], "source_site": "linkedin",
                    "source_job_id": "partial", "source_url": "https://www.linkedin.com/jobs/view/7301/",
                    "card": {"title": "Patient Access Specialist"},
                })
                saved = rpc.handle({
                    "action": "record_job", "run_id": run_id, "task_id": task["task_id"], "result_id": discovery["result_id"],
                    "detail_evidence": {"page_type": "job", "page_url": "https://www.linkedin.com/jobs/view/7301/"},
                    "job": {"source_job_id": "partial", "canonical_url": "https://www.linkedin.com/jobs/view/7301/", "title": "Patient Access Specialist", "description": "Brief but valid detail text."},
                })
                self.assertEqual(saved["content_state"], "PARTIAL")
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                before_versions = conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (saved["job_id"],)).fetchone()[0]
                conn.close()
                counts = browser_tasks.requeue_missing_enrichment(root, run_id)
                self.assertEqual(counts, {"discoveries": 1, "tasks": 1, "runs": 1})
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                row = conn.execute("SELECT detail_status,content_state,detail_error FROM search_task_results WHERE result_id=?", (discovery["result_id"],)).fetchone()
                run = conn.execute("SELECT status,enrichment_mode FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
                self.assertEqual(tuple(row), ("RETRYABLE", "PARTIAL", "user-requested re-enrichment"))
                self.assertEqual(tuple(run), ("queued", "all"))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (saved["job_id"],)).fetchone()[0], before_versions)
                conn.close()
            finally:
                rpc.BASE = previous

    def test_requested_and_observed_search_urls_preserve_context_loss(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                run_id = browser_tasks.enqueue_gate(root, "linkedin", 7, 20)
                rpc.handle({"action": "begin_run", "run_id": run_id})
                task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": "context"})["task"]
                requested = task["search_url"]
                observed = "https://www.linkedin.com/jobs/search/"
                self.assertNotEqual(requested, observed)
                self.assertTrue(rpc.handle({
                    "action": "task_progress", "run_id": run_id, "task_id": task["task_id"],
                    "checkpoint": {"requested_search_url": requested, "observed_page_url": observed, "context_status": "query_context_lost", "search_url": observed},
                })["ok"])
                conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                row = conn.execute("SELECT requested_search_url,observed_page_url,page_context_status,status,exhausted FROM browser_search_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
                self.assertEqual(tuple(row[:3]), (requested, observed, "query_context_lost"))
                self.assertNotEqual(row[3], "exhausted")
                self.assertEqual(row[4], 0)
                conn.close()
            finally:
                rpc.BASE = previous

    def test_platform_auth_and_challenge_recovery_isolated_and_durable(self) -> None:
        previous = rpc.BASE
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            rpc.BASE = root
            try:
                for platform in ("indeed", "glassdoor"):
                    run_id = browser_tasks.enqueue_gate(root, platform, 7, 20)
                    rpc.handle({"action": "begin_run", "run_id": run_id})
                    task = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": f"{platform}-auth"})["task"]
                    rpc.handle({"action": "platform_auth_result", "run_id": run_id, "task_id": task["task_id"], "platform": platform, "authenticated": False, "reason": f"{platform} sign-in required"})
                    conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                    state = conn.execute("SELECT auth_status,readiness_state FROM browser_platform_runs WHERE browser_run_id=? AND platform=?", (run_id, platform)).fetchone()
                    self.assertEqual(tuple(state), ("not_authenticated", "sign_in_required"))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='deferred_by_platform'", (run_id,)).fetchone()[0], 2)
                    dashboard.control_run(conn, "resume", run_id)
                    conn.close()
                    retry = rpc.handle({"action": "next_task", "run_id": run_id, "worker_id": f"{platform}-auth"})["task"]
                    self.assertEqual(retry["platform"], platform)
                    rpc.handle({"action": "platform_auth_result", "run_id": run_id, "task_id": retry["task_id"], "platform": platform, "authenticated": True, "reason": f"{platform} session authenticated"})
                    conn = sqlite3.connect(root / "data" / "jobs.sqlite3")
                    state = conn.execute("SELECT auth_status,readiness_state FROM browser_platform_runs WHERE browser_run_id=? AND platform=?", (run_id, platform)).fetchone()
                    self.assertEqual(tuple(state), ("verified", "verified"))
                    conn.close()
            finally:
                rpc.BASE = previous

    def test_migration_requeues_historical_identity_only_rows_without_losing_versions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            path = root / "data" / "jobs.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            migrations = all_migrations()
            for migration in migrations[:-1]:
                migration.upgrade(conn)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (migration.VERSION, migration.NAME, "2026-09-17T00:00:00+00:00"))
            now = "2026-09-17T00:00:00+00:00"
            conn.execute("INSERT INTO jobs(job_id,title,company,location_raw,canonical_url,apply_url,remote_status,description,first_seen,last_seen,source_verification,canonical_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy-1", "Legacy title", "Legacy Co", "Remote", "https://www.linkedin.com/jobs/view/legacy-1/", "https://www.linkedin.com/jobs/view/legacy-1/", "remote", "", now, now, "assisted_board", 0))
            conn.execute("INSERT INTO job_versions(job_id,version_no,observed_at,source_site,occurrence_key,content_hash,snapshot_json,diff_json,reason) VALUES(?,?,?,?,?,?,?,?,?)", ("legacy-1", 1, now, "linkedin", "linkedin|legacy-1", "hash", "{}", "{}", "historical identity"))
            run_id = int(conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)", ("3.2.1", "acceptance", "linkedin", "completed", now)).lastrowid)
            task_id = int(conn.execute("INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at) VALUES(?,?,?,?,?,?,?)", (run_id, "linkedin", "legacy", 7, "https://www.linkedin.com/jobs/search/", "exhausted", now)).lastrowid)
            conn.execute("INSERT INTO search_task_results(task_id,browser_run_id,source_site,source_job_id,source_url,canonical_job_id,first_seen_at,last_seen_at,detail_read,detail_status) VALUES(?,?,?,?,?,?,?,?,?,?)", (task_id, run_id, "linkedin", "legacy-1", "https://www.linkedin.com/jobs/view/legacy-1/", "legacy-1", now, now, 1, "COMPLETE"))
            conn.commit(); conn.close()
            Database(bundle_with_database(path)).migrate()
            conn = sqlite3.connect(path)
            result = conn.execute("SELECT detail_status,content_state,detail_error FROM search_task_results").fetchone()
            job = conn.execute("SELECT location_raw,remote_status,remote_gate,apply_url,remote_evidence_state FROM jobs WHERE job_id='legacy-1'").fetchone()
            self.assertEqual(result[0], "RETRYABLE")
            self.assertEqual(result[1], "MISSING")
            self.assertIn("re-enrichment", result[2])
            self.assertEqual(tuple(job), ("", "unknown", "review", "", "UNKNOWN"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id='legacy-1'").fetchone()[0], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
