from __future__ import annotations

import json
import sqlite3

from .m0001_ledger import _ensure


VERSION = 18
NAME = "CHG-170 versioned live-search provenance and fail-closed eligibility"


def upgrade(conn: sqlite3.Connection) -> None:
    provenance = {
        "strategy_profile": "TEXT NOT NULL DEFAULT ''",
        "strategy_profile_version": "TEXT NOT NULL DEFAULT ''",
        "query_family": "TEXT NOT NULL DEFAULT ''",
        "query_kind": "TEXT NOT NULL DEFAULT ''",
        "query_pass": "TEXT NOT NULL DEFAULT ''",
        "initial_order": "INTEGER NOT NULL DEFAULT 0",
    }
    _ensure(conn, "browser_search_tasks", provenance)
    _ensure(conn, "search_task_results", provenance)
    _ensure(conn, "source_occurrences", provenance)
    _ensure(conn, "jobs", {
        "salary_annual_min": "REAL",
        "qualification_gates_json": "TEXT NOT NULL DEFAULT '{}'",
        "preference_signals_json": "TEXT NOT NULL DEFAULT '[]'",
        "preference_adjustment": "REAL NOT NULL DEFAULT 0",
    })
    conn.execute("""UPDATE search_task_results SET
        strategy_profile=COALESCE(NULLIF(strategy_profile,''),(SELECT strategy_profile FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),''),
        strategy_profile_version=COALESCE(NULLIF(strategy_profile_version,''),(SELECT strategy_profile_version FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),''),
        query_family=COALESCE(NULLIF(query_family,''),(SELECT query_family FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),''),
        query_kind=COALESCE(NULLIF(query_kind,''),(SELECT query_kind FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),''),
        query_pass=COALESCE(NULLIF(query_pass,''),(SELECT query_pass FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),''),
        initial_order=CASE WHEN initial_order=0 THEN COALESCE((SELECT initial_order FROM browser_search_tasks t WHERE t.task_id=search_task_results.task_id),0) ELSE initial_order END
        WHERE query_family='' OR strategy_profile='' OR initial_order=0""")
    conn.execute("""UPDATE jobs SET
        recommendation='ALREADY_HANDLED'
        WHERE recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')
          AND upper(COALESCE(application_status,'NEW'))<>'NEW'""")
    conn.execute("""UPDATE jobs SET
        recommendation='REVIEW',
        qualification_gates_json=?
        WHERE recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')
          AND COALESCE(qualification_gates_json,'{}')='{}'""", (
        json.dumps({
            "decision": "review",
            "unknown": ["CHG-170 positive hard-gate evidence requires recalculation"],
            "migration": NAME,
        }, ensure_ascii=False, sort_keys=True),
    ))
