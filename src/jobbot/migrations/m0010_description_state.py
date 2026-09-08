from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 10
NAME = "truthful durable description state"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "jobs", {"description_state": "TEXT NOT NULL DEFAULT 'MISSING'"})
    conn.execute("""UPDATE jobs SET description_state=CASE
        WHEN length(trim(COALESCE(description,''))) >= 250 THEN 'COMPLETE'
        WHEN length(trim(COALESCE(description,''))) > 0 THEN 'PARTIAL_TOO_SHORT'
        ELSE 'MISSING' END""")
