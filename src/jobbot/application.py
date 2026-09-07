from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


STATUSES = (
    "NEW", "SHORTLIST", "PREPARED", "APPLIED", "SCREEN", "INTERVIEW", "FINAL",
    "OFFER", "REJECTED", "WITHDRAWN", "SKIP", "CLOSED",
)


class ApplicationError(ValueError):
    pass


@dataclass(frozen=True)
class ApplicationEvent:
    event_id: int
    job_id: str
    event_type: str
    event_at: str
    source: str
    notes: str


def normalize_status(value: str) -> str:
    status = value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {"FINAL_INTERVIEW": "FINAL"}
    status = aliases.get(status, status)
    if status not in STATUSES:
        raise ApplicationError(f"invalid application status: {value}; choose {', '.join(STATUSES)}")
    return status


def mark(conn: sqlite3.Connection, job_id: str, status: str, *, notes: str = "", source: str = "cli") -> ApplicationEvent:
    normalized = normalize_status(status)
    if conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is None:
        raise ApplicationError(f"unknown job id: {job_id}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE jobs SET application_status=?,notes=CASE WHEN ?='' THEN notes ELSE ? END WHERE job_id=?",
            (normalized, notes, notes, job_id),
        )
        cursor = conn.execute(
            "INSERT INTO application_events(job_id,event_type,event_at,notes,source) VALUES(?,?,?,?,?)",
            (job_id, normalized, now, notes, source),
        )
        conn.execute(
            "INSERT INTO funnel_events(job_id,event_type,event_at,source,notes,metadata_json) VALUES(?,?,?,?,?,'{}')",
            (job_id, normalized, now, source, notes),
        )
        timestamp_column = {
            "APPLIED": "applied_at", "SCREEN": "screen_at", "INTERVIEW": "interview_at",
            "FINAL": "final_interview_at", "OFFER": "offer_at", "REJECTED": "rejected_at",
        }.get(normalized)
        if timestamp_column:
            conn.execute(f"UPDATE jobs SET {timestamp_column}=COALESCE({timestamp_column},?) WHERE job_id=?", (now, job_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ApplicationEvent(int(cursor.lastrowid), job_id, normalized, now, source, notes)


def add_note(conn: sqlite3.Connection, job_id: str, note: str, *, source: str = "cli") -> ApplicationEvent:
    if not note.strip():
        raise ApplicationError("note cannot be empty")
    row = conn.execute("SELECT application_status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise ApplicationError(f"unknown job id: {job_id}")
    return mark(conn, job_id, str(row[0] or "NEW"), notes=note.strip(), source=source)


def history(conn: sqlite3.Connection, job_id: str) -> list[ApplicationEvent]:
    rows = conn.execute(
        "SELECT event_id,job_id,event_type,event_at,COALESCE(source,'legacy') source,notes FROM application_events WHERE job_id=? ORDER BY event_id",
        (job_id,),
    ).fetchall()
    return [ApplicationEvent(int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5])) for r in rows]
