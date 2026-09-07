from __future__ import annotations

import sqlite3

VERSION = 4
NAME = "canonical source occurrence table name"


def _object_type(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name=? AND type IN ('table','view')",
        (name,),
    ).fetchone()
    return str(row[0]) if row else None


def upgrade(conn: sqlite3.Connection) -> None:
    """Rename the historical table without copying or dropping occurrence data."""
    canonical = _object_type(conn, "source_occurrences")
    historical = _object_type(conn, "occurrences")
    if canonical is None and historical == "table":
        conn.execute("ALTER TABLE occurrences RENAME TO source_occurrences")
    elif canonical != "table":
        raise RuntimeError("source_occurrences table is missing")
    conn.execute("DROP INDEX IF EXISTS idx_occurrences_job")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_occurrences_job "
        "ON source_occurrences(job_id)"
    )
