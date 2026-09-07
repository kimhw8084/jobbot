from __future__ import annotations

import sqlite3


def execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without ``executescript``'s implicit pre-commit."""
    pending = ""
    for line in script.splitlines():
        pending += line + "\n"
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                conn.execute(statement)
    if pending.strip():
        raise sqlite3.OperationalError("incomplete SQL migration statement")
