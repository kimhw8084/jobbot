from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 14
NAME = "conservative cross-platform merge evidence"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "jobs", {
        "canonical_merge_reason": "TEXT NOT NULL DEFAULT ''",
        "canonical_merge_confidence": "REAL NOT NULL DEFAULT 0",
    })
