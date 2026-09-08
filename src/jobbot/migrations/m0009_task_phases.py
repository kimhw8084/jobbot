from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure

VERSION = 9
NAME = "explicit staged search task phases"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_search_tasks", {
        "phase": "TEXT NOT NULL DEFAULT 'UNSPECIFIED'",
    })
