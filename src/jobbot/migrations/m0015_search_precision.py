from __future__ import annotations

import sqlite3

from ._sql import execute_statements
from .m0001_ledger import _ensure


VERSION = 15
NAME = "banded search cadence, detail priority, and query yield statistics"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_search_tasks", {
        "canonical_title": "TEXT NOT NULL DEFAULT ''",
        "search_band": "TEXT NOT NULL DEFAULT 'DEEP_TAIL'",
        "cadence_hours": "INTEGER NOT NULL DEFAULT 168",
        "detail_priority": "INTEGER NOT NULL DEFAULT 0",
        "yield_recorded_at": "TEXT",
    })
    _ensure(conn, "search_task_results", {
        "detail_priority": "INTEGER NOT NULL DEFAULT 0",
        "detail_priority_reason": "TEXT NOT NULL DEFAULT ''",
    })
    execute_statements(conn, """
    CREATE TABLE IF NOT EXISTS search_definition_state(
      platform TEXT NOT NULL,
      task_key TEXT NOT NULL,
      canonical_title TEXT NOT NULL DEFAULT '',
      normalized_query TEXT NOT NULL,
      search_band TEXT NOT NULL DEFAULT 'DEEP_TAIL',
      cadence_hours INTEGER NOT NULL DEFAULT 168,
      last_started_at TEXT,
      last_completed_at TEXT,
      next_due_at TEXT,
      last_status TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(platform,task_key)
    );
    CREATE INDEX IF NOT EXISTS idx_search_definition_due
      ON search_definition_state(next_due_at,search_band,platform);
    CREATE TABLE IF NOT EXISTS query_yield_stats(
      platform TEXT NOT NULL,
      normalized_query TEXT NOT NULL,
      search_band TEXT NOT NULL,
      cards_persisted INTEGER NOT NULL DEFAULT 0,
      completed_descriptions INTEGER NOT NULL DEFAULT 0,
      apply_now INTEGER NOT NULL DEFAULT 0,
      apply_volume INTEGER NOT NULL DEFAULT 0,
      high_value_stretch INTEGER NOT NULL DEFAULT 0,
      review INTEGER NOT NULL DEFAULT 0,
      hard_reject INTEGER NOT NULL DEFAULT 0,
      out_of_scope INTEGER NOT NULL DEFAULT 0,
      remote_pass INTEGER NOT NULL DEFAULT 0,
      total_browser_ms INTEGER NOT NULL DEFAULT 0,
      observation_runs INTEGER NOT NULL DEFAULT 0,
      observation_dates TEXT NOT NULL DEFAULT '[]',
      last_observed_at TEXT,
      PRIMARY KEY(platform,normalized_query,search_band)
    );
    CREATE INDEX IF NOT EXISTS idx_query_yield_band
      ON query_yield_stats(search_band,platform,normalized_query);
    """)
