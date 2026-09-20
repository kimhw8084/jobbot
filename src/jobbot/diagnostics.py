"""Secret-free CHG-146 crawl instrumentation.

The counters are observational only. They never participate in leasing,
qualification, funnel state, or export truth. The current-main comparison is
an explicit semantic baseline for the former per-result detail-tab strategy,
which makes the architectural improvement testable without crawling a live
site twice.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any


def _iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _count_events(conn: sqlite3.Connection, run_id: int, event_type: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM browser_events WHERE browser_run_id=? AND event_type=?",
        (run_id, event_type),
    ).fetchone()[0] or 0)


def instrumentation(conn: sqlite3.Connection, run_id: int | None = None) -> dict[str, Any]:
    """Return safe run counters and a comparable current-main model."""
    if run_id is None:
        row = conn.execute("SELECT browser_run_id FROM browser_runs ORDER BY browser_run_id DESC LIMIT 1").fetchone()
        run_id = int(row[0]) if row else 0
    if not run_id:
        return {"run_id": None, "candidate": {}, "current_main_model": {}, "comparison": {}}

    event_rows = conn.execute(
        "SELECT event_at,event_type,payload_json FROM browser_events WHERE browser_run_id=? ORDER BY event_id",
        (run_id,),
    ).fetchall()
    payloads: list[dict[str, Any]] = []
    times: list[datetime] = []
    for row in event_rows:
        parsed = _iso(row["event_at"])
        if parsed:
            times.append(parsed)
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        payloads.append(payload if isinstance(payload, dict) else {})

    task_counts = conn.execute(
        """SELECT COALESCE(SUM(cards_persistence_succeeded),0) cards,
                  COALESCE(SUM(cards_persistence_failed),0) card_failures,
                  COALESCE(SUM(duplicate_cards),0) duplicates,
                  COALESCE(SUM(detail_count_read),0) task_details,
                  COALESCE(SUM(jobs_recorded),0) task_jobs,
                  COUNT(DISTINCT CASE WHEN status IN ('running','exhausted','incomplete','challenged','failed','stopped','auth_required','paused','deferred_by_platform') THEN platform END) worker_platforms
           FROM browser_search_tasks WHERE browser_run_id=?""",
        (run_id,),
    ).fetchone()
    cards = int(task_counts["cards"] or 0)
    card_failures = int(task_counts["card_failures"] or 0)
    duplicates = int(task_counts["duplicates"] or 0)
    details = int(task_counts["task_details"] or 0)
    jobs = int(task_counts["task_jobs"] or 0)
    pane_selections = _count_events(conn, run_id, "pane_selection")
    detail_navigations = _count_events(conn, run_id, "detail_navigation")
    windows_created = _count_events(conn, run_id, "worker_window_created")
    tabs_created = sum(max(0, int(payload.get("tab_count", 0) or 0)) for payload in payloads if payload.get("tab_count") is not None)
    if not tabs_created:
        tabs_created = windows_created
    detail_work_items = max(pane_selections, details, _count_events(conn, run_id, "detail_diagnostics"))
    candidate = {
        "top_level_navigations": _count_events(conn, run_id, "navigation"),
        "detail_page_navigations": detail_navigations,
        "detail_pane_selections": pane_selections,
        "windows_created": windows_created,
        "tabs_created": tabs_created,
        "search_tabs_per_platform_max": max((int(payload.get("tab_count", 1) or 1) for payload in payloads if payload.get("event_type") == "worker_window_created"), default=1),
        "cards_persisted": cards,
        "card_persistence_failures": card_failures,
        "duplicate_card_persistence": duplicates,
        "details_persisted": details,
        "jobs_persisted": jobs,
        "detail_work_items": detail_work_items,
        "platform_workers_observed": int(task_counts["worker_platforms"] or 0),
    }
    run = conn.execute("SELECT created_at,completed_at,status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
    start = _iso(run["created_at"]) if run else None
    end = _iso(run["completed_at"]) if run and run["completed_at"] else (max(times) if times else None)
    candidate["elapsed_seconds"] = max(0.0, round((end - start).total_seconds(), 3)) if start and end else None

    # This is the current-main behavior model: each detail work item implied
    # a standalone detail-page navigation. It is intentionally named as a
    # model so the evidence cannot be mistaken for a second live crawl.
    baseline = {
        "top_level_navigations": candidate["top_level_navigations"],
        "detail_page_navigations": detail_work_items,
        "detail_pane_selections": 0,
        "windows_created": max(3, int(task_counts["worker_platforms"] or 0)),
        "tabs_created": max(3, int(task_counts["worker_platforms"] or 0)),
        "search_tabs_per_platform_max": 1,
        "cards_persisted": cards,
        "details_persisted": details,
        "jobs_persisted": jobs,
        "elapsed_seconds": None,
        "basis": "current-main legacy per-result detail-tab semantics",
    }
    comparison = {
        "detail_page_navigation_delta": candidate["detail_page_navigations"] - baseline["detail_page_navigations"],
        "detail_page_navigation_reduction": baseline["detail_page_navigations"] - candidate["detail_page_navigations"],
        "one_search_tab_per_platform": candidate["search_tabs_per_platform_max"] <= 1,
        "no_duplicate_persistence_observed": candidate["duplicate_card_persistence"] == 0,
        "checkpoint_preservation_not_inferred": True,
    }
    return {"run_id": run_id, "run_status": str(run["status"]) if run else "unknown", "candidate": candidate, "current_main_model": baseline, "comparison": comparison}
