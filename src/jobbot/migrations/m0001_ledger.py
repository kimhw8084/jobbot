from __future__ import annotations

import sqlite3

VERSION = 1
NAME = "permanent ledger and immutable versions"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    present = _columns(conn, table)
    for name, ddl in columns.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS jobs(
      job_id TEXT PRIMARY KEY,
      title TEXT, company TEXT, location_raw TEXT, canonical_url TEXT, apply_url TEXT,
      remote_status TEXT, employment_type TEXT,
      salary_text TEXT, salary_min REAL, salary_max REAL, salary_currency TEXT, salary_period TEXT,
      posted_at TEXT, description TEXT, category TEXT, tags_json TEXT,
      search_profile TEXT, career_lane TEXT, resume_variant TEXT,
      matched_keywords_json TEXT, remote_gate TEXT, remote_gate_reason TEXT,
      hard_reject_reasons_json TEXT, matched_evidence_json TEXT,
      matched_positive_json TEXT, matched_accelerators_json TEXT, matched_bilingual_json TEXT,
      years_required INTEGER, landing_score REAL, career_score REAL, door_score REAL,
      recommendation TEXT, score_reasons_json TEXT,
      first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
      application_status TEXT NOT NULL DEFAULT 'NEW', notes TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS source_occurrences(
      occurrence_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, source_site TEXT NOT NULL,
      source_job_id TEXT, source_url TEXT, apply_url TEXT, raw_json TEXT,
      first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
      FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    CREATE TABLE IF NOT EXISTS runs(
      run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, mode TEXT,
      source_status_json TEXT, new_jobs INTEGER, updated_jobs INTEGER
    );
    CREATE TABLE IF NOT EXISTS application_events(
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
      event_type TEXT NOT NULL, event_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
      FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    CREATE TABLE IF NOT EXISTS job_versions(
      version_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
      version_no INTEGER NOT NULL, observed_at TEXT NOT NULL, source_site TEXT,
      occurrence_key TEXT, content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
      diff_json TEXT NOT NULL, reason TEXT NOT NULL,
      UNIQUE(job_id,version_no), FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_rec ON jobs(recommendation,door_score DESC);
    CREATE INDEX IF NOT EXISTS idx_jobs_lane ON jobs(career_lane);
    CREATE INDEX IF NOT EXISTS idx_source_occurrences_job ON source_occurrences(job_id);
    CREATE INDEX IF NOT EXISTS idx_versions_job ON job_versions(job_id,version_no DESC);
    """)
    _ensure(conn, "jobs", {
        "normalized_title_family": "TEXT", "relevance_score": "REAL", "qualification_score": "REAL",
        "domain_score": "REAL", "remote_confidence": "REAL", "source_confidence": "REAL",
        "extraction_confidence": "REAL", "requirement_matches_json": "TEXT", "requirement_gaps_json": "TEXT",
        "required_skills_json": "TEXT", "management_required": "INTEGER", "posting_status": "TEXT",
        "application_deadline": "TEXT", "applied_at": "TEXT", "screen_at": "TEXT", "interview_at": "TEXT",
        "final_interview_at": "TEXT", "offer_at": "TEXT", "rejected_at": "TEXT",
        "current_content_hash": "TEXT", "canonical_source_site": "TEXT", "canonical_source_confidence": "REAL",
        "canonical_occurrence_key": "TEXT", "last_changed_at": "TEXT", "change_status": "TEXT",
        "update_count": "INTEGER DEFAULT 0", "is_active": "INTEGER DEFAULT 1", "closed_at": "TEXT",
        "last_scored_at": "TEXT", "strategy_version": "TEXT", "change_ack_at": "TEXT",
        "travel_percent": "REAL", "timezone_requirement": "TEXT", "work_auth_gate": "TEXT",
        "work_authorization_requirement": "TEXT", "salary_annual_mid": "REAL",
        "application_friction_score": "REAL", "urgency_score": "REAL", "application_priority_score": "REAL",
        "eligibility_confidence": "REAL", "employment_class": "TEXT", "employment_reason": "TEXT",
        "source_verification": "TEXT", "source_verification_reason": "TEXT",
        "canonical_verified": "INTEGER DEFAULT 0", "recall_reason": "TEXT",
    })
    _ensure(conn, "source_occurrences", {
        "source_board": "TEXT", "content_hash": "TEXT", "is_active": "INTEGER DEFAULT 1",
        "missed_complete_scans": "INTEGER DEFAULT 0", "last_seen_run_id": "INTEGER",
    })
    _ensure(conn, "runs", {
        "raw_jobs": "INTEGER DEFAULT 0", "canonical_seen": "INTEGER DEFAULT 0",
        "unchanged_jobs": "INTEGER DEFAULT 0", "closed_jobs": "INTEGER DEFAULT 0",
    })
