from __future__ import annotations

import sqlite3

from ._sql import execute_statements


VERSION = 11
NAME = "durable continuous scheduler state"


def upgrade(conn: sqlite3.Connection) -> None:
    execute_statements(conn, """
    CREATE TABLE IF NOT EXISTS watch_state(
      watch_id INTEGER PRIMARY KEY CHECK(watch_id=1),
      status TEXT NOT NULL DEFAULT 'WAITING',
      current_phase TEXT NOT NULL DEFAULT '',
      current_run_id INTEGER,
      last_cycle_at TEXT,
      last_successful_cycle_at TEXT,
      next_recent_due_at TEXT,
      next_deep_due_at TEXT,
      next_supplemental_due_at TEXT,
      last_error TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL
    );
    """)
