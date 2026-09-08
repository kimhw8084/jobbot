from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobbot.config import load_bundle
from jobbot.db import Database
from jobbot.watch import WatchScheduler


class WatchIntegrationTests(unittest.TestCase):
    def test_stop_latch_blocks_due_work_until_explicit_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir(): (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root); Database(bundle).migrate(); conn = Database(bundle).connect()
            try:
                scheduler = WatchScheduler(conn, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
                scheduler.stop("STOP_SEARCH requested")
                self.assertTrue(scheduler.stopped())
                self.assertIsNone(scheduler.due_plan())
                scheduler.resume()
                self.assertFalse(scheduler.stopped())
                self.assertEqual(scheduler.due_plan(), "RECENT+DEEP+SUPPLEMENTAL")
            finally:
                conn.close()

    def test_stopped_watcher_cannot_reawaken_when_time_becomes_due(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "config").mkdir()
            source = Path(__file__).resolve().parents[2] / "config"
            for item in source.iterdir(): (root / "config" / item.name).write_bytes(item.read_bytes())
            bundle = load_bundle(root); Database(bundle).migrate(); conn = Database(bundle).connect()
            now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
            try:
                scheduler = WatchScheduler(conn, clock=lambda: now[0], recent_hours=6, deep_hours=24, supplemental_hours=6)
                scheduler.start("RECENT+DEEP+SUPPLEMENTAL")
                scheduler.finish("RECENT+DEEP+SUPPLEMENTAL", success=True, supplemental_ran=True)
                scheduler.stop("STOP_SEARCH requested")
                now[0] = datetime(2026, 1, 3, tzinfo=timezone.utc)
                self.assertIsNone(scheduler.due_plan())
                scheduler.finish("RECENT+DEEP+SUPPLEMENTAL", success=True, supplemental_ran=True)
                self.assertEqual(scheduler.state()["status"], "STOPPED")
                scheduler.resume()
                self.assertEqual(scheduler.due_plan(), "RECENT+DEEP+SUPPLEMENTAL")
            finally:
                conn.close()
