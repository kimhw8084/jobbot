from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 18
NAME = "durable bridge RPC idempotency receipts"


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS rpc_receipts(
      request_id TEXT PRIMARY KEY,
      action TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'COMMITTED',
      response_json TEXT NOT NULL DEFAULT '{}',
      run_id INTEGER,
      task_id INTEGER,
      created_at TEXT NOT NULL,
      completed_at TEXT
    )""")
    _ensure(conn, "rpc_receipts", {
        "request_id": "TEXT PRIMARY KEY",
        "action": "TEXT NOT NULL",
        "payload_hash": "TEXT NOT NULL",
        "status": "TEXT NOT NULL DEFAULT 'COMMITTED'",
        "response_json": "TEXT NOT NULL DEFAULT '{}'",
        "run_id": "INTEGER",
        "task_id": "INTEGER",
        "created_at": "TEXT NOT NULL",
        "completed_at": "TEXT",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rpc_receipts_completed_at ON rpc_receipts(completed_at)")
