"""Shared platform interaction-state vocabulary for the local control plane."""

from __future__ import annotations

from typing import Any, Mapping


HUMAN_READINESS_STATES = frozenset({
    "challenged_cooldown",
    "sign_in_required",
    "user_action_required",
})
HUMAN_WORKER_STATES = frozenset({"challenged", "paused", "sign_in_required"})
SYSTEM_RETRYABLE_STATES = frozenset({"retryable"})
SYSTEM_UNVERIFIED_STATES = frozenset({"unverified", "unknown"})


def _value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)  # type: ignore[union-attr]
    except AttributeError:
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError):
            return default


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
