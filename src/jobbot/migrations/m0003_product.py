from __future__ import annotations

import json
import sqlite3

from .m0001_ledger import _ensure
from ._sql import execute_statements

VERSION = 3
NAME = "dashboard application funnel verification and field diffs"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "application_events", {"source": "TEXT NOT NULL DEFAULT 'cli'"})
    execute_statements(conn, """
    CREATE TABLE IF NOT EXISTS job_diffs(
      diff_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, version_id INTEGER NOT NULL,
      field_name TEXT NOT NULL, old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
      observed_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(job_id),
      FOREIGN KEY(version_id) REFERENCES job_versions(version_id), UNIQUE(version_id,field_name)
    );
    CREATE TABLE IF NOT EXISTS funnel_events(
      funnel_event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
      event_type TEXT NOT NULL, event_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'cli',
      notes TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
      FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    CREATE TABLE IF NOT EXISTS source_verifications(
      verification_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
      occurrence_key TEXT, verified_at TEXT NOT NULL, status TEXT NOT NULL,
      canonical_url TEXT, employer TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
      FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    CREATE TABLE IF NOT EXISTS coverage_segments(
      segment_id TEXT PRIMARY KEY, platform TEXT NOT NULL, mode TEXT NOT NULL,
      search_profile TEXT NOT NULL, query_text TEXT NOT NULL, window_days INTEGER NOT NULL,
      remote_required INTEGER NOT NULL DEFAULT 1, search_url TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'due', last_opened_at TEXT, last_completed_at TEXT,
      completed_count INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS coverage_events(
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, segment_id TEXT NOT NULL,
      event_type TEXT NOT NULL, event_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_job_diffs_job ON job_diffs(job_id,version_id DESC);
    CREATE INDEX IF NOT EXISTS idx_funnel_job ON funnel_events(job_id,event_at);
    CREATE INDEX IF NOT EXISTS idx_source_verification_job ON source_verifications(job_id,verified_at DESC);
    CREATE INDEX IF NOT EXISTS idx_coverage_due ON coverage_segments(platform,status,last_completed_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_dashboard ON jobs(is_active,recommendation,application_status,door_score DESC);
    """)
    rows = conn.execute("SELECT version_id,job_id,observed_at,diff_json FROM job_versions").fetchall()
    for version_id, job_id, observed_at, raw in rows:
        try:
            diff = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(diff, dict):
            continue
        for field, change in diff.items():
            if field == "type" or not isinstance(change, dict):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO job_diffs(job_id,version_id,field_name,old_value_json,new_value_json,observed_at) VALUES(?,?,?,?,?,?)",
                (job_id, version_id, field, json.dumps(change.get("old"), ensure_ascii=False),
                 json.dumps(change.get("new"), ensure_ascii=False), observed_at),
            )
