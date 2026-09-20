from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 17
NAME = "CHG-159 durable human-wait interaction state"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_platform_runs", {
        "interaction_state": "TEXT NOT NULL DEFAULT 'IDLE'",
        "human_wait_reason": "TEXT NOT NULL DEFAULT ''",
        "human_wait_started_at": "TEXT",
        "last_checked_at": "TEXT",
    })
    _ensure(conn, "control_requests", {
        "target_worker_id": "TEXT NOT NULL DEFAULT ''",
        "target_worker_generation": "INTEGER NOT NULL DEFAULT 0",
        "delivered_at": "TEXT",
    })
    conn.execute("""UPDATE browser_platform_runs
       SET interaction_state=CASE
         WHEN readiness_state IN ('challenged_cooldown','sign_in_required','user_action_required')
              OR auth_status='not_authenticated'
              OR worker_status IN ('challenged','paused','sign_in_required') THEN 'WAITING_FOR_HUMAN'
         WHEN readiness_state='retryable' THEN 'SYSTEM_RETRYABLE'
         WHEN readiness_state IN ('unverified','unknown') THEN 'SYSTEM_UNVERIFIED'
         WHEN worker_status='rechecking' THEN 'RECHECKING'
         WHEN worker_status='running' THEN 'RUNNING'
         WHEN worker_status='stopped' THEN 'STOPPED'
         WHEN worker_status='terminal' THEN 'COMPLETE'
         ELSE 'IDLE' END,
           human_wait_reason=CASE
             WHEN readiness_state IN ('challenged_cooldown','sign_in_required','user_action_required')
                  OR auth_status='not_authenticated'
                  OR worker_status IN ('challenged','paused','sign_in_required')
             THEN COALESCE(NULLIF(challenge_reason,''),NULLIF(readiness_reason,''),NULLIF(auth_reason,''),human_wait_reason)
             ELSE human_wait_reason END,
           last_checked_at=COALESCE(last_checked_at,readiness_checked_at,worker_heartbeat_at)
       WHERE interaction_state='IDLE' OR interaction_state=''""")
    # Keep the existing incompleteness guarantee self-healing when a restored
    # ledger receives identity-only rows between migration steps.
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
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_platform_interaction_state
      ON browser_platform_runs(browser_run_id,interaction_state,platform)""")
