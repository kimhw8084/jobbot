from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from jobbot.config import load_bundle
from jobbot.db import Database
from jobbot.watch import WatchScheduler


class WatchSchedulerTests(unittest.TestCase):
    def test_fake_clock_persists_cadence_and_explicit_states(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            conn = Database(bundle).connect()
            try:
                Database(bundle).migrate()
                now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
                scheduler = WatchScheduler(conn, clock=lambda: now[0], recent_hours=6, deep_hours=24, supplemental_hours=60)
                self.assertEqual(scheduler.due_plan(), "RECENT+DEEP+SUPPLEMENTAL")
                scheduler.start("RECENT+DEEP+SUPPLEMENTAL", 10)
                self.assertEqual(scheduler.state()["status"], "RUNNING")
                scheduler.finish("RECENT+DEEP+SUPPLEMENTAL", success=True, supplemental_ran=True)
                self.assertEqual(scheduler.state()["status"], "WAITING")
                now[0] += timedelta(hours=6)
                self.assertEqual(scheduler.due_plan(), "RECENT")
                scheduler.start("RECENT", 11); scheduler.finish("RECENT", success=True)
                now[0] += timedelta(hours=6)
                self.assertEqual(scheduler.due_plan(), "RECENT")
                scheduler.start("RECENT", 12); scheduler.finish("RECENT", success=True)
                now[0] += timedelta(hours=6)
                self.assertEqual(scheduler.due_plan(), "RECENT")
                scheduler.start("RECENT", 13); scheduler.finish("RECENT", success=True)
                self.assertEqual(scheduler.state()["next_deep_due_at"], "2026-01-02T00:00:00+00:00")
                now[0] += timedelta(hours=6)
                self.assertEqual(scheduler.due_plan(), "RECENT+DEEP")
                scheduler.start("RECENT+DEEP", 14); scheduler.finish("RECENT+DEEP", success=True)
                self.assertEqual(scheduler.state()["next_supplemental_due_at"], "2026-01-03T12:00:00+00:00")
                scheduler.finish("SUPPLEMENTAL", success=False, supplemental_ran=True, supplemental_success=True)
                self.assertEqual(scheduler.state()["next_supplemental_due_at"], "2026-01-04T12:00:00+00:00")
                scheduler.stop("test stop")
                self.assertIsNone(scheduler.due_plan())
                self.assertTrue(scheduler.stopped())
                scheduler.resume()
                self.assertFalse(scheduler.stopped())
                now[0] += timedelta(hours=6)
                self.assertIsNotNone(scheduler.due_plan())
            finally:
                conn.close()

    def test_watch_schema_is_isolated_from_production_path(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir():
                (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root)
            Database(bundle).migrate()
            self.assertNotEqual(bundle.database_path, Path("data/jobs.sqlite3").resolve())
