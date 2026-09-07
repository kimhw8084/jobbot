from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jobbot as j
import jobbot_v3 as v3
from native_host import jobbot_native_host as native


class JobBotRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = j.load_toml(ROOT / "config.toml")
        cls.strategy = j.load_toml(ROOT / "strategy.toml")

    def score(self, title: str, description: str, *, location="Remote - United States", employment="Full-time", source="greenhouse"):
        job = j.Job(source_site=source, source_job_id=title.replace(" ", "-"), canonical_url="https://boards.greenhouse.io/example/jobs/1001", apply_url="https://boards.greenhouse.io/example/jobs/1001", title=title, company="Example Health", location_raw=location, remote_status="remote", employment_type=employment, description=description, posted_at=j.now_iso())
        setattr(job, "_mode", "deep")
        return j.score_job(job, self.strategy, self.config.get("candidate", {}))

    def test_prior_false_positives_and_positive_roles(self):
        negatives = [
            "Senior AI Engineer", "Staff AI Engineer", "Senior Software Engineer", "Principal R&D Engineer",
            "Finance Accounting Director", "Deal Desk Senior Manager", "Product Security Analyst",
        ]
        for title in negatives:
            self.assertEqual(self.score(title, "Remote quality, compliance, workflow, data, and implementation.").recommendation, "OUT_OF_SCOPE", title)
        self.assertEqual(self.score("Patient Enrollment Specialist", "Remote healthcare enrollment and onboarding. Required Qualifications: 2 years relevant experience.").recommendation, "APPLY_NOW")
        self.assertNotEqual(self.score("Healthcare Data Quality Analyst", "Remote healthcare data quality. Required Qualifications: SQL, HL7, FHIR, and 4 years interoperability experience.").recommendation, "APPLY_NOW")
        self.assertEqual(self.score("Patient Access Specialist", "Remote healthcare access. Hybrid work model with three days onsite.").recommendation, "SKIP_HARD_GATE")
        self.assertEqual(self.score("Patient Enrollment Specialist", "Remote healthcare enrollment.", location="Remote - Philippines").recommendation, "SKIP_HARD_GATE")
        self.assertEqual(self.score("Certified Nurse Midwife", "Remote clinical care. Active CNM license required.").recommendation, "OUT_OF_SCOPE")
        self.assertFalse(j.phrase_present("SIS", "analysis"))
        self.assertFalse(j.phrase_present("Lean", "clean"))
        self.assertFalse(j.detect_required_credential("Requirements: do the work and do it well.", "DO", self.strategy))
        self.assertEqual(j.years_required("8+ years of program management"), 8)

    def test_fixtures_cover_each_primary_adapter_contract(self):
        fixture_dir = ROOT / "tests" / "fixtures"
        expected = {
            "indeed_search.html": ("indeed-1001", "Next Page"),
            "indeed_detail.html": ("JobPosting", "Patient Enrollment Specialist"),
            "linkedin_search.html": ("/jobs/view/2001/", "View next page"),
            "linkedin_detail.html": ("JobPosting", "Healthcare Quality Specialist"),
            "glassdoor_search.html": ("jl=3001", "pagination-next"),
            "glassdoor_detail.html": ("JobPosting", "Patient Access Specialist"),
        }
        for name, needles in expected.items():
            text = (fixture_dir / name).read_text(encoding="utf-8")
            for needle in needles:
                self.assertIn(needle, text, name)

    def test_task_generation_preserves_strategy_and_has_no_production_cap(self):
        tasks = v3.iter_strategy_tasks(self.strategy, "deep", list(v3.PLATFORMS))
        combos = {(j.clean_text(keyword).lower(), int(profile.get("bootstrap_backfill_days", 30) or 30)) for profile in self.strategy["searches"] if profile.get("enabled", True) for keyword in profile.get("keywords", []) if j.clean_text(keyword)}
        self.assertEqual(len(tasks), len({(t["platform"], t["query_text"].lower(), t["window_days"]) for t in tasks}))
        self.assertGreaterEqual(len(tasks), len(combos) * 3)
        self.assertTrue(all(t["search_url"].startswith("https://") for t in tasks))

    def test_legacy_capture_cannot_automate_big_three(self):
        with self.assertRaises(SystemExit) as blocked:
            j.c.browser_capture({}, {}, "linkedin")
        self.assertIn("normal-Chrome extension runner", str(blocked.exception))
        with self.assertRaises(SystemExit):
            j.c.browser_capture({}, {}, "web", "https://www.indeed.com/viewjob?jk=blocked")

    def test_ledger_cross_source_dedupe_versions_and_sightings(self):
        with tempfile.TemporaryDirectory() as td:
            store = j.PrecisionStore(Path(td) / "jobs.sqlite3")
            first = self.score("Patient Enrollment Specialist", "Remote healthcare enrollment and onboarding. Required Qualifications: 2 years relevant experience. " + ("HIPAA documentation and Excel. " * 40))
            first.source_site = "greenhouse"; first.source_job_id = "1001"
            self.assertEqual(store.upsert(first), "new")
            mirror = self.score(first.title, first.description, source="indeed")
            mirror.source_site = "indeed"; mirror.source_job_id = "indeed-1"; mirror.canonical_url = "https://www.indeed.com/viewjob?jk=indeed-1"; mirror.apply_url = first.apply_url
            self.assertEqual(store.upsert(mirror), "unchanged")
            changed = self.score(first.title, first.description + " Updated schedule and salary information.")
            changed.source_site = "greenhouse"; changed.source_job_id = "1001"; changed.salary_text = "$60,000 - $70,000"
            self.assertEqual(store.upsert(changed), "updated")
            jid = store.resolve_job_id(first)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertGreaterEqual(store.conn.execute("SELECT COUNT(*) FROM occurrences WHERE job_id=?", (jid,)).fetchone()[0], 2)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (jid,)).fetchone()[0], 2)
            diff = json.loads(store.versions(jid)[0]["diff_json"])
            self.assertIn("salary_text", diff); self.assertIn("description", diff)
            store.close()

    def test_native_task_checkpoint_result_detail_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); shutil.copy(ROOT / "config.toml", base / "config.toml"); shutil.copy(ROOT / "strategy.toml", base / "strategy.toml")
            (base / "data").mkdir(); native.BASE = base
            db = base / "data" / "jobs.sqlite3"
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; v3.init_browser_schema(conn); now = j.now_iso()
            rid = conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)", (v3.V3_VERSION, "test", "indeed", "queued", now)).lastrowid
            conn.execute("INSERT INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)", (rid, "indeed", 1))
            tid = conn.execute("INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at) VALUES(?,?,?,?,?,?,?)", (rid, "indeed", "patient enrollment specialist", 7, v3.indeed_search_url("patient enrollment specialist", 7), "queued", now)).lastrowid
            conn.commit(); conn.close()
            self.assertTrue(native.handle({"action": "begin_run", "run_id": rid})["ok"])
            task = native.handle({"action": "next_task", "run_id": rid})["task"]
            self.assertEqual(task["status"], "running")
            native.handle({"action": "record_result", "run_id": rid, "task_id": tid, "source_site": "indeed", "source_job_id": "abc", "source_url": "https://www.indeed.com/viewjob?jk=abc"})
            native.handle({"action": "detail_read", "run_id": rid, "task_id": tid, "source_site": "indeed", "source_job_id": "abc", "source_url": "https://www.indeed.com/viewjob?jk=abc"})
            native.handle({"action": "task_progress", "run_id": rid, "task_id": tid, "results_seen": 20, "pages_visited": 2, "checkpoint": {"search_url": "https://www.indeed.com/jobs?q=x&start=10", "page_fingerprint": "abc", "page_number": 2}})
            state = native.handle({"action": "run_status", "run_id": rid})
            self.assertEqual(state["tasks"][0]["results_seen"], 20); self.assertEqual(state["tasks"][0]["detail_count_read"], 1)
            native.handle({"action": "complete_task", "run_id": rid, "task_id": tid, "status": "exhausted", "reason": "fixture end", "exhausted": True})
            final = native.handle({"action": "finish_run", "run_id": rid})
            self.assertEqual(final["status"], "completed")

    def test_challenge_isolation_and_resume_only_unfinished_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); shutil.copy(ROOT / "config.toml", base / "config.toml"); shutil.copy(ROOT / "strategy.toml", base / "strategy.toml")
            (base / "data").mkdir(); native.BASE = base
            db = base / "data" / "jobs.sqlite3"; conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            v3.init_browser_schema(conn); now = j.now_iso()
            rid = conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)", (v3.V3_VERSION, "test", "linkedin,indeed,glassdoor", "running", now)).lastrowid
            for platform in v3.PLATFORMS:
                conn.execute("INSERT INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)", (rid, platform, 1))
                conn.execute("INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at) VALUES(?,?,?,?,?,?,?)", (rid, platform, f"{platform} query", 7, "https://example.test/search", "queued", now))
            conn.commit(); conn.close()
            paused = native.handle({"action": "pause_platform", "run_id": rid, "platform": "linkedin", "reason": "fixture challenge"})
            self.assertEqual(paused["tasks_paused"], 1)
            conn = sqlite3.connect(db); states = dict(conn.execute("SELECT platform,status FROM browser_search_tasks WHERE browser_run_id=?", (rid,)).fetchall()); conn.close()
            self.assertEqual(states["linkedin"], "challenged")
            self.assertEqual(states["indeed"], "queued")
            self.assertEqual(states["glassdoor"], "queued")
            conn = sqlite3.connect(db); conn.execute("UPDATE browser_search_tasks SET status='incomplete',checkpoint_json=? WHERE browser_run_id=? AND platform='indeed'", (json.dumps({"page_number": 4}), rid)); conn.commit(); conn.close()
            self.assertEqual(v3.resume_run(base, int(rid)), int(rid))
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            resumed = {r["platform"]: (r["status"], json.loads(r["checkpoint_json"])) for r in conn.execute("SELECT platform,status,checkpoint_json FROM browser_search_tasks WHERE browser_run_id=?", (rid,))}; run_status = conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (rid,)).fetchone()[0]; conn.close()
            self.assertEqual(run_status, "queued")
            self.assertEqual(resumed["indeed"], ("queued", {"page_number": 4}))
            self.assertEqual(resumed["linkedin"][0], "challenged")

    def test_schema_migration_uses_sqlite_backup_and_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); shutil.copy(ROOT / "config.toml", base / "config.toml"); shutil.copy(ROOT / "strategy.toml", base / "strategy.toml")
            (base / "data" / "backups").mkdir(parents=True)
            db = base / "data" / "jobs.sqlite3"; conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE browser_runs(browser_run_id INTEGER PRIMARY KEY, version TEXT, mode TEXT, platform TEXT, status TEXT, created_at TEXT)")
            conn.execute("CREATE TABLE browser_search_tasks(task_id INTEGER PRIMARY KEY, browser_run_id INTEGER, platform TEXT, query_text TEXT, window_days INTEGER, search_url TEXT, status TEXT, created_at TEXT)")
            conn.commit(); conn.close()
            v3.prepare_database(base)
            backups = list((base / "data" / "backups").glob("jobs_pre_v3_1_migration_*.sqlite3"))
            self.assertEqual(len(backups), 1)
            conn = sqlite3.connect(backups[0]); self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"); conn.close()
            conn = sqlite3.connect(db); v3.init_browser_schema(conn); self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"); conn.close()


if __name__ == "__main__":
    unittest.main()
