from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 12
NAME = "durable continuous scheduler stop latch"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "watch_state", {"stop_requested": "INTEGER NOT NULL DEFAULT 0"})
