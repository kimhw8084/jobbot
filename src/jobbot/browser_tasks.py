#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import legacy_engine as j
from .config import PROJECT_ROOT, load_bundle
from .db import Database, apply_pending
from .search_plan import _task_key, build_search_url, compile_plan, compile_staged_plan, normalize_search_query
from .search_ordering import config_for_bundle, order_tasks
from .strategy_runtime import fallback_activation_enabled

V3_VERSION = "3.2.1"
EXTENSION_ID = "jfdlmelgonjhgnabpbipjefgamedpgfb"
PLATFORMS = ("linkedin", "indeed", "glassdoor")
PLATFORM_PRIORITY = {"linkedin": 0, "indeed": 1, "glassdoor": 2}


def base_dir() -> Path:
    return PROJECT_ROOT


def paths(base: Path) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any]]:
    bundle = load_bundle(base)
    cfg = bundle.legacy_runtime()
    strategy = bundle.strategy
    db = bundle.database_path
    out = bundle.output_dir
    Database(bundle).migrate()
    out.mkdir(parents=True, exist_ok=True)
    return db, out, base / "extension", cfg, strategy


def prepare_database(base: Path) -> None:
    """Compatibility entry point for the canonical migration runner."""
    Database(load_bundle(base)).migrate()


def init_browser_schema(conn: sqlite3.Connection) -> None:
    """Apply the canonical sequential migrations to a bridge connection."""
    apply_pending(conn)


def indeed_search_url(query: str, days: int) -> str:
    return build_search_url("indeed", normalize_search_query(query), days)


def linkedin_search_url(query: str, days: int) -> str:
    return build_search_url("linkedin", normalize_search_query(query), days)


def glassdoor_search_url(query: str, days: int) -> str:
    # Glassdoor exposes Remote result pages with a stable URL family.  Date is
    # enforced from card/detail age by the extension because the public result
    # URL does not reliably preserve a date filter.
    return build_search_url("glassdoor", normalize_search_query(query), days)


def _stamp_legacy_acquisition_identity(conn: sqlite3.Connection, run_id: int) -> None:
    provider_run_id = f"legacy-browser-run:{int(run_id)}"
    conn.execute(
        """UPDATE browser_runs SET acquisition_provider='legacy-browser',acquisition_mode='legacy-browser',
           provider_run_id=? WHERE browser_run_id=?""", (provider_run_id, run_id),
    )
    conn.execute(
        """UPDATE browser_search_tasks SET acquisition_provider='legacy-browser',acquisition_mode='legacy-browser',
           provider_run_id=?,query_task_key=COALESCE(NULLIF(query_task_key,''),task_key,'')
           WHERE browser_run_id=?""", (provider_run_id, run_id),
    )


def search_url(platform: str, query: str, days: int) -> str:
    return build_search_url(platform, normalize_search_query(query), days)


def _freeze_search_order(conn: sqlite3.Connection, tasks: list[Any], bundle: Any) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    payloads = []
    for task in tasks:
        if isinstance(task, dict):
            value = dict(task)
            value.setdefault("query_text", value.get("query", ""))
            value.setdefault("window_days", value.get("age_days", 0))
        else:
            value = {
                "task_key": task.task_key, "strategy_profile": task.strategy_profile,
                "strategy_profile_version": task.strategy_profile_version, "query_family": task.query_family,
                "query_kind": task.query_kind, "query_pass": task.query_pass, "platform": task.platform,
                "query_text": task.query, "window_days": task.age_days, "phase": task.phase,
                "priority": task.priority, "execution_rank": task.execution_rank,
            }
        payloads.append(value)
    ordered, metadata = order_tasks(conn, payloads, config_for_bundle(bundle))
    fields = {(str(item.get("task_key") or ""), str(item.get("phase") or "")): {
        "baseline_execution_rank": item["baseline_execution_rank"],
        "effective_execution_rank": item["effective_execution_rank"],
        "learned_order_reason": item["learned_order_reason"],
        "learned_order_sample_size": item["learned_order_sample_size"],
        "ordering_algorithm_version": item["ordering_algorithm_version"],
    } for item in ordered}
    return fields, metadata


def iter_strategy_tasks(strategy: dict[str, Any], mode: str, platforms: list[str]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    max_priority = int(strategy.get("strategy", {}).get("run_modes", {}).get(mode, {}).get("max_priority", 3))
    for lane in strategy.get("lanes", []):
        if not lane.get("enabled", True) or not lane.get("core", True):
            continue
        pri = int(lane.get("priority", 99))
        if pri > max_priority:
            continue
        days = int(lane.get(f"{mode}_days", 30))
        for kw in lane.get("titles", []):
            query = normalize_search_query(j.clean_text(kw))
            if not query: continue
            for platform in platforms:
                key = (platform, query.lower(), days)
                if key in seen: continue
                seen.add(key)
                tasks.append({
                    "platform": platform,
                    "query_text": query,
                    "window_days": days,
                    "search_profile": j.clean_text(lane.get("profile")),
                    "career_lane": j.clean_text(lane.get("id")),
                    "resume_variant": j.clean_text(lane.get("resume_variant")),
                    "priority": pri,
                    "execution_rank": int(lane.get("execution_rank", 1000)),
                    "search_url": search_url(platform, query, days),
                })
    tasks.sort(key=lambda x: (PLATFORM_PRIORITY.get(x["platform"], 99), x["execution_rank"], x["priority"], x["search_profile"], x["query_text"].lower()))
    return tasks


def enqueue_production(base: Path, mode: str = "deep", platforms: list[str] | None = None) -> int:
    db, _, _, _, strategy = paths(base)
    bundle = load_bundle(base)
    chosen = platforms or list(PLATFORMS)
    bad = [p for p in chosen if p not in PLATFORMS]
    if bad: raise ValueError(f"unsupported platform(s): {', '.join(bad)}")
    store = j.PrecisionStore(db); init_browser_schema(store.conn)
    include_fallback = fallback_activation_enabled(store.conn, bundle.runtime)
    if mode == "staged":
        planned = compile_staged_plan(bundle, chosen, include_fallback=include_fallback)
    elif mode == "staged_recent":
        planned = compile_staged_plan(bundle, chosen, ("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT"), include_fallback=include_fallback)
    elif mode == "staged_deep":
        planned = compile_staged_plan(bundle, chosen, ("C_DEEP_BACKFILL",), include_fallback=include_fallback)
    else:
        planned = compile_plan(bundle, mode, chosen, include_fallback=include_fallback)
    tasks = [{
        "task_key": task.task_key, "platform": task.platform, "query_text": task.query,
        "strategy_profile": task.strategy_profile,
        "strategy_profile_version": task.strategy_profile_version,
        "query_family": task.query_family, "query_kind": task.query_kind,
        "query_pass": task.query_pass, "initial_order": task.initial_order,
        "window_days": task.age_days, "search_profile": task.profile,
        "career_lane": task.career_lane, "resume_variant": task.resume_variant,
        "priority": task.priority, "search_url": task.search_url,
        "execution_rank": task.execution_rank, "phase": task.phase,
    } for task in planned]
    ordering_fields, ordering_metadata = _freeze_search_order(store.conn, tasks, bundle)
    now = j.now_iso()
    cur = store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, mode, ",".join(chosen), "queued", now,
         f"Platform-first exhaustive search. {len(tasks)} persistent tasks; no strategic result-count limit."),
    )
    rid = int(cur.lastrowid)
    store.conn.execute("UPDATE browser_runs SET ordering_config_json=? WHERE browser_run_id=?", (json.dumps(ordering_metadata, sort_keys=True), rid))
    for platform in chosen:
        n = sum(1 for t in tasks if t["platform"] == platform)
        store.conn.execute(
            "INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)",
            (rid, platform, n),
        )
    for t in tasks:
        store.conn.execute(
            """INSERT INTO browser_search_tasks(
              browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,status,created_at,
              search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,task_key,phase,
              strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,
              baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,ordering_algorithm_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, t["platform"], t["query_text"], 1, t["window_days"], "date", t["search_url"], None, "queued", now,
             t["search_profile"], t["career_lane"], t["resume_variant"], t["priority"], t["execution_rank"], 1, t["task_key"], t["phase"],
             t["strategy_profile"], t["strategy_profile_version"], t["query_family"], t["query_kind"], t["query_pass"], t["initial_order"],
             *ordering_fields[(t["task_key"], t["phase"])].values()),
        )
    _stamp_legacy_acquisition_identity(store.conn, rid)
    store.conn.commit(); store.close()
    return rid


def enqueue_validation_sample(
    base: Path,
    platforms: list[str] | None = None,
    *,
    phases: tuple[str, ...] = (
        "A_FASTEST_DOOR_RECENT",
        "B_REMAINING_CORE_RECENT",
        "C_DEEP_BACKFILL",
    ),
    per_phase_per_platform: int = 6,
    bundle=None,
) -> int:
    """Queue a bounded representative sample of the real staged plan.

    This is only for the final validator.  It uses the same compiled production
    definitions and URL builders, but deliberately leaves the rest of the
    production universe queued so a short validation window never pretends to
    have exhausted it.
    """
    if per_phase_per_platform < 1:
        raise ValueError("per_phase_per_platform must be positive")
    chosen = platforms or list(PLATFORMS)
    bad = [p for p in chosen if p not in PLATFORMS]
    if bad:
        raise ValueError(f"unsupported platform(s): {', '.join(bad)}")
    active_bundle = bundle or load_bundle(base)
    db = active_bundle.database_path
    Database(active_bundle).migrate()
    probe = Database(active_bundle).connect()
    try:
        include_fallback = fallback_activation_enabled(probe, active_bundle.runtime)
    finally:
        probe.close()
    planned = compile_staged_plan(active_bundle, chosen, phases, include_fallback=include_fallback)
    selected: list[Any] = []
    counts: dict[tuple[str, str], int] = {}
    for task in planned:
        key = (task.phase, task.platform)
        if counts.get(key, 0) >= per_phase_per_platform:
            continue
        selected.append(task)
        counts[key] = counts.get(key, 0) + 1
    store = j.PrecisionStore(db); init_browser_schema(store.conn); now = j.now_iso()
    ordering_fields, ordering_metadata = _freeze_search_order(store.conn, selected, active_bundle)
    rid = int(store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, "validation_sample", ",".join(chosen), "queued", now,
         f"Bounded representative A/B/C validation sample; {len(selected)} of the uncapped production definitions."),
    ).lastrowid)
    store.conn.execute("UPDATE browser_runs SET ordering_config_json=? WHERE browser_run_id=?", (json.dumps(ordering_metadata, sort_keys=True), rid))
    for platform in chosen:
        n = sum(1 for task in selected if task.platform == platform)
        store.conn.execute(
            "INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)",
            (rid, platform, n),
        )
    for task in selected:
        store.conn.execute(
            """INSERT INTO browser_search_tasks(
              browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,status,created_at,
              search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,task_key,phase,
              strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,
              baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,ordering_algorithm_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, task.platform, task.query, 1, task.age_days, task.sort_mode, task.search_url, None, "queued", now,
             task.profile, task.career_lane, task.resume_variant, task.priority, task.execution_rank, 1, task.task_key, task.phase,
             task.strategy_profile, task.strategy_profile_version, task.query_family, task.query_kind, task.query_pass, task.initial_order,
             *ordering_fields[(task.task_key, task.phase)].values()),
        )
    _stamp_legacy_acquisition_identity(store.conn, rid)
    store.conn.commit(); store.close()
    return rid


def enqueue_gate(base: Path, platform: str = "indeed", days: int = 7, max_results: int = 20) -> int:
    db, _, _, _, _ = paths(base)
    bundle = load_bundle(base)
    planned = compile_plan(bundle, "fast", [platform])[:3]
    store = j.PrecisionStore(db); init_browser_schema(store.conn); now = j.now_iso()
    gate_tasks = [{
        "task_key": _task_key(platform, task.query, days), "strategy_profile": task.strategy_profile,
        "strategy_profile_version": task.strategy_profile_version, "query_family": task.query_family,
        "query_kind": task.query_kind, "query_pass": task.query_pass, "platform": platform,
        "query_text": task.query, "window_days": days, "phase": "ACCEPTANCE_SMOKE",
        "priority": task.priority, "execution_rank": task.execution_rank,
    } for task in planned]
    ordering_fields, ordering_metadata = _freeze_search_order(store.conn, gate_tasks, bundle)
    rid = int(store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, "acceptance", platform, "queued", now, "Acceptance: auth + pagination + multi-query"),
    ).lastrowid)
    store.conn.execute("UPDATE browser_runs SET ordering_config_json=? WHERE browser_run_id=?", (json.dumps(ordering_metadata, sort_keys=True), rid))
    store.conn.execute("INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)", (rid, platform, len(planned)))
    for task in planned:
        store.conn.execute(
            """INSERT INTO browser_search_tasks(
              browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,status,created_at,
              search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,task_key,phase,
              strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,
              baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,ordering_algorithm_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, platform, task.query, 1, days, "date", search_url(platform, task.query, days), max_results, "queued", now,
             task.profile, task.career_lane, task.resume_variant, task.priority, task.execution_rank, 1,
             _task_key(platform, task.query, days), "ACCEPTANCE_SMOKE", task.strategy_profile,
             task.strategy_profile_version, task.query_family, task.query_kind, task.query_pass, task.initial_order,
             *ordering_fields[(_task_key(platform, task.query, days), "ACCEPTANCE_SMOKE")].values()),
        )
    _stamp_legacy_acquisition_identity(store.conn, rid)
    store.conn.commit(); store.close(); return rid


def enqueue_validation(base: Path, platforms: list[str] | None = None, *, max_results: int | None = None, bundle=None) -> int:
    """Create a tiny all-primary live proof run; never used by production RUN NOW."""
    chosen = platforms or list(PLATFORMS)
    bad = [p for p in chosen if p not in PLATFORMS]
    if bad:
        raise ValueError(f"unsupported platform(s): {', '.join(bad)}")
    active_bundle = bundle or load_bundle(base)
    db = active_bundle.database_path
    Database(active_bundle).migrate()
    store = j.PrecisionStore(db); init_browser_schema(store.conn); now = j.now_iso()
    planned = [(platform, compile_plan(active_bundle, "fast", [platform])[0]) for platform in chosen]
    ordering_payloads = [{
        "task_key": task.task_key, "strategy_profile": task.strategy_profile,
        "strategy_profile_version": task.strategy_profile_version, "query_family": task.query_family,
        "query_kind": task.query_kind, "query_pass": task.query_pass, "platform": platform,
        "query_text": task.query, "window_days": task.age_days, "phase": "VALIDATION_MICRO",
        "priority": task.priority, "execution_rank": task.execution_rank,
    } for platform, task in planned]
    ordering_fields, ordering_metadata = _freeze_search_order(store.conn, ordering_payloads, active_bundle)
    rid = int(store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, "validation_micro", ",".join(chosen), "queued", now, "Bounded <=5 minute production proof; runtime bounds the validation; production retrieval remains uncapped."),
    ).lastrowid)
    store.conn.execute("UPDATE browser_runs SET ordering_config_json=? WHERE browser_run_id=?", (json.dumps(ordering_metadata, sort_keys=True), rid))
    store.conn.executemany("INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,1)", [(rid, p) for p in chosen])
    for platform, task in planned:
        store.conn.execute(
            """INSERT INTO browser_search_tasks(
              browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,status,created_at,
              search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,task_key,phase,
              strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,
              baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,ordering_algorithm_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, platform, task.query, 1, task.age_days, "date", task.search_url, max_results, "queued", now,
             task.profile, task.career_lane, task.resume_variant, task.priority, task.execution_rank, 1,
             task.task_key, "VALIDATION_MICRO", task.strategy_profile, task.strategy_profile_version,
             task.query_family, task.query_kind, task.query_pass, task.initial_order,
             *ordering_fields[(task.task_key, "VALIDATION_MICRO")].values()),
        )
    _stamp_legacy_acquisition_identity(store.conn, rid)
    store.conn.commit(); store.close(); return rid


def resume_run(base: Path, rid: int | None = None) -> int:
    """Re-queue only unfinished tasks while retaining every task checkpoint."""
    db, _, _, _, _ = paths(base)
    store = j.PrecisionStore(db); init_browser_schema(store.conn)
    if rid is None:
        row = store.conn.execute(
            """SELECT browser_run_id FROM browser_runs
               WHERE status IN ('queued','running','partial','stopped')
                  OR EXISTS (SELECT 1 FROM browser_search_tasks t WHERE t.browser_run_id=browser_runs.browser_run_id
                    AND (t.status IN ('queued','running','stopped') OR (t.status='incomplete' AND t.safety_stop_reason NOT LIKE 'Acceptance limit reached%')))
               ORDER BY browser_run_id DESC LIMIT 1"""
        ).fetchone()
        if not row:
            store.close(); raise RuntimeError("no resumable browser run found")
        rid = int(row[0])
    exists = store.conn.execute("SELECT acquisition_provider,acquisition_mode FROM browser_runs WHERE browser_run_id=?", (rid,)).fetchone()
    if not exists:
        store.close(); raise RuntimeError(f"browser run not found: {rid}")
    if str(exists["acquisition_provider"] or "legacy-browser") != "legacy-browser":
        store.close()
        raise RuntimeError("provider acquisition runs must be resumed with their acquisition provider; legacy Chrome is never selected as fallback")
    now = j.now_iso()
    store.conn.execute(
        """UPDATE browser_search_tasks
           SET status='queued', completed_at=NULL, lease_owner='', lease_until=NULL,
               last_error='', safety_stop_reason=''
           WHERE browser_run_id=? AND (status IN ('running','stopped') OR
             (status='incomplete' AND safety_stop_reason NOT LIKE 'Acceptance limit reached%'))
             AND platform NOT IN (
               SELECT platform FROM browser_platform_runs WHERE browser_run_id=? AND interaction_state='WAITING_FOR_HUMAN'
             )""", (rid, rid)
    )
    store.conn.execute(
        """UPDATE search_task_results SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL,
             detail_error=CASE WHEN detail_error='' THEN 'requeued after run interruption' ELSE detail_error END
           WHERE browser_run_id=? AND detail_status='RUNNING'
             AND task_id IN (
               SELECT task_id FROM browser_search_tasks
               WHERE browser_run_id=? AND platform NOT IN (
                 SELECT platform FROM browser_platform_runs WHERE browser_run_id=? AND interaction_state='WAITING_FOR_HUMAN'
               )
             )""", (rid, rid, rid)
    )
    store.conn.execute(
        """UPDATE browser_search_tasks SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,
             challenge_reason='',last_error=''
           WHERE browser_run_id=? AND status IN ('auth_required','deferred_by_platform') AND platform IN (
             SELECT platform FROM browser_platform_runs WHERE browser_run_id=? AND auth_status IN ('not_authenticated','unknown','retryable','user_action_required')
           ) AND platform NOT IN (
             SELECT platform FROM browser_platform_runs WHERE browser_run_id=? AND interaction_state='WAITING_FOR_HUMAN'
           )""", (rid, rid, rid)
    )
    # Human-gated lanes are intentionally not released by elapsed cooldowns or
    # by a global resume.  Only the explicit platform recheck control may
    # convert this durable checkpoint back into runnable work.
    store.conn.execute(
        """UPDATE search_task_results SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL
           WHERE browser_run_id=? AND detail_status='EXTERNAL_BLOCKED' AND task_id IN (
             SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? AND status='queued'
               AND platform NOT IN (
                 SELECT platform FROM browser_platform_runs WHERE browser_run_id=? AND interaction_state='WAITING_FOR_HUMAN'
               )
           )""", (rid, rid, rid)
    )
    store.conn.execute(
        "UPDATE browser_platform_runs SET auth_status='unchecked',auth_reason='',readiness_state='unchecked',readiness_reason='',readiness_checked_at=NULL,interaction_state='RECHECKING',resumed_at=? WHERE browser_run_id=? AND platform IN (SELECT DISTINCT platform FROM browser_search_tasks WHERE browser_run_id=? AND status='queued') AND interaction_state<>'WAITING_FOR_HUMAN'",
        (now, rid, rid),
    )
    store.conn.execute(
        """UPDATE browser_runs SET status='queued', completed_at=NULL, stop_requested=0,
           stop_after_current=0, last_error='', last_progress_at=? WHERE browser_run_id=?""", (now, rid)
    )
    store.conn.commit(); store.close(); return int(rid)


def requeue_missing_enrichment(base: Path, rid: int | None = None, platforms: list[str] | None = None) -> dict[str, int]:
    """User-invoked maintenance path for existing identity-only discoveries.

    This changes only queue state. Sightings, job versions, and current ledger
    identity remain durable; the next normal-Chrome run may re-read detail
    surfaces with enrichment_mode=all.
    """
    db, _, _, _, _ = paths(base)
    store = j.PrecisionStore(db); init_browser_schema(store.conn)
    args: list[Any] = []
    platform_clause = ""
    if platforms:
        bad = [p for p in platforms if p not in PLATFORMS]
        if bad: raise ValueError(f"unsupported platform(s): {', '.join(bad)}")
        platform_clause = " AND t.platform IN (" + ",".join("?" for _ in platforms) + ")"
        args.extend(platforms)
    run_clause = ""
    if rid is not None:
        run_clause = " AND r.browser_run_id=?"; args.append(int(rid))
    rows = store.conn.execute(
        """SELECT r.result_id,r.task_id,r.browser_run_id FROM search_task_results r
           JOIN browser_search_tasks t ON t.task_id=r.task_id
           WHERE (r.content_state IN ('MISSING','PARTIAL') OR r.detail_status IN ('PARTIAL','FAILED'))"""
        + platform_clause + run_clause, args,
    ).fetchall()
    if not rows:
        store.close(); return {"discoveries": 0, "tasks": 0, "runs": 0}
    result_ids = [int(row[0]) for row in rows]
    task_ids = sorted({int(row[1]) for row in rows})
    run_ids = sorted({int(row[2]) for row in rows})
    placeholders = ",".join("?" for _ in result_ids)
    store.conn.execute(
        f"""UPDATE search_task_results SET detail_status='RETRYABLE',content_state=CASE WHEN content_state='PARTIAL' THEN 'PARTIAL' ELSE 'MISSING' END,
             detail_error='user-requested re-enrichment',detail_lease_owner='',detail_lease_until=NULL
             WHERE result_id IN ({placeholders})""", result_ids,
    )
    task_placeholders = ",".join("?" for _ in task_ids)
    store.conn.execute(
        f"""UPDATE browser_search_tasks SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,last_error='re-enrichment queued'
           WHERE task_id IN ({task_placeholders}) AND status NOT IN ('challenged','auth_required','deferred_by_platform')""", task_ids,
    )
    run_placeholders = ",".join("?" for _ in run_ids)
    store.conn.execute(
        f"""UPDATE browser_runs SET enrichment_mode='all',status='queued',completed_at=NULL,stop_requested=0,stop_after_current=0,last_error='user-requested re-enrichment queued'
           WHERE browser_run_id IN ({run_placeholders})""", run_ids,
    )
    store.conn.commit(); store.close()
    return {"discoveries": len(result_ids), "tasks": len(task_ids), "runs": len(run_ids)}


def import_database(base: Path, source: Path) -> int:
    """Import a ledger with SQLite's online backup API, never a live-file cp."""
    db, _, _, cfg, _ = paths(base)
    source = source.expanduser().resolve()
    if not source.is_file(): raise FileNotFoundError(source)
    if source == db.resolve(): raise ValueError("source ledger is already the active ledger")
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        backup_dir = j.abs_path(base, cfg.get("ledger", {}).get("backup_dir", "data/backups"))
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        target = backup_dir / f"jobs_before_import_{stamp}.sqlite3"
        current = sqlite3.connect(db); saved = sqlite3.connect(target)
        try: current.backup(saved)
        finally: saved.close(); current.close()
    src = sqlite3.connect(source); dst = sqlite3.connect(db)
    try:
        source_check = src.execute("PRAGMA integrity_check").fetchone()[0]
        if source_check != "ok":
            raise RuntimeError(f"source database integrity check failed: {source_check}")
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok": raise RuntimeError(f"imported database integrity check failed: {result}")
    finally:
        dst.close(); src.close()
    # Opening once performs additive migration and verifies the post-migration ledger.
    store = j.PrecisionStore(db); init_browser_schema(store.conn)
    check = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
    store.close()
    if check != "ok": raise RuntimeError(f"post-import integrity check failed: {check}")
    return 0


def show_status(base: Path, rid: int | None = None, verbose: bool = False) -> int:
    db, _, _, _, _ = paths(base)
    if not db.exists(): print("No ledger yet."); return 1
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row; init_browser_schema(conn)
    r=conn.execute("SELECT * FROM browser_runs ORDER BY browser_run_id DESC LIMIT 1").fetchone() if rid is None else conn.execute("SELECT * FROM browser_runs WHERE browser_run_id=?",(rid,)).fetchone()
    if not r: print("No browser run found."); conn.close(); return 1
    print(f"Browser run #{r['browser_run_id']} | {r['mode']} | {r['status']} | platforms={r['platform']}")
    print(f"Jobs recorded={r['jobs_recorded']} new={r['jobs_new']} updated={r['jobs_updated']} unchanged={r['jobs_unchanged']} | last progress={r['last_progress_at'] or 'never'}")
    for p in conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=? ORDER BY CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 ELSE 2 END",(r['browser_run_id'],)):
        print(f"  {p['platform']:<10} auth={p['auth_status']:<16} exhausted={p['tasks_completed']}/{p['tasks_total']} incomplete={p['tasks_incomplete']} challenged={p['tasks_challenged']} failed={p['tasks_failed']} jobs={p['jobs_recorded']}")
    counts=conn.execute("SELECT status,COUNT(*) n FROM browser_search_tasks WHERE browser_run_id=? GROUP BY status ORDER BY status",(r['browser_run_id'],)).fetchall()
    print("Tasks: " + ", ".join(f"{x['status']}={x['n']}" for x in counts))
    if verbose:
        for t in conn.execute("SELECT * FROM browser_search_tasks WHERE browser_run_id=? ORDER BY platform,priority,task_id",(r['browser_run_id'],)):
            print(f"  [{t['task_id']}] {t['platform']:<9} {t['status']:<14} pages={t['pages_visited']:<3} seen={t['results_seen']:<5} details={t['detail_count_read']:<4} saved={t['jobs_recorded']:<4} {t['query_text']}")
    conn.close(); return 0


def request_stop(base: Path, rid: int | None = None) -> int:
    db, _, _, _, _ = paths(base); conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row; init_browser_schema(conn)
    if rid is None:
        r=conn.execute("SELECT browser_run_id FROM browser_runs WHERE status IN ('queued','running') ORDER BY browser_run_id DESC LIMIT 1").fetchone()
        if not r: print("No active browser run."); conn.close(); return 1
        rid=int(r[0])
    conn.execute("UPDATE browser_runs SET stop_requested=1,notes=trim(notes || ' stop requested') WHERE browser_run_id=?",(rid,)); conn.commit(); conn.close()
    print(f"Stop requested for browser run #{rid}."); return 0


def emergency_stop(base: Path, rid: int | None = None) -> int:
    """Persist an immediate manual stop when the extension/bridge is unavailable."""
    db, _, _, _, _ = paths(base)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_browser_schema(conn)
    if rid is None:
        row = conn.execute(
            "SELECT browser_run_id FROM browser_runs WHERE status IN ('queued','running') ORDER BY browser_run_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No active browser run.")
            conn.close()
            return 1
        rid = int(row[0])
    row = conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (rid,)).fetchone()
    if not row:
        conn.close()
        print(f"No browser run found: {rid}")
        return 1
    now = j.now_iso()
    conn.execute(
        """UPDATE browser_search_tasks
           SET status='incomplete', completed_at=?, lease_owner='', lease_until=NULL,
               safety_stop_reason=CASE WHEN status='running' THEN 'manual_emergency_stop' ELSE safety_stop_reason END,
               last_error=CASE WHEN status='running' THEN 'manual emergency stop' ELSE last_error END,
               last_progress_at=?
           WHERE browser_run_id=? AND status='running'""",
        (now, now, rid),
    )
    conn.execute(
        """UPDATE browser_runs
           SET status='stopped', stop_requested=1, completed_at=?, current_task_id=NULL,
               tasks_incomplete=(SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='incomplete'),
               last_progress_at=?, last_error='manual emergency stop'
           WHERE browser_run_id=?""",
        (now, rid, now, rid),
    )
    conn.commit()
    conn.close()
    print(f"Emergency stop persisted for browser run #{rid}; unfinished tasks remain resumable.")
    return 0


def report(base: Path, rid: int | None = None) -> int:
    db,out,_,_,_=paths(base); conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row; init_browser_schema(conn)
    r=conn.execute("SELECT * FROM browser_runs ORDER BY browser_run_id DESC LIMIT 1").fetchone() if rid is None else conn.execute("SELECT * FROM browser_runs WHERE browser_run_id=?",(rid,)).fetchone()
    if not r: print("No browser run found."); conn.close(); return 1
    lines=[f"# JobBot v3 Platform-First Search Report — Run {r['browser_run_id']}","",f"- Mode: **{r['mode']}**",f"- Status: **{r['status']}**",f"- Platforms: **{r['platform']}**",f"- Jobs recorded: **{r['jobs_recorded']}** (new {r['jobs_new']}, updated {r['jobs_updated']}, unchanged {r['jobs_unchanged']})",f"- Last progress: **{r['last_progress_at'] or 'never'}**",f"- Human-readable log: `out/logs/run_{r['browser_run_id']}.log`","","## Platforms",""]
    for p in conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=? ORDER BY CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 ELSE 2 END",(r['browser_run_id'],)):
        lines += [f"### {p['platform'].title()}",f"- Authentication: {p['auth_status']}",f"- Tasks exhausted: {p['tasks_completed']} / {p['tasks_total']}",f"- Incomplete/safety stops: {p['tasks_incomplete']}",f"- Challenged: {p['tasks_challenged']}",f"- Failed: {p['tasks_failed']}",f"- Jobs recorded: {p['jobs_recorded']}",""]
    lines += ["## Task summary",""]
    for row in conn.execute("SELECT platform,status,COUNT(*) n,SUM(results_seen) seen,SUM(jobs_recorded) saved,SUM(pages_visited) pages FROM browser_search_tasks WHERE browser_run_id=? GROUP BY platform,status ORDER BY platform,status",(r['browser_run_id'],)):
        detail=conn.execute("SELECT COALESCE(SUM(detail_count_read),0) FROM browser_search_tasks WHERE browser_run_id=? AND platform=? AND status=?",(r['browser_run_id'],row['platform'],row['status'])).fetchone()[0]
        lines.append(f"- {row['platform']} / {row['status']}: {row['n']} task(s), pages={row['pages'] or 0}, results_seen={row['seen'] or 0}, details_read={detail}, jobs_recorded={row['saved'] or 0}")
    lines += ["","## Important","","> Result-count limits are not used in production mode. A task ends on platform/search exhaustion, age-boundary logic, challenge, failure, or explicit stop.",""]
    p=out/f"v3_run_{r['browser_run_id']}_report.md"; p.write_text("\n".join(lines),encoding="utf-8"); conn.close(); print(p); return 0


def wait_run(base: Path, rid: int, interval: float=5.0, timeout_minutes: int=1440) -> int:
    db,_,_,_,_=paths(base); deadline=time.time()+max(1,timeout_minutes)*60; last=None
    while time.time()<deadline:
        conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row; init_browser_schema(conn)
        r=conn.execute("SELECT * FROM browser_runs WHERE browser_run_id=?",(rid,)).fetchone()
        if not r: conn.close(); print("Run not found."); return 2
        counts=tuple((x['platform'],x['status'],x['n'],x['saved']) for x in conn.execute("SELECT platform,status,COUNT(*) n,SUM(jobs_recorded) saved FROM browser_search_tasks WHERE browser_run_id=? GROUP BY platform,status ORDER BY platform,status",(rid,)))
        snap=(r['status'],r['jobs_recorded'],r['jobs_new'],r['jobs_updated'],counts)
        if snap!=last:
            print(f"[run {rid}] {r['status']} | recorded={r['jobs_recorded']} new={r['jobs_new']} updated={r['jobs_updated']} unchanged={r['jobs_unchanged']}")
            for x in counts: print(f"  {x[0]:<10} {x[1]:<14} tasks={x[2]:<4} jobs={x[3] or 0}")
            last=snap
        final=r['status'] in {'completed','partial','stopped','failed'}; conn.close()
        if final: return 0 if r['status']=='completed' else 1
        time.sleep(max(.75,interval))
    print(f"Timed out waiting for run #{rid}. The run remains checkpointed and can be resumed.")
    return 3


def install_check(base: Path) -> int:
    ok=True
    print(f"Extension ID: {EXTENSION_ID}")
    print(f"Extension folder: {base/'extension'}")
    bridge=base/'jobbot_bridge.py'
    if not bridge.exists():
        print("  FAIL: loopback bridge missing")
        ok=False
    else:
        print(f"Loopback bridge: {bridge}")
    manifest=base/'extension'/'manifest.json'
    try:
        d=json.loads(manifest.read_text())
        if d.get('manifest_version')!=3: print("  FAIL: extension manifest is not MV3"); ok=False
        if 'http://127.0.0.1/*' not in d.get('host_permissions',[]): print("  FAIL: loopback host permission missing"); ok=False
        if 'nativeMessaging' in d.get('permissions',[]): print("  FAIL: obsolete nativeMessaging permission still present"); ok=False
    except Exception as e:
        print(f"  FAIL: invalid extension manifest: {e}")
        ok=False
    return 0 if ok else 1


def self_test(base: Path) -> int:
    import tempfile
    _,_,_,cfg,strategy=paths(base)
    bundle=load_bundle(base)
    tasks=compile_plan(bundle,'deep',list(PLATFORMS))
    identities={(t.platform,t.query.casefold(),t.age_days,t.remote_required) for t in tasks}
    required={(str(f['id']),p) for f in bundle.live_search.get('families',[]) if f.get('enabled',True) and f.get('minimum_deep_recall',False) for p in PLATFORMS}
    covered={(t.query_family,t.platform) for t in tasks}
    assert len(identities)==len(tasks)
    assert required<=covered,(required-covered)
    assert all(t.search_url.startswith('https://') for t in tasks)
    assert all(t.platform in PLATFORMS for t in tasks)
    assert all(t.strategy_profile and t.query_family and t.query_kind and t.query_pass for t in tasks)
    assert 'f_WT=2' in linkedin_search_url('patient access specialist',7)
    assert 'fromage=7' in indeed_search_url('patient access specialist',7)
    assert '/Job/remote-patient-access-specialist-jobs-' in glassdoor_search_url('patient access specialist',7)
    with tempfile.TemporaryDirectory() as td:
        p=Path(td); s=j.PrecisionStore(p/'jobs.sqlite3'); init_browser_schema(s.conn); now=j.now_iso()
        rid=int(s.conn.execute("INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)",(V3_VERSION,'test','indeed','running',now)).lastrowid)
        tid=int(s.conn.execute("INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,status,created_at) VALUES(?,?,?,?,?,?,?)",(rid,'indeed','patient enrollment specialist',7,indeed_search_url('patient enrollment specialist',7),'running',now)).lastrowid)
        job=j.Job(source_site='indeed',source_job_id='abc123',canonical_url='https://www.indeed.com/viewjob?jk=abc123',apply_url='https://www.indeed.com/viewjob?jk=abc123',title='Patient Enrollment Specialist',company='Example Health',location_raw='Remote',remote_status='remote',employment_type='Full-time',posted_at=now,description='Remote healthcare patient enrollment and onboarding. Required Qualifications: 2 years relevant experience. HIPAA documentation and Excel.',raw={'browser_task_id':tid})
        setattr(job,'_mode','deep'); j.score_job(job,strategy,cfg.get('candidate',{})); a=s.upsert(job); b=s.upsert(job); job.description+=' Updated workflow documentation.'; setattr(job,'_mode','deep'); j.score_job(job,strategy,cfg.get('candidate',{})); c=s.upsert(job)
        assert (a,b,c)==('new','unchanged','updated'),(a,b,c); s.close()
    print(f"V3 SELF-TEST PASSED — {len(required) // len(PLATFORMS)} configured families have deep recall on all three platforms")
    return 0


def main() -> int:
    ap=argparse.ArgumentParser(description='JobBot v3 normal-Chrome platform-first controller')
    sub=ap.add_subparsers(dest='cmd',required=True)
    q=sub.add_parser('enqueue-production'); q.add_argument('--mode',choices=['fast','deep'],default='deep'); q.add_argument('--platform',action='append',choices=list(PLATFORMS),help='Repeat to select platforms; default all three')
    g=sub.add_parser('enqueue-acceptance'); g.add_argument('--platform',choices=list(PLATFORMS),default='indeed'); g.add_argument('--days',type=int,default=7); g.add_argument('--max-results',type=int,default=20)
    s=sub.add_parser('status'); s.add_argument('--run-id',type=int); s.add_argument('--verbose',action='store_true')
    x=sub.add_parser('stop'); x.add_argument('--run-id',type=int)
    e=sub.add_parser('emergency-stop'); e.add_argument('--run-id',type=int)
    r=sub.add_parser('report'); r.add_argument('--run-id',type=int)
    w=sub.add_parser('wait'); w.add_argument('--run-id',type=int,required=True); w.add_argument('--interval',type=float,default=5.0); w.add_argument('--timeout-minutes',type=int,default=1440)
    sub.add_parser('install-check'); sub.add_parser('self-test'); sub.add_parser('resume-run')
    imp=sub.add_parser('import-db'); imp.add_argument('source')
    args=ap.parse_args(); base=base_dir()
    if args.cmd=='enqueue-production': rid=enqueue_production(base,args.mode,args.platform); print(rid); return 0
    if args.cmd=='enqueue-acceptance': rid=enqueue_gate(base,args.platform,max(1,args.days),max(1,min(200,args.max_results))); print(rid); return 0
    if args.cmd=='status': return show_status(base,args.run_id,args.verbose)
    if args.cmd=='stop': return request_stop(base,args.run_id)
    if args.cmd=='emergency-stop': return emergency_stop(base,args.run_id)
    if args.cmd=='report': return report(base,args.run_id)
    if args.cmd=='wait': return wait_run(base,args.run_id,args.interval,args.timeout_minutes)
    if args.cmd=='install-check': return install_check(base)
    if args.cmd=='self-test': return self_test(base)
    if args.cmd=='resume-run':
        rid=resume_run(base); print(rid); return 0
    if args.cmd=='import-db':
        import_database(base,Path(args.source)); print(f"Imported ledger safely into {paths(base)[0]}"); return 0
    return 2

if __name__=='__main__': raise SystemExit(main())
