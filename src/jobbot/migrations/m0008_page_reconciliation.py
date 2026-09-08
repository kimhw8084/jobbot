from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure

VERSION = 8
NAME = "page persistence reconciliation counters"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_search_tasks", {
        "execution_rank": "INTEGER NOT NULL DEFAULT 1000",
        "cards_extracted": "INTEGER NOT NULL DEFAULT 0",
        "cards_persistence_attempted": "INTEGER NOT NULL DEFAULT 0",
        "cards_persistence_succeeded": "INTEGER NOT NULL DEFAULT 0",
        "cards_persistence_failed": "INTEGER NOT NULL DEFAULT 0",
        "duplicate_cards": "INTEGER NOT NULL DEFAULT 0",
        "pending_details": "INTEGER NOT NULL DEFAULT 0",
        "details_failed": "INTEGER NOT NULL DEFAULT 0",
    })
