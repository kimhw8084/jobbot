from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 16
NAME = "CHG-146 platform workers controls and owned Chrome runtime"


def upgrade(conn: sqlite3.Connection) -> None:
    """Additive, idempotent runtime state for the dashboard-first control plane.

    Ephemeral Chrome ids are diagnostic state only.  The durable task lease and
    platform/run identity remain the business authority, so a browser restart
    can safely reclaim work without treating a stale tab as truth.
    """
    _ensure(conn, "browser_runs", {
        "last_meaningful_progress_at": "TEXT",
        "last_refresh_at": "TEXT",
        "active_worker_count": "INTEGER NOT NULL DEFAULT 0",
        "worker_generation": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure(conn, "browser_search_tasks", {
        "worker_id": "TEXT NOT NULL DEFAULT ''",
        "worker_generation": "INTEGER NOT NULL DEFAULT 0",
        "last_heartbeat_at": "TEXT",
        "detail_acquisition_mode": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure(conn, "browser_platform_runs", {
        "worker_status": "TEXT NOT NULL DEFAULT 'idle'",
        "worker_id": "TEXT NOT NULL DEFAULT ''",
        "worker_generation": "INTEGER NOT NULL DEFAULT 0",
        "worker_heartbeat_at": "TEXT",
        "worker_started_at": "TEXT",
        "worker_completed_at": "TEXT",
        "current_task_id": "INTEGER",
        "current_query": "TEXT NOT NULL DEFAULT ''",
        "current_search_url": "TEXT NOT NULL DEFAULT ''",
        "current_page_number": "INTEGER NOT NULL DEFAULT 0",
        "current_batch_size": "INTEGER NOT NULL DEFAULT 0",
        "last_meaningful_progress_at": "TEXT",
        "last_progress_message": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "challenge_reason": "TEXT NOT NULL DEFAULT ''",
        "stop_after_current": "INTEGER NOT NULL DEFAULT 0",
        "emergency_stop": "INTEGER NOT NULL DEFAULT 0",
        "receiver_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "chrome_available": "INTEGER NOT NULL DEFAULT 0",
        "owned_window": "INTEGER NOT NULL DEFAULT 0",
        "window_id": "INTEGER",
        "window_state": "TEXT NOT NULL DEFAULT ''",
        "window_focused": "INTEGER NOT NULL DEFAULT 0",
        "search_tab_id": "INTEGER",
        "search_tab_url": "TEXT NOT NULL DEFAULT ''",
        "focus_requested_at": "TEXT",
        "last_control_at": "TEXT",
        "last_control_action": "TEXT NOT NULL DEFAULT ''",
    })
    conn.execute("""CREATE TABLE IF NOT EXISTS control_requests(
      control_id INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id TEXT NOT NULL UNIQUE,
      browser_run_id INTEGER NOT NULL,
      platform TEXT NOT NULL DEFAULT '',
      action TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'PENDING',
      requested_at TEXT NOT NULL,
      acknowledged_at TEXT,
      worker_id TEXT NOT NULL DEFAULT '',
      result_json TEXT NOT NULL DEFAULT '{}',
      requested_by TEXT NOT NULL DEFAULT 'dashboard',
      FOREIGN KEY(browser_run_id) REFERENCES browser_runs(browser_run_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_control_requests_delivery
      ON control_requests(browser_run_id,platform,status,control_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_platform_workers
      ON browser_platform_runs(browser_run_id,platform,worker_status,worker_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_task_worker_lease
      ON browser_search_tasks(browser_run_id,platform,status,lease_until,worker_id)""")
    conn.execute("""UPDATE browser_platform_runs
      SET worker_status=CASE
        WHEN auth_status='sign_in_required' THEN 'sign_in_required'
        WHEN readiness_state='challenged_cooldown' THEN 'challenged'
        ELSE worker_status END
      WHERE worker_status='idle'""")
    # A restored database may have had the previous migrations applied before
    # a legacy result was inserted. Re-evaluate identity-only rows here so the
    # additive CHG-146 migration remains safe and self-healing.
    conn.execute("""UPDATE search_task_results
       SET content_state='MISSING', detail_status='RETRYABLE',
           detail_error=CASE WHEN detail_error='' THEN
             'historical identity-only detail requires re-enrichment' ELSE detail_error END
       WHERE detail_status='COMPLETE' AND (
         canonical_job_id IS NULL OR NOT EXISTS (
           SELECT 1 FROM jobs j WHERE j.job_id=search_task_results.canonical_job_id
             AND (j.description_state='COMPLETE' OR length(trim(COALESCE(j.description,'')))>=250)))""")
    conn.execute("""UPDATE jobs SET
       location_raw='', remote_status='unknown', remote_gate='review',
       remote_gate_reason='location not observed; search intent is not evidence',
       remote_confidence=0, location_evidence_state='UNKNOWN', remote_evidence_state='UNKNOWN'
       WHERE source_verification='assisted_board' AND COALESCE(canonical_verified,0)=0
         AND lower(trim(COALESCE(location_raw,''))) IN
           ('remote','fully remote','remote - united states','remote — united states','remote – united states')""")
    conn.execute("""UPDATE jobs SET
       apply_url='', apply_destination_state='UNKNOWN'
       WHERE source_verification='assisted_board' AND COALESCE(canonical_verified,0)=0
         AND lower(trim(COALESCE(apply_url,'')))=lower(trim(COALESCE(canonical_url,'')))""")
