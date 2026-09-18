from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 15
NAME = "production integrity evidence and enrichment states"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_runs", {
        "enrichment_mode": "TEXT NOT NULL DEFAULT 'recall_first'",
    })
    _ensure(conn, "browser_search_tasks", {
        "requested_search_url": "TEXT NOT NULL DEFAULT ''",
        "observed_page_url": "TEXT NOT NULL DEFAULT ''",
        "page_context_status": "TEXT NOT NULL DEFAULT 'UNVERIFIED'",
        "context_recovery_attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_recovery_at": "TEXT",
    })
    _ensure(conn, "browser_platform_runs", {
        "readiness_state": "TEXT NOT NULL DEFAULT 'unchecked'",
        "readiness_reason": "TEXT NOT NULL DEFAULT ''",
        "readiness_checked_at": "TEXT",
        "resumed_at": "TEXT",
    })
    _ensure(conn, "search_task_results", {
        "identity_status": "TEXT NOT NULL DEFAULT 'CAPTURED'",
        "identity_persisted_at": "TEXT",
        "card_metadata_status": "TEXT NOT NULL DEFAULT 'MISSING'",
        "content_state": "TEXT NOT NULL DEFAULT 'MISSING'",
        "enrichment_priority": "INTEGER NOT NULL DEFAULT 0",
        "recall_selected": "INTEGER NOT NULL DEFAULT 0",
        "recall_qa_sample": "INTEGER NOT NULL DEFAULT 0",
        "recall_reason": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure(conn, "jobs", {
        "content_state": "TEXT NOT NULL DEFAULT 'MISSING'",
        "enrichment_status": "TEXT NOT NULL DEFAULT 'IDENTITY_ONLY'",
        "enrichment_last_error": "TEXT NOT NULL DEFAULT ''",
        "location_evidence_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "remote_evidence_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "apply_destination_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "evidence_provenance_json": "TEXT NOT NULL DEFAULT '{}'",
    })

    conn.execute("""UPDATE browser_search_tasks
       SET requested_search_url=CASE WHEN requested_search_url='' THEN search_url ELSE requested_search_url END,
           observed_page_url=CASE WHEN observed_page_url='' THEN current_search_url ELSE observed_page_url END
       WHERE requested_search_url='' OR observed_page_url=''""")

    conn.execute("""UPDATE search_task_results
       SET identity_status='PERSISTED',
           identity_persisted_at=COALESCE(identity_persisted_at, first_seen_at),
           card_metadata_status=CASE
             WHEN length(trim(COALESCE(company_hint,'')))>0
               OR length(trim(COALESCE(location_hint,'')))>0
               OR length(trim(COALESCE(posted_text,'')))>0 THEN 'CAPTURED'
             ELSE 'PARTIAL' END,
           content_state=CASE
             WHEN canonical_job_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM jobs j WHERE j.job_id=search_task_results.canonical_job_id
                 AND j.description_state='COMPLETE') THEN 'COMPLETE'
             WHEN canonical_job_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM jobs j WHERE j.job_id=search_task_results.canonical_job_id
                 AND length(trim(COALESCE(j.description,'')))>0) THEN 'PARTIAL'
             ELSE 'MISSING' END,
           detail_status=CASE
             WHEN detail_status='COMPLETE' AND canonical_job_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM jobs j WHERE j.job_id=search_task_results.canonical_job_id
                 AND j.description_state='COMPLETE') THEN 'COMPLETE'
             WHEN detail_status='COMPLETE' AND canonical_job_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM jobs j WHERE j.job_id=search_task_results.canonical_job_id
                 AND length(trim(COALESCE(j.description,'')))>0) THEN 'PARTIAL'
             WHEN detail_status='COMPLETE' THEN 'RETRYABLE'
             ELSE detail_status END,
           detail_error=CASE
             WHEN detail_status='COMPLETE' AND (
               canonical_job_id IS NULL OR NOT EXISTS (
                 SELECT 1 FROM jobs j WHERE j.job_id=search_task_results.canonical_job_id
                   AND j.description_state='COMPLETE'))
               THEN CASE WHEN detail_error='' THEN 'historical identity-only detail requires re-enrichment' ELSE detail_error END
             ELSE detail_error END
       WHERE identity_status<>'PERSISTED' OR identity_persisted_at IS NULL""")

    conn.execute("""UPDATE jobs SET
       content_state=CASE
         WHEN description_state='COMPLETE' OR length(trim(COALESCE(description,'')))>=250 THEN 'COMPLETE'
         WHEN length(trim(COALESCE(description,'')))>0 THEN 'PARTIAL'
         ELSE 'MISSING' END,
       enrichment_status=CASE
         WHEN description_state='COMPLETE' OR length(trim(COALESCE(description,'')))>=250 THEN 'ENRICHED'
         WHEN length(trim(COALESCE(description,'')))>0 THEN 'PARTIAL'
         ELSE 'IDENTITY_ONLY' END,
       location_evidence_state=CASE WHEN length(trim(COALESCE(location_raw,'')))>0 THEN 'OBSERVED' ELSE 'UNKNOWN' END,
       remote_evidence_state=CASE
         WHEN remote_status IN ('remote','fully remote','100 remote','us remote')
           AND length(trim(COALESCE(location_raw,'')))>0 THEN 'OBSERVED'
         ELSE 'UNKNOWN' END,
       apply_destination_state=CASE
         WHEN length(trim(COALESCE(apply_url,'')))>0 AND apply_url<>canonical_url THEN 'OBSERVED'
         ELSE 'UNKNOWN' END,
       evidence_provenance_json=CASE WHEN evidence_provenance_json='{}' OR evidence_provenance_json='' THEN
         json_object('source_verification',COALESCE(source_verification,''),
                     'canonical_verified',COALESCE(canonical_verified,0))
         ELSE evidence_provenance_json END
    """)

    # CHG-112 production evidence proved these values were synthesized by the
    # browser path. Correct only current assisted-board state; immutable job
    # versions remain untouched and continue to preserve what was observed.
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
    conn.execute("""UPDATE source_occurrences SET apply_url=''
       WHERE source_site IN ('linkedin','indeed','glassdoor')
         AND lower(trim(COALESCE(apply_url,'')))=lower(trim(COALESCE(source_url,'')))""")

    conn.execute("""CREATE INDEX IF NOT EXISTS idx_search_results_enrichment_queue
       ON search_task_results(browser_run_id,task_id,detail_status,enrichment_priority,result_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_content_state
       ON jobs(content_state,enrichment_status)""")
