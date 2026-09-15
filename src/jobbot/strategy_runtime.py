from __future__ import annotations

import copy
import sqlite3
from typing import Any


FALLBACK_LANE_ID = "FALLBACK_TRANSFERABLE"
ACTIONABLE_RECOMMENDATIONS = ("APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH")
TERMINAL_APPLICATION_STATUSES = (
    "APPLIED", "SCREEN", "INTERVIEW", "FINAL", "OFFER", "REJECTED", "WITHDRAWN", "SKIP", "CLOSED",
)


def fallback_activation_enabled(conn: sqlite3.Connection, runtime: dict[str, Any]) -> bool:
    """Return whether the configured zero-allocation fallback may be used."""
    threshold = int(runtime.get("daily_planner", {}).get("reservoir_fallback_threshold", 0) or 0)
    if threshold <= 0:
        return False
    recommendation_marks = ",".join("?" for _ in ACTIONABLE_RECOMMENDATIONS)
    status_marks = ",".join("?" for _ in TERMINAL_APPLICATION_STATUSES)
    row = conn.execute(
        f"""SELECT COUNT(*) FROM jobs
            WHERE is_active=1
              AND upper(COALESCE(posting_status,'')) <> 'CLOSED'
              AND remote_gate='pass'
              AND recommendation IN ({recommendation_marks})
              AND upper(COALESCE(application_status,'NEW')) NOT IN ({status_marks})""",
        (*ACTIONABLE_RECOMMENDATIONS, *TERMINAL_APPLICATION_STATUSES),
    ).fetchone()
    return int(row[0] or 0) < threshold


def with_fallback_activation(strategy: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Return a scoring view with the current fallback activation state."""
    active = copy.deepcopy(strategy)
    active["_fallback_enabled"] = bool(enabled)
    return active
