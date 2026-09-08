from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from jobbot.browser_tasks import enqueue_gate
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
                scheduler = WatchScheduler(conn, clock=lambda: now[0], recent_hours=6, deep_hours=24)
                self.assertEqual(scheduler.due_mode(), "staged")
                scheduler.start("staged", 10)
                self.assertEqual(scheduler.state()["status"], "RUNNING")
                scheduler.finish(success=True)
                self.assertEqual(scheduler.state()["status"], "WAITING")
                self.assertIsNone(scheduler.due_mode())
                now[0] += timedelta(hours=6)
                self.assertEqual(scheduler.due_mode(), "staged_recent")
                now[0] += timedelta(hours=18)
                self.assertEqual(scheduler.due_mode(), "staged")
                scheduler.stop("test stop")
                self.assertEqual(scheduler.state()["status"], "STOPPED")
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
