from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 22
NAME = "CHG-200 Bright Data provider runtime diagnostics"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_search_tasks", {
        "provider_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "provider_requests_submitted": "INTEGER NOT NULL DEFAULT 0",
        "provider_records_delivered": "INTEGER NOT NULL DEFAULT 0",
        "provider_reported_cost_json": "TEXT NOT NULL DEFAULT '{}'",
    })
