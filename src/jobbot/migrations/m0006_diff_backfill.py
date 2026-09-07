from __future__ import annotations

import json
import sqlite3

VERSION = 6
NAME = "backfill reviewable description diff values"


def upgrade(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT d.diff_id,v.diff_json
        FROM job_diffs d JOIN job_versions v ON v.version_id=d.version_id
        WHERE d.field_name='description'
          AND (d.old_value_json='null' OR d.new_value_json='null')"""
    ).fetchall()
    for diff_id, raw in rows:
        try:
            change = json.loads(raw or "{}").get("description", {})
        except (AttributeError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(change, dict):
            continue
        old_value = change.get("old", change.get("removed", []))
        new_value = change.get("new", change.get("added", []))
        conn.execute(
            "UPDATE job_diffs SET old_value_json=?,new_value_json=? WHERE diff_id=?",
            (json.dumps(old_value, ensure_ascii=False), json.dumps(new_value, ensure_ascii=False), diff_id),
        )
