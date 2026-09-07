from __future__ import annotations

import sqlite3

from ._sql import execute_statements

from .m0001_ledger import _ensure

VERSION = 2
NAME = "durable normal-Chrome search tasks"


def upgrade(conn: sqlite3.Connection) -> None:
    execute_statements(conn, """
    CREATE TABLE IF NOT EXISTS browser_runs(
      browser_run_id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT NOT NULL, mode TEXT NOT NULL,
      platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
      started_at TEXT, completed_at TEXT, stop_requested INTEGER NOT NULL DEFAULT 0,
      auth_status TEXT NOT NULL DEFAULT 'unchecked', jobs_new INTEGER NOT NULL DEFAULT 0,
      jobs_updated INTEGER NOT NULL DEFAULT 0, jobs_unchanged INTEGER NOT NULL DEFAULT 0,
      jobs_recorded INTEGER NOT NULL DEFAULT 0, tasks_completed INTEGER NOT NULL DEFAULT 0,
      tasks_challenged INTEGER NOT NULL DEFAULT 0, tasks_incomplete INTEGER NOT NULL DEFAULT 0,
      current_task_id INTEGER, last_progress_at TEXT, last_error TEXT NOT NULL DEFAULT '',
      stop_after_current INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS browser_search_tasks(
      task_id INTEGER PRIMARY KEY AUTOINCREMENT, browser_run_id INTEGER NOT NULL,
      task_key TEXT, platform TEXT NOT NULL, query_text TEXT NOT NULL,
      remote_required INTEGER NOT NULL DEFAULT 1, window_days INTEGER NOT NULL,
      sort_order TEXT NOT NULL DEFAULT 'date', search_url TEXT NOT NULL, max_results INTEGER,
      status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL, started_at TEXT,
      completed_at TEXT, results_seen INTEGER NOT NULL DEFAULT 0, jobs_recorded INTEGER NOT NULL DEFAULT 0,
      jobs_new INTEGER NOT NULL DEFAULT 0, jobs_updated INTEGER NOT NULL DEFAULT 0,
      jobs_unchanged INTEGER NOT NULL DEFAULT 0, pages_visited INTEGER NOT NULL DEFAULT 0,
      challenge_reason TEXT NOT NULL DEFAULT '', checkpoint_json TEXT NOT NULL DEFAULT '{}',
      search_profile TEXT NOT NULL DEFAULT '', career_lane TEXT NOT NULL DEFAULT '',
      priority INTEGER NOT NULL DEFAULT 99, exhausted INTEGER NOT NULL DEFAULT 0,
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
      skip_old_cards INTEGER NOT NULL DEFAULT 1, detail_count_read INTEGER NOT NULL DEFAULT 0,
      unique_jobs_recorded INTEGER NOT NULL DEFAULT 0, duplicate_sightings INTEGER NOT NULL DEFAULT 0,
      current_search_url TEXT NOT NULL DEFAULT '', page_number INTEGER NOT NULL DEFAULT 0,
      scroll_generation INTEGER NOT NULL DEFAULT 0, last_page_fingerprint TEXT NOT NULL DEFAULT '',
      last_source_job_id TEXT NOT NULL DEFAULT '', last_progress_at TEXT,
      lease_owner TEXT NOT NULL DEFAULT '', lease_until TEXT, exhaustion_reason TEXT NOT NULL DEFAULT '',
      safety_stop_reason TEXT NOT NULL DEFAULT '', resume_variant TEXT NOT NULL DEFAULT '',
      FOREIGN KEY(browser_run_id) REFERENCES browser_runs(browser_run_id)
    );
    CREATE TABLE IF NOT EXISTS browser_events(
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, browser_run_id INTEGER, task_id INTEGER,
      event_at TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
      payload_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS browser_platform_runs(
      browser_run_id INTEGER NOT NULL, platform TEXT NOT NULL,
      auth_status TEXT NOT NULL DEFAULT 'unchecked', auth_reason TEXT NOT NULL DEFAULT '',
      auth_checked_at TEXT, tasks_total INTEGER NOT NULL DEFAULT 0,
      tasks_completed INTEGER NOT NULL DEFAULT 0, tasks_challenged INTEGER NOT NULL DEFAULT 0,
      tasks_failed INTEGER NOT NULL DEFAULT 0, tasks_incomplete INTEGER NOT NULL DEFAULT 0,
      jobs_recorded INTEGER NOT NULL DEFAULT 0, cooldown_until TEXT,
      PRIMARY KEY(browser_run_id,platform)
    );
    CREATE TABLE IF NOT EXISTS search_task_results(
      result_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
      source_site TEXT NOT NULL, source_job_id TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
      canonical_job_id TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
      sighting_count INTEGER NOT NULL DEFAULT 1, detail_read INTEGER NOT NULL DEFAULT 0,
      UNIQUE(task_id,source_site,source_job_id,source_url),
      FOREIGN KEY(task_id) REFERENCES browser_search_tasks(task_id)
    );
    CREATE INDEX IF NOT EXISTS idx_browser_tasks_run_status ON browser_search_tasks(browser_run_id,status,task_id);
    CREATE INDEX IF NOT EXISTS idx_browser_tasks_run_platform_status ON browser_search_tasks(browser_run_id,platform,status,priority,task_id);
    CREATE INDEX IF NOT EXISTS idx_browser_tasks_lease ON browser_search_tasks(status,lease_until);
    CREATE INDEX IF NOT EXISTS idx_search_task_results_task ON search_task_results(task_id);
    CREATE INDEX IF NOT EXISTS idx_search_task_results_job ON search_task_results(canonical_job_id);
    """)
    _ensure(conn, "browser_runs", {
        "tasks_incomplete": "INTEGER NOT NULL DEFAULT 0", "current_task_id": "INTEGER",
        "last_progress_at": "TEXT", "last_error": "TEXT NOT NULL DEFAULT ''",
        "stop_after_current": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure(conn, "browser_search_tasks", {
        "task_key": "TEXT", "search_profile": "TEXT NOT NULL DEFAULT ''",
        "career_lane": "TEXT NOT NULL DEFAULT ''", "priority": "INTEGER NOT NULL DEFAULT 99",
        "exhausted": "INTEGER NOT NULL DEFAULT 0", "attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT NOT NULL DEFAULT ''", "skip_old_cards": "INTEGER NOT NULL DEFAULT 1",
        "detail_count_read": "INTEGER NOT NULL DEFAULT 0", "unique_jobs_recorded": "INTEGER NOT NULL DEFAULT 0",
        "duplicate_sightings": "INTEGER NOT NULL DEFAULT 0", "current_search_url": "TEXT NOT NULL DEFAULT ''",
        "page_number": "INTEGER NOT NULL DEFAULT 0", "scroll_generation": "INTEGER NOT NULL DEFAULT 0",
        "last_page_fingerprint": "TEXT NOT NULL DEFAULT ''", "last_source_job_id": "TEXT NOT NULL DEFAULT ''",
        "last_progress_at": "TEXT", "lease_owner": "TEXT NOT NULL DEFAULT ''", "lease_until": "TEXT",
        "exhaustion_reason": "TEXT NOT NULL DEFAULT ''", "safety_stop_reason": "TEXT NOT NULL DEFAULT ''",
        "resume_variant": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure(conn, "browser_platform_runs", {
        "tasks_incomplete": "INTEGER NOT NULL DEFAULT 0", "cooldown_until": "TEXT",
    })
