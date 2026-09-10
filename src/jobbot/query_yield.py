"""Durable query economics and band cadence helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .search_strategy import BANDS, next_due, query_yield_estimate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(task: Any, key: str, default: Any = "") -> Any:
    if isinstance(task, dict):
        return task.get(key, default)
    return task[key] if key in task.keys() else default


def ensure_definition(conn, task: Any) -> None:
    task_key = _value(task, "task_key") or f"task:{_value(task, 'task_id', '')}"
    conn.execute(
        """INSERT INTO search_definition_state(
          platform,task_key,canonical_title,normalized_query,search_band,window_class,window_days,cadence_hours
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,task_key) DO UPDATE SET
          canonical_title=excluded.canonical_title,normalized_query=excluded.normalized_query,
          search_band=excluded.search_band,window_class=excluded.window_class,
          window_days=excluded.window_days,cadence_hours=excluded.cadence_hours""",
        (_value(task, "platform"), task_key, _value(task, "canonical_title"),
         _value(task, "query_text", _value(task, "query", "")), _value(task, "search_band", "DEEP_TAIL"),
         _value(task, "window_class", "DEEP"), int(_value(task, "window_days", _value(task, "age_days", 30)) or 30),
         int(_value(task, "cadence_hours", 168))),
    )


def mark_definition_started(conn, task: Any, started_at: str | None = None) -> None:
    ensure_definition(conn, task)
    task_key = _value(task, "task_key") or f"task:{_value(task, 'task_id', '')}"
    conn.execute(
        "UPDATE search_definition_state SET last_started_at=?,last_status='running' WHERE platform=? AND task_key=?",
        (started_at or _now(), _value(task, "platform"), task_key),
    )


def mark_definition_completed(conn, task: Any, status: str, completed_at: str | None = None) -> None:
    ensure_definition(conn, task)
    task_key = _value(task, "task_key") or f"task:{_value(task, 'task_id', '')}"
    completed_at = completed_at or _now()
    due_at = next_due(completed_at, int(_value(task, "cadence_hours", 168))) if status == "exhausted" else None
    conn.execute(
        """UPDATE search_definition_state SET last_completed_at=CASE WHEN ?='exhausted' THEN ? ELSE last_completed_at END,
          next_due_at=CASE WHEN ?='exhausted' THEN ? ELSE next_due_at END,last_status=?
          WHERE platform=? AND task_key=?""",
        (status, completed_at, status, due_at, status, _value(task, "platform"), task_key),
    )


def definition_is_due(row: Any, now: str) -> bool:
    if row is None:
        return True
    due_at = row["next_due_at"]
    return not due_at or str(due_at) <= now


def record_task_yield(conn, task_id: int, observed_at: str | None = None) -> bool:
    """Aggregate one exhausted task once; incomplete details are not negatives."""
    task = conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None or task["yield_recorded_at"] or task["status"] != "exhausted":
        return False
    observed_at = observed_at or task["completed_at"] or _now()
    all_rows = conn.execute(
        """SELECT r.canonical_job_id job_id,
                  MAX(j.recommendation) recommendation,
                  MAX(j.remote_gate) remote_gate,
                  CASE
                    WHEN MAX(CASE WHEN UPPER(COALESCE(j.description_state,''))='COMPLETE' THEN 1 ELSE 0 END)=1 THEN 'COMPLETE'
                    WHEN MAX(CASE WHEN UPPER(COALESCE(j.description_state,''))='PARTIAL_TOO_SHORT' THEN 1 ELSE 0 END)=1 THEN 'PARTIAL_TOO_SHORT'
                    WHEN MAX(CASE WHEN UPPER(COALESCE(j.description_state,''))='MISSING' THEN 1 ELSE 0 END)=1 THEN 'MISSING'
                    ELSE MAX(j.description_state)
                  END description_state
           FROM search_task_results r LEFT JOIN jobs j ON j.job_id=r.canonical_job_id
          WHERE r.task_id=? AND r.canonical_job_id IS NOT NULL
          GROUP BY r.canonical_job_id""", (task_id,)
    ).fetchall()
    rows = [row for row in all_rows if str(row["description_state"] or "").upper() == "COMPLETE"]
    card_count = int(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE task_id=?", (task_id,)).fetchone()[0] or 0)
    completed_descriptions = sum(row["description_state"] == "COMPLETE" for row in rows)
    def count(*values: str) -> int:
        return sum(str(row["recommendation"] or "") in values for row in rows)
    descriptions_missing = sum(str(row["description_state"] or "").upper() == "MISSING" for row in all_rows)
    descriptions_partial = sum(str(row["description_state"] or "").upper() == "PARTIAL_TOO_SHORT" for row in all_rows)
    detail_failed = int(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE task_id=? AND detail_status='FAILED'", (task_id,)).fetchone()[0] or 0)
    ledger_status_by_job: dict[str, str] = {}
    for event in conn.execute("SELECT payload_json FROM browser_events WHERE task_id=? AND event_type='job_recorded'", (task_id,)):
        try:
            payload = json.loads(event[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        job_id = str(payload.get("job_id") or "")
        status = str(payload.get("ledger_status") or "").lower()
        if job_id and status in {"new", "updated", "unchanged"}:
            # A later event for the same canonical job may be an unchanged
            # sighting. Preserve NEW evidence for this task's marginal value.
            if ledger_status_by_job.get(job_id) != "new":
                ledger_status_by_job[job_id] = status
    canonical_jobs_observed = len(all_rows)
    new_canonical_jobs = sum(ledger_status_by_job.get(str(row["job_id"]), "") == "new" for row in all_rows)
    if not ledger_status_by_job:
        new_canonical_jobs = min(int(task["jobs_new"] or 0), canonical_jobs_observed)
    apply_ready_observed = sum(str(row["recommendation"] or "") in {"APPLY_NOW", "APPLY_VOLUME"} for row in rows)
    new_apply_ready = sum(
        str(row["recommendation"] or "") in {"APPLY_NOW", "APPLY_VOLUME"}
        and ledger_status_by_job.get(str(row["job_id"]), "") == "new"
        for row in rows
    )
    duplicate_canonical_jobs = max(0, canonical_jobs_observed - new_canonical_jobs)
    active_browser_ms = max(0, int(task["task_active_browser_ms"] or 0))
    if not active_browser_ms:
        # Compatibility for pre-v16 rows and deterministic fixtures that
        # record the event directly. New bridge writes maintain the explicit
        # task-level non-overlapping accumulator above.
        for event in conn.execute("SELECT payload_json FROM browser_events WHERE task_id=? AND event_type='task_active_time'", (task_id,)):
            try:
                payload = json.loads(event[0] or "{}")
                active_browser_ms += max(0, int(float(payload.get("task_active_browser_ms", 0))))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    dates = {str(observed_at)[:10]}
    existing = conn.execute(
        "SELECT observation_dates FROM query_yield_stats WHERE platform=? AND normalized_query=? AND search_band=? AND window_class=? AND window_days=?",
        (task["platform"], str(task["query_text"]).casefold(), task["search_band"], task["window_class"], task["window_days"]),
    ).fetchone()
    if existing:
        try:
            dates.update(str(value) for value in json.loads(existing[0] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    conn.execute(
        """INSERT INTO query_yield_stats(
          platform,normalized_query,search_band,window_class,window_days,cards_persisted,completed_descriptions,
          descriptions_missing,descriptions_partial,detail_failed,apply_now,apply_volume,high_value_stretch,review,
          hard_reject,out_of_scope,remote_pass,task_active_browser_ms,total_browser_ms,observation_runs,observation_dates,last_observed_at,
          canonical_jobs_observed,new_canonical_jobs,apply_ready_observed,new_apply_ready,duplicate_canonical_jobs
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,normalized_query,search_band,window_class,window_days) DO UPDATE SET
          cards_persisted=query_yield_stats.cards_persisted+excluded.cards_persisted,
          completed_descriptions=query_yield_stats.completed_descriptions+excluded.completed_descriptions,
          descriptions_missing=query_yield_stats.descriptions_missing+excluded.descriptions_missing,
          descriptions_partial=query_yield_stats.descriptions_partial+excluded.descriptions_partial,
          detail_failed=query_yield_stats.detail_failed+excluded.detail_failed,
          apply_now=query_yield_stats.apply_now+excluded.apply_now,
          apply_volume=query_yield_stats.apply_volume+excluded.apply_volume,
          high_value_stretch=query_yield_stats.high_value_stretch+excluded.high_value_stretch,
          review=query_yield_stats.review+excluded.review,
          hard_reject=query_yield_stats.hard_reject+excluded.hard_reject,
          out_of_scope=query_yield_stats.out_of_scope+excluded.out_of_scope,
          remote_pass=query_yield_stats.remote_pass+excluded.remote_pass,
          task_active_browser_ms=query_yield_stats.task_active_browser_ms+excluded.task_active_browser_ms,
          total_browser_ms=query_yield_stats.total_browser_ms+excluded.total_browser_ms,
          canonical_jobs_observed=query_yield_stats.canonical_jobs_observed+excluded.canonical_jobs_observed,
          new_canonical_jobs=query_yield_stats.new_canonical_jobs+excluded.new_canonical_jobs,
          apply_ready_observed=query_yield_stats.apply_ready_observed+excluded.apply_ready_observed,
          new_apply_ready=query_yield_stats.new_apply_ready+excluded.new_apply_ready,
          duplicate_canonical_jobs=query_yield_stats.duplicate_canonical_jobs+excluded.duplicate_canonical_jobs,
          observation_runs=query_yield_stats.observation_runs+1,
          observation_dates=excluded.observation_dates,last_observed_at=excluded.last_observed_at""",
        (task["platform"], str(task["query_text"]).casefold(), task["search_band"], task["window_class"], int(task["window_days"] or 30), card_count,
         completed_descriptions, descriptions_missing, descriptions_partial, detail_failed, count("APPLY_NOW"), count("APPLY_VOLUME"),
         count("HIGH_VALUE_STRETCH"), count("REVIEW", "REVIEW_REMOTE"), count("SKIP_HARD_GATE"), count("OUT_OF_SCOPE"),
         sum(row["remote_gate"] == "pass" for row in rows), active_browser_ms, active_browser_ms, 1, json.dumps(sorted(dates)), observed_at,
         canonical_jobs_observed, new_canonical_jobs, apply_ready_observed, new_apply_ready, duplicate_canonical_jobs),
    )
    conn.execute("UPDATE browser_search_tasks SET yield_recorded_at=? WHERE task_id=?", (observed_at, task_id))
    return True


def economics(conn) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM query_yield_stats ORDER BY platform,normalized_query,search_band,window_class,window_days").fetchall()
    bands = {band: {"queries": 0, "sample_eligible": 0, "apply_ready": 0, "completed_descriptions": 0} for band in BANDS}
    eligible: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        estimate = query_yield_estimate(record)
        record["estimate"] = estimate
        band = record["search_band"] if record["search_band"] in bands else "DEEP_TAIL"
        bands[band]["queries"] += 1
        bands[band]["completed_descriptions"] += int(record["completed_descriptions"] or 0)
        bands[band]["apply_ready"] += int(estimate["apply_ready"])
        bands[band]["sample_eligible"] += int(estimate["sample_eligible"])
        if estimate["sample_eligible"]:
            eligible.append(record)
    # Operator ranking must match the scheduler: marginal NEW actionable yield
    # is the scarce value after overlap, while gross apply-ready rate remains a
    # diagnostic only.
    eligible.sort(key=lambda item: item["estimate"]["conservative_new_actionable_per_minute"], reverse=True)
    def compact(row: dict[str, Any]) -> dict[str, Any]:
        return {"platform": row["platform"], "query": row["normalized_query"], "band": row["search_band"],
                "window_class": row["window_class"], "window_days": row["window_days"], **row["estimate"]}
    return {
        "bands": bands,
        "eligible_sample_minimum": ">=30 completed descriptions across >=2 run dates",
        "top_queries": [compact(row) for row in eligible[:10]],
        "low_queries": [compact(row) for row in eligible[-10:]],
    }
