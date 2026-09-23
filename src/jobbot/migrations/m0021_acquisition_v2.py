from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 21
NAME = "CHG-200 provider-neutral acquisition provenance"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_runs", {
        "acquisition_provider": "TEXT NOT NULL DEFAULT 'legacy-browser'",
        "acquisition_mode": "TEXT NOT NULL DEFAULT 'legacy-browser'",
        "provider_run_id": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure(conn, "browser_search_tasks", {
        "acquisition_provider": "TEXT NOT NULL DEFAULT 'legacy-browser'",
        "acquisition_mode": "TEXT NOT NULL DEFAULT 'legacy-browser'",
        "provider_run_id": "TEXT NOT NULL DEFAULT ''",
        "provider_task_id": "TEXT NOT NULL DEFAULT ''",
        "provider_completion_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "provider_failure_class": "TEXT NOT NULL DEFAULT ''",
        "completion_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        "query_task_key": "TEXT NOT NULL DEFAULT ''",
    })
    provenance_fields = {
        "acquisition_provider": "TEXT NOT NULL DEFAULT 'legacy-browser'",
        "acquisition_mode": "TEXT NOT NULL DEFAULT 'legacy-browser'",
        "provider_run_id": "TEXT NOT NULL DEFAULT ''",
        "provider_record_id": "TEXT NOT NULL DEFAULT ''",
        "provider_observed_at": "TEXT NOT NULL DEFAULT ''",
        "provider_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "query_task_key": "TEXT NOT NULL DEFAULT ''",
        "phase": "TEXT NOT NULL DEFAULT ''",
        "employer_job_url": "TEXT NOT NULL DEFAULT ''",
    }
    _ensure(conn, "search_task_results", provenance_fields)
    _ensure(conn, "source_occurrences", provenance_fields)

    # Older rows have no provider identity. Mark them deterministically while
    # keeping all existing job truth and evidence fields untouched.
    conn.execute("""UPDATE browser_runs SET acquisition_provider='legacy-browser',
        acquisition_mode='unknown', provider_run_id=''
        WHERE acquisition_provider='legacy-browser' AND provider_run_id=''""")
    conn.execute("""UPDATE browser_search_tasks SET acquisition_provider='legacy-browser',
        acquisition_mode='unknown', provider_run_id='', provider_task_id='',
        provider_completion_state='UNKNOWN', completion_evidence_json='{}',
        query_task_key=COALESCE(NULLIF(query_task_key,''),task_key,'')
        WHERE acquisition_provider='legacy-browser' AND provider_run_id=''""")
    conn.execute("""UPDATE search_task_results SET acquisition_provider='legacy-browser',
        acquisition_mode='unknown', provider_run_id='', provider_record_id='',
        provider_observed_at='', provider_metadata_json='{}',
        query_task_key=COALESCE(NULLIF(query_task_key,''),(SELECT COALESCE(task_key,'')
          FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),''),
        phase=COALESCE(NULLIF(phase,''),(SELECT COALESCE(phase,'')
          FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),'')
        WHERE acquisition_provider='legacy-browser' AND provider_run_id=''""")
    conn.execute("""UPDATE source_occurrences SET acquisition_provider='legacy-browser',
        acquisition_mode='unknown', provider_run_id='', provider_record_id='',
        provider_observed_at='', provider_metadata_json='{}',
        query_task_key=COALESCE(NULLIF(query_task_key,''),(SELECT COALESCE(NULLIF(t.task_key,''),'unknown')
          FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id
          WHERE r.canonical_job_id=source_occurrences.job_id AND r.source_site=source_occurrences.source_site
            AND (r.source_job_id=source_occurrences.source_job_id OR r.source_url=source_occurrences.source_url)
          ORDER BY r.last_seen_at DESC,r.result_id DESC LIMIT 1),'unknown'),
        phase=COALESCE(NULLIF(phase,''),(SELECT COALESCE(NULLIF(t.phase,''),'unknown')
          FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id
          WHERE r.canonical_job_id=source_occurrences.job_id AND r.source_site=source_occurrences.source_site
            AND (r.source_job_id=source_occurrences.source_job_id OR r.source_url=source_occurrences.source_url)
          ORDER BY r.last_seen_at DESC,r.result_id DESC LIMIT 1),'unknown')
        WHERE acquisition_provider='legacy-browser' AND provider_run_id=''""")
