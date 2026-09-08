from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from jobbot.application import history, mark
from jobbot.dashboard import create_server, live_discoveries
from jobbot.db import Database
from jobbot.exports import export_all
from jobbot.funnel import analyze

from tests.helpers import bundle_with_database


class DashboardExportApplicationTests(unittest.TestCase):
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
                request = urllib.request.Request(base + f"/api/jobs/{job_id}/application", data=json.dumps({"status": "APPLIED", "notes": "dashboard test"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=5) as response: self.assertTrue(json.loads(response.read())["ok"])
                conn = Database(bundle).connect()
                self.assertEqual(conn.execute("SELECT application_status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "APPLIED")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM application_events WHERE job_id=?", (job_id,)).fetchone()[0], 1); conn.close()
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


if __name__ == "__main__": unittest.main()
