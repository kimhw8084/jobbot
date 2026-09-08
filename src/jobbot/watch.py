from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable


RECENT = "RECENT"
DEEP = "DEEP"
SUPPLEMENTAL = "SUPPLEMENTAL"


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


def plan_name(*, recent: bool, deep: bool, supplemental: bool) -> str | None:
    names = [name for name, enabled in ((RECENT, recent), (DEEP, deep), (SUPPLEMENTAL, supplemental)) if enabled]
    return "+".join(names) if names else None


class WatchScheduler:
    """Durable cadence planner; browser work remains owned by SQLite tasks."""

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
        self.conn.execute("INSERT OR IGNORE INTO watch_state(watch_id,status,updated_at,stop_requested) VALUES(1,'WAITING',?,0)", (now,))
        self.conn.commit()

    def state(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM watch_state WHERE watch_id=1").fetchone()

    def stopped(self) -> bool:
        row = self.state()
        return str(row["status"]) == "STOPPED" or bool(row["stop_requested"])

    def due_plan(self, now: datetime | None = None) -> str | None:
        moment = now or self.clock()
        row = self.state()
        if self.stopped():
            return None
        recent_due = due(moment, row["next_recent_due_at"])
        deep_due = due(moment, row["next_deep_due_at"])
        supplemental_due = due(moment, row["next_supplemental_due_at"])
        return plan_name(recent=recent_due, deep=deep_due, supplemental=supplemental_due)

    # Backwards-compatible alias used by the first watch implementation.
    def due_mode(self, now: datetime | None = None) -> str | None:
        return self.due_plan(now)

    def start(self, plan: str, run_id: int | None = None) -> None:
        if self.stopped():
            raise RuntimeError("watch is STOPPED; explicitly restart continuous mode to clear the stop latch")
        now = iso(self.clock())
        self.conn.execute("UPDATE watch_state SET status='RUNNING',current_phase=?,current_run_id=?,updated_at=?,last_error='' WHERE watch_id=1", (plan, run_id, now))
        self.conn.commit()

    def bind_run(self, run_id: int) -> None:
        self.conn.execute("UPDATE watch_state SET current_run_id=?,updated_at=? WHERE watch_id=1", (run_id, iso(self.clock())))
        self.conn.commit()

    def finish(self, plan: str, *, success: bool, supplemental_ran: bool = False,
               supplemental_success: bool | None = None,
               stopped: bool = False, error: str = "") -> None:
        """Advance only cadence classes actually run successfully.

        A recent cycle never moves the deep deadline, and a browser cycle never
        moves the supplemental deadline unless the supplemental stage ran.
        """
        now = self.clock()
        if stopped:
            self.stop(error or "watch cycle stopped")
            return
        # STOP_SEARCH may have latched the durable state while the current
        # atomic browser task was winding down. Never overwrite that STOPPED
        # state with a successful WAITING update.
        if self.stopped():
            self.stop(error or "watch stop requested")
            return
        updates: dict[str, object] = {
            "status": "WAITING",
            "current_phase": "",
            "current_run_id": None,
            "last_cycle_at": iso(now),
            "last_error": "" if success else (error or "watch cycle incomplete"),
            "updated_at": iso(now),
        }
        if success and RECENT in plan:
            updates["next_recent_due_at"] = iso(now + timedelta(hours=self.recent_hours))
        if success and DEEP in plan:
            updates["next_deep_due_at"] = iso(now + timedelta(hours=self.deep_hours))
        supplemental_ok = success if supplemental_success is None else bool(supplemental_success)
        if supplemental_ran and supplemental_ok and SUPPLEMENTAL in plan:
            updates["next_supplemental_due_at"] = iso(now + timedelta(hours=self.supplemental_hours))
        if success:
            updates["last_successful_cycle_at"] = iso(now)
        assignments = ",".join(f"{key}=?" for key in updates)
        self.conn.execute(f"UPDATE watch_state SET {assignments} WHERE watch_id=1", (*updates.values(),))
        self.conn.commit()

    def stop(self, reason: str = "watch stopped") -> None:
        self.conn.execute("""UPDATE watch_state SET status='STOPPED',stop_requested=1,
          current_phase='',current_run_id=NULL,last_error=?,updated_at=? WHERE watch_id=1""", (reason, iso(self.clock())))
        self.conn.commit()

    def resume(self) -> None:
        self.conn.execute("UPDATE watch_state SET status='WAITING',stop_requested=0,current_phase='',current_run_id=NULL,last_error='',updated_at=? WHERE watch_id=1", (iso(self.clock()),))
        self.conn.commit()
