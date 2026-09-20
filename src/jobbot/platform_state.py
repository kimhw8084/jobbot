"""Shared platform interaction-state vocabulary for the local control plane."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping


HUMAN_READINESS_STATES = frozenset({
    "challenged_cooldown",
    "sign_in_required",
    "user_action_required",
})
HUMAN_WORKER_STATES = frozenset({"challenged", "paused", "sign_in_required"})
SYSTEM_RETRYABLE_STATES = frozenset({"retryable"})
SYSTEM_UNVERIFIED_STATES = frozenset({"unverified", "unknown"})
RESUMABLE_TASK_STATUSES = frozenset({"queued", "running", "stopped", "incomplete"})
TERMINAL_TASK_MARKERS = (
    "acceptance limit reached",
    "acceptance-limit",
    "test limit reached",
    "test-limit",
    "limit reached",
    "exhausted",
    "terminal",
)


def _value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)  # type: ignore[union-attr]
    except AttributeError:
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError):
            return default


def _mappings(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, args)
    columns = [column[0] for column in cursor.description or ()]
    return [
        dict(zip(columns, row)) if not hasattr(row, "keys")
        else {key: row[key] for key in row.keys()}
        for row in cursor.fetchall()
    ]


def human_waiting(row: Mapping[str, Any]) -> bool:
    """Return whether the authoritative row requires an explicit human action."""
    interaction = str(_value(row, "interaction_state") or "").upper()
    readiness = str(_value(row, "readiness_state") or "").lower()
    auth = str(_value(row, "auth_status") or "").lower()
    worker = str(_value(row, "worker_status") or "").lower()
    return (
        interaction == "WAITING_FOR_HUMAN"
        or readiness in HUMAN_READINESS_STATES
        or auth == "not_authenticated"
        or worker in HUMAN_WORKER_STATES
    )


def interaction_state(row: Mapping[str, Any]) -> str:
    """Classify a platform for presentation and control ownership.

    Human ownership has precedence over system status so a challenged worker
    never renders as retryable, failed, or merely unverified.
    """
    persisted = str(_value(row, "interaction_state") or "").upper()
    if persisted == "WAITING_FOR_HUMAN" or human_waiting(row):
        return "WAITING_FOR_HUMAN"

    readiness = str(_value(row, "readiness_state") or "").lower()
    worker = str(_value(row, "worker_status") or "").lower()
    if worker in {"stale", "offline"}:
        return worker.upper()
    if worker in {"failed", "error"}:
        return "INTERNAL_ERROR"
    if readiness in SYSTEM_RETRYABLE_STATES:
        return "SYSTEM_RETRYABLE"
    if readiness in SYSTEM_UNVERIFIED_STATES:
        return "SYSTEM_UNVERIFIED"
    if worker == "rechecking":
        return "RECHECKING"
    if worker == "terminal" or (
        int(_value(row, "tasks_total") or 0) > 0
        and int(_value(row, "tasks_completed") or 0) >= int(_value(row, "tasks_total") or 0)
    ):
        return "COMPLETE"
    if worker == "running" or int(_value(row, "running") or 0) > 0:
        return "RUNNING"
    if worker == "stopped":
        return "STOPPED"
    if readiness in {"verified", "resumed"}:
        return "READY"
    return "IDLE"


def state_owner(state: str) -> str:
    if state == "WAITING_FOR_HUMAN":
        return "You"
    if state in {"SYSTEM_RETRYABLE", "SYSTEM_UNVERIFIED", "STALE", "OFFLINE", "INTERNAL_ERROR"}:
        return "JobBot"
    if state in {"STOPPED", "READY"}:
        return "Operator"
    return "Worker"


def is_runnable(row: Mapping[str, Any]) -> bool:
    state = interaction_state(row)
    return state in {"RUNNING", "READY", "RECHECKING", "IDLE"} and not human_waiting(row)


def resumable_task(row: Mapping[str, Any]) -> bool:
    """Return whether a task can be safely re-leased by a resume launcher.

    Acceptance/test-limit and exhausted rows are terminal even when a stale
    worker left them with an otherwise resumable-looking status.
    """
    if str(_value(row, "status") or "").lower() not in RESUMABLE_TASK_STATUSES:
        return False
    if int(_value(row, "exhausted") or 0):
        return False
    reason = " ".join(
        str(_value(row, key) or "").strip().lower()
        for key in ("safety_stop_reason", "exhaustion_reason")
    )
    return not any(marker in reason for marker in TERMINAL_TASK_MARKERS)


def runnable_resume(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Authoritatively calculate the platform/task set a global resume may touch.

    This intentionally excludes human ownership and system-owned recovery
    states.  Those states have their own explicit control contracts and must
    never become runnable merely because a global command exists.
    """
    platform_rows = _mappings(conn,
        "SELECT * FROM browser_platform_runs WHERE browser_run_id=? "
        "ORDER BY CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 "
        "WHEN 'glassdoor' THEN 2 ELSE 9 END, platform",
        (run_id,),
    )
    platforms: list[dict[str, Any]] = []
    task_ids: list[int] = []
    for platform_row in platform_rows:
        platform = str(platform_row["platform"])
        state = interaction_state(platform_row)
        task_rows = _mappings(conn,
            "SELECT * FROM browser_search_tasks WHERE browser_run_id=? AND platform=? "
            "ORDER BY task_id",
            (run_id, platform),
        )
        eligible = [row for row in task_rows if resumable_task(row)]
        # RUNNING/READY/IDLE/RECHECKING are runnable only when durable work
        # exists. STOPPED is runnable only for safe stopped/incomplete work.
        eligible_state = state in {"RUNNING", "READY", "IDLE", "RECHECKING"}
        if state == "STOPPED":
            eligible_state = bool(eligible)
        if human_waiting(platform_row) or state in {
            "SYSTEM_RETRYABLE", "SYSTEM_UNVERIFIED", "STALE", "OFFLINE",
            "INTERNAL_ERROR", "COMPLETE",
        }:
            eligible_state = False
        selected = eligible if eligible_state else []
        selected_ids = [int(row["task_id"]) for row in selected]
        if selected_ids:
            task_ids.extend(selected_ids)
            platforms.append({
                "platform": platform,
                "interaction_state": state,
                "task_ids": selected_ids,
                "task_count": len(selected_ids),
            })
    return {
        "platforms": platforms,
        "runnable_platforms": [item["platform"] for item in platforms],
        "task_ids": task_ids,
        "runnable_task_count": len(task_ids),
    }
