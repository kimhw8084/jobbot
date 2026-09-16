from __future__ import annotations

import sqlite3

from ._sql import execute_statements

VERSION = 13
NAME = "durable normal-Chrome extension refresh control"


def upgrade(conn: sqlite3.Connection) -> None:
    execute_statements(conn, """
    CREATE TABLE IF NOT EXISTS extension_refresh_requests(
      refresh_id TEXT PRIMARY KEY,
      browser_run_id INTEGER,
      expected_build TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      requested_at TEXT NOT NULL,
      confirmed_at TEXT,
      observed_build TEXT NOT NULL DEFAULT '',
      last_error TEXT NOT NULL DEFAULT '',
      reload_count INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY(browser_run_id) REFERENCES browser_runs(browser_run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_extension_refresh_run_status
      ON extension_refresh_requests(browser_run_id,status,requested_at);
    """)
