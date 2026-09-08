from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot.audit import collect
from jobbot.browser_tasks import enqueue_gate
from jobbot.config import load_bundle
from jobbot.db import Database


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
