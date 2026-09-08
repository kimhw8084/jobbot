from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def due(now: datetime, scheduled: str | None) -> bool:
    point = parse(scheduled)
    return point is None or now >= point


class WatchScheduler:
    """Small durable scheduler; browser work remains owned by SQLite tasks."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] = utc_now,
                 recent_hours: int = 6, deep_hours: int = 24, supplemental_hours: int = 6):
        self.conn = conn
        self.clock = clock
        self.recent_hours = max(1, int(recent_hours))
        self.deep_hours = max(1, int(deep_hours))
        self.supplemental_hours = max(1, int(supplemental_hours))
        self._ensure()

    def _ensure(self) -> None:
        now = iso(self.clock())
        self.conn.execute("INSERT OR IGNORE INTO watch_state(watch_id,status,updated_at) VALUES(1,'WAITING',?)", (now,))
        self.conn.commit()

    def state(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM watch_state WHERE watch_id=1").fetchone()

    def due_mode(self, now: datetime | None = None) -> str | None:
        moment = now or self.clock()
        row = self.state()
        recent_due = due(moment, row["next_recent_due_at"])
        deep_due = due(moment, row["next_deep_due_at"])
        if recent_due and not row["last_cycle_at"]:
            return "staged"
        if recent_due and deep_due:
            return "staged"
        if deep_due:
            return "staged_deep"
        if recent_due:
            return "staged_recent"
        return None

    def start(self, mode: str, run_id: int | None = None) -> None:
        now = iso(self.clock())
        phase = {"staged": "A_FASTEST_DOOR_RECENT+B_REMAINING_CORE_RECENT+C_DEEP_BACKFILL",
                 "staged_recent": "A_FASTEST_DOOR_RECENT+B_REMAINING_CORE_RECENT",
                 "staged_deep": "C_DEEP_BACKFILL"}.get(mode, mode)
        self.conn.execute("UPDATE watch_state SET status='RUNNING',current_phase=?,current_run_id=?,updated_at=?,last_error='' WHERE watch_id=1", (phase, run_id, now))
        self.conn.commit()

    def bind_run(self, run_id: int) -> None:
        self.conn.execute("UPDATE watch_state SET current_run_id=?,updated_at=? WHERE watch_id=1", (run_id, iso(self.clock())))
        self.conn.commit()

    def finish(self, *, success: bool, stopped: bool = False, error: str = "") -> None:
        now = self.clock()
        status = "STOPPED" if stopped else "WAITING"
        self.conn.execute("""UPDATE watch_state SET status=?,current_phase='',current_run_id=NULL,
          last_cycle_at=?,last_successful_cycle_at=CASE WHEN ? THEN ? ELSE last_successful_cycle_at END,
          next_recent_due_at=?,next_deep_due_at=?,next_supplemental_due_at=?,last_error=?,updated_at=?
          WHERE watch_id=1""", (status, iso(now), int(success), iso(now) if success else None,
          iso(now + timedelta(hours=self.recent_hours)), iso(now + timedelta(hours=self.deep_hours)),
          iso(now + timedelta(hours=self.supplemental_hours)), error, iso(now)))
        self.conn.commit()

    def stop(self, reason: str = "watch stopped") -> None:
        self.conn.execute("UPDATE watch_state SET status='STOPPED',current_phase='',current_run_id=NULL,last_error=?,updated_at=? WHERE watch_id=1", (reason, iso(self.clock())))
        self.conn.commit()
