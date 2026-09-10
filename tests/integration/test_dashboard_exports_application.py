from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from jobbot.application import add_note, history, mark
from jobbot.dashboard import create_server, live_discoveries
from jobbot.db import Database
from jobbot.exports import export_all
from jobbot.funnel import analyze

from tests.helpers import bundle_with_database


class DashboardExportApplicationTests(unittest.TestCase):
    def mutation_headers(self, server) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "X-JobBot-CSRF": server.csrf_token,
        }

    def post_status(self, url: str, payload: dict[str, object], headers: dict[str, str]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def seed(self, conn: sqlite3.Connection, count: int = 2100) -> None:
        rows = []
        for index in range(count):
            rows.append((
                f"J{index:06d}", "Patient Enrollment Specialist", f"Employer {index % 100}",
                "Remote — United States", f"https://example.test/jobs/{index}", f"https://example.test/jobs/{index}",
                "remote", "Full-time permanent", "$60,000", "2026-09-01", "Complete healthcare enrollment description. " * 10,
                "HEALTHCARE_OPS_ACCESS", "enrollment_operations", "pass", 90.0, 85.0, 82.0, 72.0, 79.0,
                "APPLY_NOW", "2026-09-01T00:00:00+00:00", "2026-09-07T00:00:00+00:00", "NEW", "active", 1,
            ))
        conn.executemany("""INSERT INTO jobs(job_id,title,company,location_raw,canonical_url,apply_url,remote_status,employment_type,
          salary_text,posted_at,description,career_lane,resume_variant,remote_gate,relevance_score,qualification_score,landing_score,career_score,door_score,
          recommendation,first_seen,last_seen,application_status,posting_status,is_active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.commit()

    def test_dashboard_server_side_pagination_detail_and_status_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "jobs.sqlite3", root / "out"); Database(bundle).migrate()
            conn = Database(bundle).connect(); self.seed(conn); conn.close()
            server = create_server(bundle, port=0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/api/jobs?page=2&page_size=50", timeout=5) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["total"], 2100); self.assertEqual(len(payload["jobs"]), 50); self.assertEqual(payload["page"], 2)
                with urllib.request.urlopen(base + "/api/coverage", timeout=5) as response:
                    coverage = json.loads(response.read())
                self.assertEqual(set(coverage["primary"]), {"linkedin", "indeed", "glassdoor"})
                self.assertIn("supplemental", coverage)
                job_id = payload["jobs"][0]["job_id"]
                request = urllib.request.Request(base + f"/api/jobs/{job_id}/application", data=json.dumps({"status": "APPLIED", "notes": "dashboard test"}).encode(), headers=self.mutation_headers(server), method="POST")
                with urllib.request.urlopen(request, timeout=5) as response: self.assertTrue(json.loads(response.read())["ok"])
                conn = Database(bundle).connect()
                self.assertEqual(conn.execute("SELECT application_status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "APPLIED")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM application_events WHERE job_id=?", (job_id,)).fetchone()[0], 1); conn.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_dashboard_mutations_require_exact_origin_host_json_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "jobs.sqlite3", root / "out"); Database(bundle).migrate()
            conn = Database(bundle).connect(); self.seed(conn, 1); conn.close()
            server = create_server(bundle, port=0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                status, session = self.post_status(base + "/api/jobs/J000000/application", {"status": "APPLIED"}, self.mutation_headers(server))
                self.assertEqual(status, 200); self.assertTrue(session["ok"])
                conn = Database(bundle).connect(); self.assertEqual(conn.execute("SELECT application_status FROM jobs WHERE job_id='J000000'").fetchone()[0], "APPLIED"); conn.close()
                invalid = self.mutation_headers(server)
                invalid["Origin"] = "https://attacker.example"
                status, _ = self.post_status(base + "/api/jobs/J000000/application", {"status": "REJECTED"}, invalid)
                self.assertEqual(status, 403)
                invalid = self.mutation_headers(server); invalid.pop("X-JobBot-CSRF")
                status, _ = self.post_status(base + "/api/jobs/J000000/application", {"status": "REJECTED"}, invalid)
                self.assertEqual(status, 403)
                invalid = self.mutation_headers(server); invalid["Content-Type"] = "text/plain"
                status, _ = self.post_status(base + "/api/jobs/J000000/application", {"status": "REJECTED"}, invalid)
                self.assertEqual(status, 415)
                invalid = self.mutation_headers(server); invalid["Host"] = "attacker.example"
                status, _ = self.post_status(base + "/api/jobs/J000000/application", {"status": "REJECTED"}, invalid)
                self.assertEqual(status, 403)
                with urllib.request.urlopen(base + "/api/session", timeout=5) as response:
                    bootstrap = json.loads(response.read()); self.assertTrue(bootstrap["csrf_token"]); self.assertIn("127.0.0.1", bootstrap["origin"])
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_exports_formula_safety_application_history_and_funnel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "jobs.sqlite3", root / "out"); Database(bundle).migrate()
            conn = Database(bundle).connect(); self.seed(conn, 3)
            conn.execute("UPDATE jobs SET company='=HYPERLINK(\"bad\")' WHERE job_id='J000000'"); conn.commit()
            mark(conn, "J000000", "APPLIED", notes="submitted", source="test")
            mark(conn, "J000001", "SCREEN", notes="recruiter", source="test")
            self.assertEqual(len(history(conn, "J000000")), 1)
            funnel = analyze(conn); self.assertTrue(any(row.applications for row in funnel))
            paths = export_all(conn, bundle.output_dir, batch_size=2); conn.close()
            expected = {"all_jobs.csv", "active_jobs.csv", "qualified_jobs.csv", "unapplied_jobs.csv", "apply_now.csv", "apply_volume.csv", "stretch.csv", "application_tracker.csv", "jobs.jsonl", "recent_updates.csv", "chatgpt_batch.md"}
            self.assertEqual(set(paths), expected); self.assertTrue(all(path.is_file() for path in paths.values()))
            with paths["all_jobs.csv"].open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows[0]["company"].startswith("'="))

    def test_dashboard_reads_rich_discoveries_while_writer_commits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "jobs.sqlite3", root / "out"); Database(bundle).migrate()
            writer = Database(bundle).connect()
            now = "2026-09-07T12:00:00+00:00"
            run_id = writer.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES('3.2.1','test','linkedin','running',?)", (now,)).lastrowid
            task_id = writer.execute("INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at) VALUES(?,?,?,?,?,'running',?)", (run_id,"linkedin","patient access specialist",7,"https://www.linkedin.com/jobs/search/",now)).lastrowid
            writer.commit()
            server = create_server(bundle, port=0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                for index in range(3):
                    writer.execute("""INSERT INTO search_task_results(task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,browser_run_id,title_hint,company_hint,observed_at,detail_status)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (task_id,"linkedin",str(index),f"https://linkedin.test/{index}",now,now,run_id,"Patient Access Specialist","Example Health",now,"PENDING"))
                    writer.commit()
                    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/discoveries", timeout=5) as response:
                        payload = json.loads(response.read())
                    self.assertEqual(payload["pending"], index + 1)
                reader = Database(bundle).connect()
                try: self.assertEqual(len(live_discoveries(reader)["discoveries"]), 3)
                finally: reader.close()
            finally:
                writer.close(); server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_dashboard_identity_and_actionable_view(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "acceptance.sqlite3", root / "out"); Database(bundle).migrate()
            conn = Database(bundle).connect(); self.seed(conn, 2)
            conn.execute("UPDATE jobs SET recommendation='OUT_OF_SCOPE' WHERE job_id='J000001'"); conn.commit(); conn.close()
            server = create_server(bundle, port=0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/api/identity", timeout=5) as response: identity = json.loads(response.read())
                self.assertEqual(identity["resolved_database_path"], str((root / "acceptance.sqlite3").resolve()))
                self.assertEqual(identity["workspace_root"], str(bundle.root.resolve()))
                with urllib.request.urlopen(base + "/api/jobs?view=actionable", timeout=5) as response: actionable = json.loads(response.read())
                self.assertEqual(actionable["total"], 1)
                with urllib.request.urlopen(base + "/api/jobs?view=today", timeout=5) as response: today = json.loads(response.read())
                self.assertEqual(today["total"], 1)
                with urllib.request.urlopen(base + "/", timeout=5) as response: html = response.read().decode()
                self.assertIn('/static/dashboard.js', html)
                self.assertIn('id="identity"', html)
                self.assertIn('id="todayView"', html)
                self.assertIn('id="trackerView"', html)
                self.assertIn('ALL DISCOVERIES', html)
                self.assertNotIn('export.onclick', html)
                self.assertNotIn('<script>', html)
                self.assertIn('id="stopRunButton"', html)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_dashboard_note_is_not_a_fake_status_transition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "jobs.sqlite3", root / "out"); Database(bundle).migrate()
            conn = Database(bundle).connect(); self.seed(conn, 1); mark(conn, "J000000", "SHORTLIST", source="test")
            event = add_note(conn, "J000000", "Review benefits and follow up Friday", source="test")
            self.assertEqual(event.event_type, "NOTE")
            self.assertEqual(conn.execute("SELECT application_status FROM jobs WHERE job_id='J000000'").fetchone()[0], "SHORTLIST")
            self.assertEqual(conn.execute("SELECT event_type FROM application_events WHERE job_id='J000000' ORDER BY event_id DESC LIMIT 1").fetchone()[0], "NOTE")
            conn.close()

    def test_dashboard_run_controls_preserve_resumable_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bundle = bundle_with_database(root / "jobs.sqlite3", root / "out"); Database(bundle).migrate()
            conn = Database(bundle).connect(); now = "2026-09-07T12:00:00+00:00"
            run_id = conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES('3.2.1','test','linkedin','running',?)", (now,)).lastrowid
            task_id = conn.execute("INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at) VALUES(?,?,?,?,?,'running',?)", (run_id, "linkedin", "patient access specialist", 7, "https://www.linkedin.com/jobs/search/", now)).lastrowid
            conn.execute("INSERT INTO search_task_results(task_id,browser_run_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,detail_status,detail_lease_owner) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, run_id, "linkedin", "abc", "https://www.linkedin.com/jobs/view/abc", now, now, "RUNNING", "worker"))
            conn.commit(); conn.close()
            server = create_server(bundle, port=0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                def control(action: str) -> dict[str, object]:
                    request = urllib.request.Request(base + "/api/run/control", data=json.dumps({"action": action, "run_id": run_id}).encode(), headers=self.mutation_headers(server), method="POST")
                    with urllib.request.urlopen(request, timeout=5) as response: return json.loads(response.read())
                self.assertEqual(control("stop")["status"], "running")
                conn = Database(bundle).connect(); self.assertEqual(conn.execute("SELECT stop_after_current FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()[0], 1); conn.close()
                self.assertEqual(control("emergency")["status"], "stopped")
                conn = Database(bundle).connect()
                self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()[0], "incomplete")
                self.assertEqual(conn.execute("SELECT detail_status FROM search_task_results WHERE result_id=1").fetchone()[0], "RETRYABLE")
                conn.close()
                with patch("jobbot.dashboard.subprocess.Popen") as launch:
                    self.assertEqual(control("resume")["status"], "queued")
                    launch.assert_called_once()
                conn = Database(bundle).connect()
                self.assertEqual(conn.execute("SELECT status FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()[0], "queued")
                self.assertEqual(conn.execute("SELECT detail_status FROM search_task_results WHERE result_id=1").fetchone()[0], "RETRYABLE")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM browser_events WHERE browser_run_id=? AND event_type LIKE 'dashboard_%'", (run_id,)).fetchone()[0], 3)
                conn.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__": unittest.main()
