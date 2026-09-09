from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure
from ._sql import execute_statements


VERSION = 16
NAME = "separate recent/deep precision windows and active browser time"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _create_yield_table(conn: sqlite3.Connection, name: str = "query_yield_stats") -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {name}(
      platform TEXT NOT NULL,
      normalized_query TEXT NOT NULL,
      search_band TEXT NOT NULL,
      window_class TEXT NOT NULL DEFAULT 'DEEP',
      window_days INTEGER NOT NULL DEFAULT 30,
      cards_persisted INTEGER NOT NULL DEFAULT 0,
      completed_descriptions INTEGER NOT NULL DEFAULT 0,
      descriptions_missing INTEGER NOT NULL DEFAULT 0,
      descriptions_partial INTEGER NOT NULL DEFAULT 0,
      detail_failed INTEGER NOT NULL DEFAULT 0,
      apply_now INTEGER NOT NULL DEFAULT 0,
      apply_volume INTEGER NOT NULL DEFAULT 0,
      high_value_stretch INTEGER NOT NULL DEFAULT 0,
      review INTEGER NOT NULL DEFAULT 0,
      hard_reject INTEGER NOT NULL DEFAULT 0,
      out_of_scope INTEGER NOT NULL DEFAULT 0,
      remote_pass INTEGER NOT NULL DEFAULT 0,
      task_active_browser_ms INTEGER NOT NULL DEFAULT 0,
      total_browser_ms INTEGER NOT NULL DEFAULT 0,
      observation_runs INTEGER NOT NULL DEFAULT 0,
      observation_dates TEXT NOT NULL DEFAULT '[]',
      last_observed_at TEXT,
      PRIMARY KEY(platform,normalized_query,search_band,window_class,window_days)
    )""")


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_search_tasks", {
        "window_class": "TEXT NOT NULL DEFAULT 'DEEP'",
        "query_variant": "TEXT NOT NULL DEFAULT 'primary'",
        "task_active_browser_ms": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure(conn, "search_definition_state", {
        "window_class": "TEXT NOT NULL DEFAULT 'DEEP'",
        "window_days": "INTEGER NOT NULL DEFAULT 30",
    })
    columns = _columns(conn, "query_yield_stats")
    if "window_class" not in columns:
        conn.execute("ALTER TABLE query_yield_stats RENAME TO query_yield_stats_legacy_v15")
        _create_yield_table(conn)
        conn.execute("""INSERT INTO query_yield_stats(
          platform,normalized_query,search_band,window_class,window_days,cards_persisted,
          completed_descriptions,apply_now,apply_volume,high_value_stretch,review,hard_reject,
          out_of_scope,remote_pass,total_browser_ms,observation_runs,observation_dates,last_observed_at
        ) SELECT platform,normalized_query,search_band,'DEEP',30,cards_persisted,
          completed_descriptions,apply_now,apply_volume,high_value_stretch,review,hard_reject,
          out_of_scope,remote_pass,total_browser_ms,observation_runs,observation_dates,last_observed_at
          FROM query_yield_stats_legacy_v15""")
        conn.execute("DROP TABLE query_yield_stats_legacy_v15")
    else:
        _ensure(conn, "query_yield_stats", {
            "window_days": "INTEGER NOT NULL DEFAULT 30",
            "descriptions_missing": "INTEGER NOT NULL DEFAULT 0",
            "descriptions_partial": "INTEGER NOT NULL DEFAULT 0",
            "detail_failed": "INTEGER NOT NULL DEFAULT 0",
            "task_active_browser_ms": "INTEGER NOT NULL DEFAULT 0",
        })
    execute_statements(conn, """
      CREATE INDEX IF NOT EXISTS idx_query_yield_window
        ON query_yield_stats(platform,normalized_query,window_class,window_days);
      CREATE INDEX IF NOT EXISTS idx_search_definition_window
        ON search_definition_state(window_class,window_days,platform,next_due_at);
    """)
