from __future__ import annotations

import sqlite3


VERSION = 13
NAME = "durable cross-run platform circuit state"


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS platform_state(
      platform TEXT PRIMARY KEY,
      auth_status TEXT NOT NULL DEFAULT 'unchecked',
      auth_reason TEXT NOT NULL DEFAULT '',
      last_auth_checked_at TEXT,
      challenged_at TEXT,
      cooldown_until TEXT,
      manual_retry_requested_at TEXT,
      last_success_at TEXT,
      last_error TEXT NOT NULL DEFAULT ''
    )""")
    conn.executemany("INSERT OR IGNORE INTO platform_state(platform) VALUES(?)", [(p,) for p in ("linkedin", "indeed", "glassdoor")])
