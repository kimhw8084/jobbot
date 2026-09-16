from __future__ import annotations

import sqlite3


VERSION = 14
NAME = "durable extension runtime binding diagnostics"


def upgrade(conn: sqlite3.Connection) -> None:
    fields = {row[1] for row in conn.execute("PRAGMA table_info(extension_refresh_requests)")}
    statements = []
    if "observed_source_identity" not in fields:
        statements.append("ALTER TABLE extension_refresh_requests ADD COLUMN observed_source_identity TEXT NOT NULL DEFAULT ''")
    if "observed_deployment_root" not in fields:
        statements.append("ALTER TABLE extension_refresh_requests ADD COLUMN observed_deployment_root TEXT NOT NULL DEFAULT ''")
    if "diagnostics_json" not in fields:
        statements.append("ALTER TABLE extension_refresh_requests ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}'")
    for statement in statements:
        conn.execute(statement)
