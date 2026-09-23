from __future__ import annotations

import sqlite3

from .m0001_ledger import _ensure


VERSION = 20
NAME = "CHG-114 durable search-quality ordering and inspection"


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "browser_search_tasks", {
        "baseline_execution_rank": "INTEGER NOT NULL DEFAULT 0",
        "effective_execution_rank": "INTEGER NOT NULL DEFAULT 0",
        "learned_order_reason": "TEXT NOT NULL DEFAULT 'baseline: historical ordering unavailable'",
        "learned_order_sample_size": "INTEGER NOT NULL DEFAULT 0",
        "ordering_algorithm_version": "TEXT NOT NULL DEFAULT 'chg114-yield-v1'",
    })
    _ensure(conn, "browser_runs", {
        "ordering_config_json": "TEXT NOT NULL DEFAULT '{}'",
    })
    conn.execute("""UPDATE browser_search_tasks
       SET baseline_execution_rank=execution_rank,
           effective_execution_rank=execution_rank
       WHERE baseline_execution_rank=0 OR effective_execution_rank=0""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_search_task_results_quality
       ON search_task_results(task_id,canonical_job_id,detail_status,result_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_source_occurrences_quality
       ON source_occurrences(strategy_profile,strategy_profile_version,source_site,query_family,query_kind,query_pass,job_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_browser_tasks_quality_identity
       ON browser_search_tasks(strategy_profile,strategy_profile_version,platform,query_family,query_kind,query_pass,task_key,window_days,phase)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_application_events_type_job
       ON application_events(event_type,job_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_sibling_recent
       ON jobs(last_seen DESC,job_id)""")
