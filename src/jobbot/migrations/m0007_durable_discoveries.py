from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure

VERSION = 7
NAME = "rich discoveries and durable detail queue"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "search_task_results", {
        "browser_run_id": "INTEGER",
        "title_hint": "TEXT NOT NULL DEFAULT ''",
        "company_hint": "TEXT NOT NULL DEFAULT ''",
        "location_hint": "TEXT NOT NULL DEFAULT ''",
        "posted_text": "TEXT NOT NULL DEFAULT ''",
        "posted_age_days": "REAL",
        "observed_at": "TEXT",
        "card_json": "TEXT NOT NULL DEFAULT '{}'",
        "detail_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        "detail_attempts": "INTEGER NOT NULL DEFAULT 0",
        "detail_started_at": "TEXT",
        "detail_completed_at": "TEXT",
        "detail_error": "TEXT NOT NULL DEFAULT ''",
        "detail_lease_owner": "TEXT NOT NULL DEFAULT ''",
        "detail_lease_until": "TEXT",
    })
    conn.execute(
        """UPDATE search_task_results
           SET browser_run_id=(SELECT browser_run_id FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),
               observed_at=COALESCE(observed_at,first_seen_at),
               detail_status=CASE WHEN detail_read=1 THEN 'COMPLETE' ELSE 'PENDING' END
           WHERE browser_run_id IS NULL OR observed_at IS NULL"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_results_detail_queue "
        "ON search_task_results(browser_run_id,task_id,detail_status,result_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_results_detail_lease "
        "ON search_task_results(detail_status,detail_lease_until)"
    )
