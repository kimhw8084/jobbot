from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 17
NAME = "marginal canonical query yield metrics"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "query_yield_stats", {
        "canonical_jobs_observed": "INTEGER NOT NULL DEFAULT 0",
        "new_canonical_jobs": "INTEGER NOT NULL DEFAULT 0",
        "apply_ready_observed": "INTEGER NOT NULL DEFAULT 0",
        "new_apply_ready": "INTEGER NOT NULL DEFAULT 0",
        "duplicate_canonical_jobs": "INTEGER NOT NULL DEFAULT 0",
    })
