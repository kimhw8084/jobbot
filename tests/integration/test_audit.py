from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.audit import collect
from jobbot.browser_tasks import enqueue_gate
from jobbot.config import load_bundle
from jobbot.db import Database
from jobbot.legacy_engine import PrecisionStore
from tests.helpers import scored


class AuditIntegrationTests(unittest.TestCase):
    def test_current_run_terminal_and_reconciliation_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_gate(root, "linkedin")
            conn = Database(bundle).connect()
            try:
                conn.execute("UPDATE browser_search_tasks SET status='exhausted' WHERE browser_run_id=?", (run_id,))
                conn.execute("UPDATE browser_runs SET status='completed' WHERE browser_run_id=?", (run_id,))
                conn.commit()
                audit = collect(conn, strategy=bundle.strategy)
                self.assertEqual(audit["current_run_id"], run_id)
                self.assertEqual(audit["terminal_classification"], "COMPLETED_FULL")
                self.assertTrue(audit["reconciliation"]["ok"])
                self.assertIn("deferred_by_platform", audit["platforms"]["linkedin"])
                self.assertIn("detail_external_blocked", audit["global"])
                self.assertIn("cumulative", audit)
            finally:
                conn.close()

    def test_current_run_job_metrics_do_not_leak_from_cumulative_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_one = enqueue_gate(root, "linkedin")
            run_two = enqueue_gate(root, "linkedin")
            store = PrecisionStore(bundle.database_path)
            job_a = scored("Patient Enrollment Specialist", "Patient enrollment and access operations for a healthcare organization. " * 20)
            job_b = scored("Software Engineer", "Software engineering and backend development responsibilities. " * 20)
            store.upsert(job_a)
            store.upsert(job_b)
            store.conn.execute("UPDATE jobs SET recommendation='APPLY_NOW',is_active=1 WHERE job_id=?", (job_a.job_id,))
            store.conn.execute("UPDATE jobs SET recommendation='OUT_OF_SCOPE',is_active=1 WHERE job_id=?", (job_b.job_id,))
            task_one = store.conn.execute("SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? LIMIT 1", (run_one,)).fetchone()[0]
            task_two = store.conn.execute("SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? LIMIT 1", (run_two,)).fetchone()[0]
            now = "2026-09-08T12:00:00+00:00"
            store.conn.executemany(
                """INSERT INTO search_task_results(
                   task_id,source_site,source_job_id,source_url,canonical_job_id,first_seen_at,last_seen_at,detail_status
                ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (task_one, "linkedin", "a-1", "https://www.linkedin.com/jobs/view/a-1", job_a.job_id, now, now, "COMPLETE"),
                    (task_two, "linkedin", "b-1", "https://www.linkedin.com/jobs/view/b-1", job_b.job_id, now, now, "COMPLETE"),
                ],
            )
            store.conn.execute("UPDATE browser_search_tasks SET status='exhausted',cards_extracted=1,cards_persistence_attempted=1,cards_persistence_succeeded=1 WHERE browser_run_id IN (?,?)", (run_one, run_two))
            store.conn.execute("UPDATE browser_runs SET status='completed' WHERE browser_run_id IN (?,?)", (run_one, run_two))
            store.conn.commit()
            try:
                audit = collect(store.conn, run_id=run_two, strategy=bundle.strategy)
                self.assertEqual(audit["terminal_classification"], "COMPLETED_FULL")
                self.assertEqual(audit["global"]["apply_now"], 0)
                self.assertEqual(audit["global"]["canonical_jobs"], 1)
                self.assertEqual(audit["cumulative"]["global"]["apply_now"], 1)
                self.assertEqual(audit["cumulative"]["global"]["canonical_jobs"], 2)
            finally:
                store.close()

    def test_external_partial_is_a_successful_terminal_classification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            run_id = enqueue_gate(root, "linkedin")
            conn = Database(bundle).connect()
            try:
                conn.execute("UPDATE browser_search_tasks SET status='challenged' WHERE browser_run_id=? AND task_id=(SELECT MIN(task_id) FROM browser_search_tasks WHERE browser_run_id=?)", (run_id, run_id))
                conn.execute("UPDATE browser_search_tasks SET status='deferred_by_platform' WHERE browser_run_id=? AND task_id<>(SELECT MIN(task_id) FROM browser_search_tasks WHERE browser_run_id=?)", (run_id, run_id))
                conn.execute("UPDATE browser_runs SET status='partial' WHERE browser_run_id=?", (run_id,))
                conn.commit()
                audit = collect(conn, run_id=run_id, strategy=bundle.strategy)
                self.assertEqual(audit["terminal_classification"], "COMPLETED_PARTIAL_EXTERNAL")
                self.assertTrue(audit["reconciliation"]["ok"])
            finally:
                conn.close()
