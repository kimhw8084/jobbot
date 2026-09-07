from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure

VERSION = 5
NAME = "structured requirements remote evidence and score components"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "jobs", {
        "required_qualifications": "TEXT NOT NULL DEFAULT ''",
        "preferred_qualifications": "TEXT NOT NULL DEFAULT ''",
        "eligible_states_json": "TEXT NOT NULL DEFAULT '[]'",
        "remote_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        "schedule_requirement": "TEXT NOT NULL DEFAULT ''",
        "score_components_json": "TEXT NOT NULL DEFAULT '{}'",
    })
