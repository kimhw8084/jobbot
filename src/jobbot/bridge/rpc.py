#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, load_bundle
from .. import legacy_engine as j
from .. import browser_tasks as v3
from .. import crawl_observations
from .. import diagnostics
from ..discoveries import block_detail, claim_next_detail, fail_detail, finish_detail, upsert_card
from ..extension_identity import extension_build as expected_extension_build
from ..runtime_binding import deployment_diagnostics
from ..strategy_runtime import fallback_activation_enabled, with_fallback_activation
from ..platform_state import human_waiting, runnable_resume

BASE = PROJECT_ROOT
j.VERSION=v3.V3_VERSION; j.c.VERSION=v3.V3_VERSION

AUTH_STATES = {'unchecked', 'verified', 'not_authenticated', 'unknown'}
READINESS_STATES = {'unchecked', 'verified', 'resumed', 'sign_in_required', 'challenged_cooldown', 'retryable', 'unknown', 'unverified', 'user_action_required'}


def log(msg:str)->None: print(f"[jobbot-rpc] {msg}",file=sys.stderr,flush=True)

def lease_time(seconds:int=180)->str:
    return (datetime.now(timezone.utc)+timedelta(seconds=max(1,int(seconds)))).isoformat(timespec='seconds')

def open_store():
    db,out,_,cfg,strategy=v3.paths(BASE);store=j.PrecisionStore(db);v3.init_browser_schema(store.conn)
    strategy=with_fallback_activation(strategy, fallback_activation_enabled(store.conn, cfg))
    return store,cfg,strategy,out


def _cache_bundle():
    return load_bundle(BASE)


def _safe_source_build() -> str:
    """Return cache provenance even when an isolated test root has no extension."""
    try:
        return expected_extension_build(BASE)
    except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError):
        return v3.V3_VERSION


def _control_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    value = {key: row[key] for key in row.keys()}
    try:
        value["result"] = json.loads(value.pop("result_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value["result"] = {}
    return value


def _control_request(conn, msg: dict[str, Any], out: Path) -> dict[str, Any]:
    rid = int(msg.get("run_id") or 0)
    platform = j.clean_text(msg.get("platform") or "")
    action = j.clean_text(msg.get("control_action") or msg.get("action_name") or msg.get("requested_action") or "").lower()
    request_id = j.clean_text(msg.get("control_request_id") or msg.get("request_id") or "")
    allowed = {"focus_window", "stop_after_current", "resume_platform", "recheck", "emergency_stop", "stop_all", "resume_ready_platforms"}
    if not rid or get_run(conn, rid) is None:
        return {"ok": False, "error": "run_not_found"}
    if action not in allowed:
        return {"ok": False, "error": "unsupported_control_action", "action": action}
    if platform and platform not in v3.PLATFORMS:
        return {"ok": False, "error": "unsupported_platform", "platform": platform}
    platform_row = conn.execute(
        "SELECT * FROM browser_platform_runs WHERE browser_run_id=? AND platform=?", (rid, platform),
    ).fetchone() if platform else None
    if action == "resume_platform" and platform_row is not None and human_waiting(platform_row):
        return {"ok": False, "error": "human_wait_requires_recheck", "platform": platform}
    if not request_id:
        request_id = f"bridge:{uuid.uuid4()}"
    now = j.now_iso()
    existing = conn.execute("SELECT * FROM control_requests WHERE request_id=?", (request_id,)).fetchone()
    if existing is not None:
        if int(existing["browser_run_id"]) != rid or str(existing["platform"] or "") != platform or str(existing["action"]) != action:
            return {"ok": False, "error": "control_request_conflict", "request_id": request_id}
        return {"ok": True, "deduplicated": True, "control": _control_row(existing)}
    target_worker_id = str(platform_row["worker_id"] or "") if platform_row is not None else ""
    target_worker_generation = int(platform_row["worker_generation"] or 0) if platform_row is not None else 0
    conn.execute("""INSERT INTO control_requests(
      request_id,browser_run_id,platform,action,status,requested_at,requested_by,
      target_worker_id,target_worker_generation
    ) VALUES(?,?,?,?,?,?,?,?,?)""", (request_id, rid, platform, action, "PENDING", now, j.clean_text(msg.get("requested_by") or "dashboard"), target_worker_id, target_worker_generation))
    if platform:
        if action == "stop_after_current":
            conn.execute("UPDATE browser_platform_runs SET stop_after_current=1,last_control_at=?,last_control_action=? WHERE browser_run_id=? AND platform=?", (now, action, rid, platform))
        elif action == "emergency_stop":
            conn.execute("UPDATE browser_platform_runs SET emergency_stop=1,last_control_at=?,last_control_action=? WHERE browser_run_id=? AND platform=?", (now, action, rid, platform))
        elif action in {"resume_platform", "recheck"}:
            conn.execute("UPDATE browser_platform_runs SET stop_after_current=0,emergency_stop=0,worker_status='rechecking',interaction_state='RECHECKING',readiness_state='unchecked',readiness_reason='',last_checked_at=?,last_control_at=?,last_control_action=? WHERE browser_run_id=? AND platform=?", (now, now, action, rid, platform))
            conn.execute("""UPDATE browser_search_tasks SET status='queued',completed_at=NULL,last_error='',lease_owner='',worker_id='',lease_until=NULL
              WHERE browser_run_id=? AND platform=? AND status IN ('challenged','deferred_by_platform','auth_required')""", (rid, platform))
            conn.execute("UPDATE browser_runs SET status='running',completed_at=NULL,stop_requested=0,last_progress_at=? WHERE browser_run_id=?", (now, rid))
        elif action == "focus_window":
            conn.execute("UPDATE browser_platform_runs SET focus_requested_at=?,last_control_at=?,last_control_action=? WHERE browser_run_id=? AND platform=?", (now, now, action, rid, platform))
    elif action in {"stop_all", "stop_after_current"}:
        conn.execute("UPDATE browser_runs SET stop_after_current=1,last_error=?,last_progress_at=? WHERE browser_run_id=?", ("stop after current requested from dashboard", now, rid))
    elif action == "emergency_stop":
        conn.execute("""UPDATE browser_search_tasks SET status='incomplete',completed_at=?,lease_owner='',lease_until=NULL,
            safety_stop_reason='manual_emergency_stop',last_error='manual emergency stop',last_progress_at=?
            WHERE browser_run_id=? AND status='running'""", (now, now, rid))
        conn.execute("""UPDATE browser_runs SET status='stopped',stop_requested=1,stop_after_current=0,completed_at=?,current_task_id=NULL,
            tasks_incomplete=(SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='incomplete'),last_error=?,last_progress_at=?
            WHERE browser_run_id=?""", (now, rid, "manual emergency stop", now, rid))
        conn.execute("UPDATE browser_platform_runs SET emergency_stop=1,stop_after_current=0,worker_status='stopped',last_control_at=?,last_control_action=? WHERE browser_run_id=?", (now, action, rid))
    event(conn, rid, None, "control_requested", f"{platform or 'global'}: {action}", {"request_id": request_id, "platform": platform, "action": action}, out)
    conn.commit()
    row = conn.execute("SELECT * FROM control_requests WHERE request_id=?", (request_id,)).fetchone()
    return {"ok": True, "deduplicated": False, "control": _control_row(row)}


def _consume_controls(conn, msg: dict[str, Any], out: Path) -> dict[str, Any]:
    rid = int(msg.get("run_id") or 0)
    platform = j.clean_text(msg.get("platform") or "")
    worker_id = j.clean_text(msg.get("worker_id") or "")
    candidates = conn.execute("""SELECT c.* FROM control_requests c
      WHERE browser_run_id=? AND status='PENDING' AND (platform=? OR platform='')
      ORDER BY control_id""", (rid, platform)).fetchall()
    row = None
    current = conn.execute("SELECT worker_generation FROM browser_platform_runs WHERE browser_run_id=? AND platform=?", (rid, platform)).fetchone()
    generation = int(current["worker_generation"] or 0) if current is not None else 0
    supplied_generation = int(msg.get("worker_generation") or 0)
    runnable_platforms = set(runnable_resume(conn, rid)["runnable_platforms"])
    for candidate in candidates:
        if str(candidate["action"]) == "resume_ready_platforms":
            if platform not in runnable_platforms or (supplied_generation and supplied_generation != generation):
                continue
        row = candidate
        break
    if row is None:
        return {"ok": True, "control": None}
    now = j.now_iso()
    conn.execute("""UPDATE control_requests SET delivered_at=?,target_worker_id=?,target_worker_generation=?
       WHERE control_id=? AND status='PENDING'""", (now, worker_id, generation, int(row["control_id"])))
    row = conn.execute("SELECT * FROM control_requests WHERE control_id=?", (int(row["control_id"]),)).fetchone()
    event(conn, rid, None, "control_delivered", f"{platform or 'global'}: {row['action']}", {"control_id": row["control_id"], "request_id": row["request_id"], "worker_id": worker_id, "worker_generation": generation}, out)
    conn.commit()
    return {"ok": True, "control": _control_row(row)}


def _ack_control(conn, msg: dict[str, Any], out: Path) -> dict[str, Any]:
    rid = int(msg.get("run_id") or 0)
    platform = j.clean_text(msg.get("platform") or "")
    request_id = j.clean_text(msg.get("request_id") or "")
    worker_id = j.clean_text(msg.get("worker_id") or "")
    status = j.clean_text(msg.get("status") or "ACKNOWLEDGED").upper()
    row = conn.execute("SELECT * FROM control_requests WHERE request_id=? AND browser_run_id=? AND (platform=? OR platform='')", (request_id, rid, platform)).fetchone()
    if row is None:
        return {"ok": False, "error": "stale_control_response"}
    if str(row["status"]) != "PENDING":
        return {"ok": True, "deduplicated": True, "control": _control_row(row)}
    if str(row["action"]) == "resume_ready_platforms":
        eligible = set(runnable_resume(conn, rid)["runnable_platforms"])
        if platform not in eligible:
            return {"ok": False, "error": "inapplicable_global_resume"}
    current = conn.execute("SELECT worker_id,worker_generation FROM browser_platform_runs WHERE browser_run_id=? AND platform=?", (rid, platform)).fetchone() if platform else None
    expected_worker = str(row["target_worker_id"] or "")
    expected_generation = int(row["target_worker_generation"] or 0)
    incoming_generation = int(msg.get("worker_generation") or 0)
    if current is not None and (
        (expected_worker and worker_id and expected_worker != worker_id)
        or (expected_generation and int(current["worker_generation"] or 0) != expected_generation)
        or (incoming_generation and expected_generation and incoming_generation != expected_generation)
        or (str(current["worker_id"] or "") and worker_id and str(current["worker_id"]) != worker_id)
    ):
        return {"ok": False, "error": "stale_control_response", "request_id": request_id}
    now = j.now_iso()
    result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
    conn.execute("UPDATE control_requests SET status=?,acknowledged_at=?,worker_id=?,result_json=? WHERE control_id=? AND status='PENDING'", (status if status in {"ACKNOWLEDGED", "REJECTED"} else "ACKNOWLEDGED", now, worker_id, json.dumps(result, ensure_ascii=False), int(row["control_id"])))
    if platform:
        conn.execute("UPDATE browser_platform_runs SET last_control_at=?,last_control_action=? WHERE browser_run_id=? AND platform=?", (now, str(row["action"]), rid, platform))
    event(conn, rid, None, "control_acknowledged", f"{platform or 'global'}: {row['action']}", {"request_id": request_id, "worker_id": worker_id, "status": status, "result": result}, out)
    conn.commit()
    fresh = conn.execute("SELECT * FROM control_requests WHERE control_id=?", (int(row["control_id"]),)).fetchone()
    return {"ok": True, "control": _control_row(fresh)}

def run_log(out:Path,run_id:int|None,message:str)->None:
    if not run_id:return
    try:
        p=out/'logs'/f'run_{run_id}.log'; p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('a',encoding='utf-8') as f:f.write(f"{j.now_iso()} {j.clean_text(message)}\n")
    except Exception as e: log(f'run log warning: {e}')

def event(conn,run_id,task_id,typ,msg='',payload=None,out=None):
    now=j.now_iso(); clean=j.clean_text(msg); data=payload or {}
    conn.execute("INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)",(run_id,task_id,now,typ,clean,json.dumps(data,ensure_ascii=False)))
    run_log(out,run_id,f"event={typ} task={task_id or '-'} {clean}")

def get_run(conn,rid):return conn.execute("SELECT * FROM browser_runs WHERE browser_run_id=?",(rid,)).fetchone()

def _refresh_value(row):
    if row is None:
        return None
    try:
        diagnostics = json.loads(row['diagnostics_json'] or '{}')
    except (AttributeError, TypeError, json.JSONDecodeError):
        diagnostics = {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    confirmed = (
        str(row['status']) == 'confirmed'
        and str(row['observed_build']) == str(row['expected_build'])
        and diagnostics.get('classification') == 'intended_extension_reachable_and_current'
    )
    return {
        'refresh_id': str(row['refresh_id']), 'browser_run_id': row['browser_run_id'],
        'expected_build': str(row['expected_build']), 'status': str(row['status']),
        'requested_at': row['requested_at'], 'confirmed_at': row['confirmed_at'],
        'observed_build': str(row['observed_build'] or ''),
        'last_error': str(row['last_error'] or ''), 'reload_count': int(row['reload_count'] or 0),
        'observed_source_identity': str(row['observed_source_identity'] or ''),
        'observed_deployment_root': str(row['observed_deployment_root'] or ''),
        'diagnostics': diagnostics,
        'identity_confirmed': confirmed, 'refreshed': confirmed,
    }

def _refresh_response(row, **extra):
    value = _refresh_value(row) or {}
    return {'ok': True, **value, **extra}

def _active_run(conn, requested_run_id: int | None):
    if requested_run_id:
        requested = get_run(conn, requested_run_id)
        if requested is None:
            return None, 'run_not_found'
        if str(requested['status']) == 'running':
            return requested, 'active_run'
        other = conn.execute(
            "SELECT * FROM browser_runs WHERE status='running' AND browser_run_id<>? ORDER BY browser_run_id DESC LIMIT 1",
            (requested_run_id,),
        ).fetchone()
        return (other, 'active_run') if other is not None else (None, '')
    active = conn.execute(
        "SELECT * FROM browser_runs WHERE status='running' ORDER BY browser_run_id DESC LIMIT 1"
    ).fetchone()
    return (active, 'active_run') if active is not None else (None, '')


def _runtime_diagnostics(build: str, expected: str, observed: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    deployment = deployment_diagnostics(BASE)
    expected_deployment = deployment.get('expected', {})
    build_ok = bool(build) and build == expected
    source_ok = bool(
        deployment.get('ok')
        and observed.get('extension_id') == expected_deployment.get('extension_id')
        and observed.get('version_name') == expected
        and observed.get('source_identity') == expected_deployment.get('source_identity')
        and observed.get('deployed_extension_root') == expected_deployment.get('extension_root')
    )
    if build_ok and source_ok:
        classification = 'intended_extension_reachable_and_current'
    elif not build_ok:
        classification = 'wrong_or_stale_build'
    else:
        classification = 'bootstrap_or_deployment_source_mismatch'
    diagnostics = {
        'classification': classification,
        'expected_build': expected,
        'observed_build': build,
        'expected_source_identity': expected_deployment.get('source_identity', ''),
        'observed_source_identity': str(observed.get('source_identity') or ''),
        'expected_extension_root': expected_deployment.get('extension_root', ''),
        'observed_deployment_root': str(observed.get('deployed_extension_root') or ''),
        'deployment': deployment,
    }
    return build_ok and source_ok, classification, diagnostics

def _refresh_request(conn, msg, out):
    expected = j.clean_text(msg.get('expected_build') or '')
    actual_expected = expected_extension_build(BASE)
    if not expected:
        return {'ok': False, 'error': 'expected_build_missing', 'expected_build': actual_expected}
    if expected != actual_expected:
        return {'ok': False, 'error': 'expected_build_mismatch', 'expected_build': actual_expected}
    requested_run_id = int(msg.get('run_id') or 0) or None
    refresh_id = j.clean_text(msg.get('refresh_id') or msg.get('request_id') or '')
    if not refresh_id:
        refresh_id = f"refresh-{secrets.token_urlsafe(18)}"
    existing = conn.execute(
        "SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)
    ).fetchone()
    if existing is not None:
        if str(existing['expected_build']) != expected or (requested_run_id and existing['browser_run_id'] not in (None, requested_run_id)):
            return {'ok': False, 'error': 'refresh_id_conflict', 'refresh_id': refresh_id}
        active, reason = _active_run(conn, requested_run_id)
        if reason == 'active_run' and str(existing['status']) not in {'confirmed', 'failed'}:
            return {'ok': False, 'error': reason, 'active_run_id': int(active['browser_run_id'])}
        response = _refresh_response(existing, requested=True)
        if str(existing['status']) == 'failed':
            response['ok'] = False
        return response
    active, reason = _active_run(conn, requested_run_id)
    if reason == 'run_not_found':
        return {'ok': False, 'error': reason}
    if reason == 'active_run':
        return {'ok': False, 'error': reason, 'active_run_id': int(active['browser_run_id'])}
    now = j.now_iso()
    # One outstanding request per run/build makes retries from a dashboard or
    # a restarted bridge idempotent even when callers use different request IDs.
    duplicate = conn.execute(
        "SELECT * FROM extension_refresh_requests WHERE expected_build=? AND browser_run_id IS ? "
        "AND status IN ('pending','reloading','confirmed') ORDER BY requested_at DESC LIMIT 1",
        (expected, requested_run_id),
    ).fetchone()
    if duplicate is not None:
        return _refresh_response(duplicate, requested=True, deduplicated=True)
    conn.execute(
        "INSERT INTO extension_refresh_requests(refresh_id,browser_run_id,expected_build,status,requested_at) VALUES(?,?,?,?,?)",
        (refresh_id, requested_run_id, expected, 'pending', now),
    )
    event(conn, requested_run_id, None, 'extension_refresh_requested', f'refresh {refresh_id} requested for {expected}',
          {'refresh_id': refresh_id, 'expected_build': expected}, out)
    conn.commit()
    row = conn.execute("SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)).fetchone()
    return _refresh_response(row, requested=True)

def _report_extension_build(conn, msg, out):
    build = j.clean_text(msg.get('build') or '')
    expected = expected_extension_build(BASE)
    requested_expected = j.clean_text(msg.get('expected_build') or '')
    if requested_expected and requested_expected != expected:
        return {'ok': False, 'error': 'expected_build_mismatch', 'expected_build': expected, 'build': build}
    rid = int(msg.get('run_id') or 0) or None
    if rid and get_run(conn, rid) is None:
        return {'ok': False, 'error': 'run_not_found', 'build': build, 'expected_build': expected}
    refresh_id = j.clean_text(msg.get('refresh_id') or '')
    row = conn.execute(
        "SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)
    ).fetchone() if refresh_id else None
    observed_deployment = msg.get('deployment_identity')
    if not isinstance(observed_deployment, dict):
        observed_deployment = {}
    valid, classification, diagnostics = _runtime_diagnostics(build, expected, observed_deployment)
    now = j.now_iso()
    if row is not None:
        if valid and str(row['expected_build']) == expected:
            conn.execute(
                "UPDATE extension_refresh_requests SET status='confirmed',confirmed_at=?,observed_build=?,last_error='',observed_source_identity=?,observed_deployment_root=?,diagnostics_json=?,reload_count=reload_count+? WHERE refresh_id=?",
                (now, build, observed_deployment.get('source_identity', ''), observed_deployment.get('deployed_extension_root', ''),
                 json.dumps(diagnostics, ensure_ascii=False), 1 if str(row['status']) == 'reloading' else 0, refresh_id),
            )
        else:
            conn.execute(
                "UPDATE extension_refresh_requests SET status='failed',observed_build=?,last_error=?,observed_source_identity=?,observed_deployment_root=?,diagnostics_json=? WHERE refresh_id=?",
                (build, 'stale_or_wrong_extension_build' if classification == 'wrong_or_stale_build' else classification,
                 observed_deployment.get('source_identity', ''), observed_deployment.get('deployed_extension_root', ''),
                 json.dumps(diagnostics, ensure_ascii=False), refresh_id),
            )
    elif refresh_id and valid:
        conn.execute(
            "INSERT INTO extension_refresh_requests(refresh_id,browser_run_id,expected_build,status,requested_at,confirmed_at,observed_build,observed_source_identity,observed_deployment_root,diagnostics_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (refresh_id, rid, expected, 'confirmed', now, now, build, observed_deployment.get('source_identity', ''),
             observed_deployment.get('deployed_extension_root', ''), json.dumps(diagnostics, ensure_ascii=False)),
        )
    event(conn, rid, None, 'extension_build', build or 'unknown',
          {'build': build, 'expected_build': expected, 'refresh_id': refresh_id,
           'identity_confirmed': valid, 'diagnostics': diagnostics}, out)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)
    ).fetchone() if refresh_id else None
    response = _refresh_response(row) if row is not None else {
        'ok': valid, 'build': build, 'expected_build': expected,
        'identity_confirmed': valid, 'refreshed': False,
    }
    response.update({'ok': valid and bool(response.get('ok', True)), 'build': build, 'expected_build': expected})
    if not valid:
        response['error'] = 'stale_or_wrong_extension_build' if classification == 'wrong_or_stale_build' else classification
    return response

def platform_order_sql()->str:return "CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 WHEN 'glassdoor' THEN 2 ELSE 99 END"

def phase_order_sql()->str:return "CASE phase WHEN 'A_FASTEST_DOOR_RECENT' THEN 0 WHEN 'B_REMAINING_CORE_RECENT' THEN 1 WHEN 'C_DEEP_BACKFILL' THEN 2 ELSE 9 END"


def _auth_state(msg: dict[str, Any], authenticated: bool, reason: str) -> str:
    explicit = j.clean_text(msg.get('auth_state') or '').lower()
    if authenticated or explicit == 'verified':
        return 'verified'
    if explicit in AUTH_STATES - {'unchecked', 'verified'}:
        return explicit
    lowered = reason.lower()
    if any(value in lowered for value in ('sign in', 'sign-in', 'log in', 'log-in', 'login', 'not authenticated', 'authentication required')):
        return 'not_authenticated'
    return 'unknown'


def _overall_auth_state(states: list[str]) -> str:
    if not states:
        return 'unchecked'
    if all(value == 'verified' for value in states):
        return 'verified'
    if any(value == 'verified' for value in states):
        return 'partial'
    if any(value == 'not_authenticated' for value in states):
        return 'not_authenticated'
    if any(value == 'unknown' for value in states):
        return 'unknown'
    return 'unchecked'


def _recall_decision(
    strategy: dict[str, Any], platform: str, title: str, source_job_id: str, source_url: str,
    query_family: str = "",
) -> tuple[bool, bool, str, int]:
    """Use the existing high-recall prefilter only for enrichment ordering."""
    configured_families = {
        str(family.get("id")) for family in strategy.get("_live_search_profile", {}).get("families", [])
        if family.get("enabled", True) and family.get("minimum_deep_recall", False)
    }
    probe = j.Job(source_site=platform, source_job_id=source_job_id,
                  canonical_url=source_url, title=title, remote_status="unknown")
    selected, reason = j.recall_prefilter(probe, strategy)
    sample_key = f"{platform}|{source_job_id}|{source_url}".encode("utf-8", errors="ignore")
    qa_sample = int(hashlib.sha256(sample_key).hexdigest()[:8], 16) % 20 == 0
    if query_family and query_family in configured_families:
        if reason in {"excluded occupation family", "explicit out-of-scope title"}:
            return False, bool(qa_sample), j.clean_text(reason), 1 if qa_sample else 2
        return True, False, f"active live-search family recall: {query_family}", 0
    return bool(selected), bool(qa_sample), j.clean_text(reason), 0 if selected else (1 if qa_sample else 2)


def _pending_detail_count(conn, task_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM search_task_results WHERE task_id=? "
        "AND detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED','DEFERRED_RECALL','PARTIAL')",
        (task_id,),
    ).fetchone()[0] or 0)


def _unsafe_detail_reason(detail: dict[str, Any], title: str, page_url: str) -> str:
    surface = j.clean_text(detail.get("page_type") or detail.get("surface") or "").lower()
    haystack = " ".join((surface, title, page_url, j.clean_text(detail.get("challenge_reason") or ""))).lower()
    markers = (
        "tunnel connection failed", "could not establish connection", "receiving end does not exist",
        "page load timed out", "sign in", "log in", "login", "checkpoint", "authwall",
        "captcha", "challenge", "access denied", "security check", "temporarily unavailable",
    )
    if surface in {"error", "login", "challenge", "interstitial"}:
        return surface
    hit = next((marker for marker in markers if marker in haystack), "")
    return hit

def refresh_task_counters(conn, rid:int)->None:
    """Derive status counters from durable task rows after resume/state changes."""
    counts=conn.execute("""SELECT
      COALESCE(SUM(status='exhausted'),0) completed,
      COALESCE(SUM(status='incomplete'),0) incomplete,
      COALESCE(SUM(status='challenged'),0) challenged
      FROM browser_search_tasks WHERE browser_run_id=?""",(rid,)).fetchone()
    conn.execute("UPDATE browser_runs SET tasks_completed=?,tasks_incomplete=?,tasks_challenged=? WHERE browser_run_id=?",
        (counts['completed'],counts['incomplete'],counts['challenged'],rid))
    platforms=conn.execute("SELECT platform FROM browser_platform_runs WHERE browser_run_id=?",(rid,)).fetchall()
    for row in platforms:
        platform=row['platform']
        values=conn.execute("""SELECT
          COALESCE(SUM(status='exhausted'),0) completed,
          COALESCE(SUM(status='incomplete'),0) incomplete,
          COALESCE(SUM(status='challenged'),0) challenged,
          COALESCE(SUM(status='failed'),0) failed
          FROM browser_search_tasks WHERE browser_run_id=? AND platform=?""",(rid,platform)).fetchone()
        conn.execute("""UPDATE browser_platform_runs
          SET tasks_completed=?,tasks_incomplete=?,tasks_challenged=?,tasks_failed=?
          WHERE browser_run_id=? AND platform=?""",
          (values['completed'],values['incomplete'],values['challenged'],values['failed'],rid,platform))

def refresh_result_reconciliation(conn, rid:int, tid:int)->None:
    """Derive card/detail counters from committed SQLite state.

    Older unpacked extension workers can persist cards while omitting the
    newer card_stats checkpoint payload. The bridge is the authoritative write
    boundary, so dashboard reconciliation cannot depend on that payload.
    Client-reported counters remain monotonic maxima; pending work is derived
    directly because it can decrease as details complete.
    """
    if not tid:
        return
    unique_cards = int(conn.execute(
        "SELECT COUNT(*) FROM search_task_results WHERE browser_run_id=? AND task_id=?",
        (rid, tid),
    ).fetchone()[0] or 0)
    failed_persistence = int(conn.execute(
        "SELECT COUNT(*) FROM browser_events WHERE browser_run_id=? AND task_id=? AND event_type='result_persistence_failed'",
        (rid, tid),
    ).fetchone()[0] or 0)
    duplicate_cards = int(conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN sighting_count>1 THEN sighting_count-1 ELSE 0 END),0) FROM search_task_results WHERE browser_run_id=? AND task_id=?",
        (rid, tid),
    ).fetchone()[0] or 0)
    pending_details = int(conn.execute(
        "SELECT COUNT(*) FROM search_task_results WHERE browser_run_id=? AND task_id=? "
        "AND detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED','DEFERRED_RECALL','PARTIAL')",
        (rid, tid),
    ).fetchone()[0] or 0)
    detail_failures = int(conn.execute(
        "SELECT COUNT(*) FROM browser_events WHERE browser_run_id=? AND task_id=? AND event_type='job_error'",
        (rid, tid),
    ).fetchone()[0] or 0)
    conn.execute(
        """UPDATE browser_search_tasks SET
             cards_extracted=MAX(cards_extracted,?),
             cards_persistence_attempted=MAX(cards_persistence_attempted,?+?),
             cards_persistence_succeeded=MAX(cards_persistence_succeeded,?),
             cards_persistence_failed=MAX(cards_persistence_failed,?),
             duplicate_cards=MAX(duplicate_cards,?),
             pending_details=?,
             details_failed=MAX(details_failed,?)
           WHERE browser_run_id=? AND task_id=?""",
        (unique_cards, unique_cards, failed_persistence, unique_cards, failed_persistence,
         duplicate_cards, pending_details, detail_failures, rid, tid),
    )

def handle(msg:dict[str,Any])->dict[str,Any]:
    action=str(msg.get('action') or '')
    store,cfg,strategy,out=open_store();conn=store.conn
    try:
        if action=='ping': return {'ok':True,'version':v3.V3_VERSION,'bridge':'loopback'}
        if action in {'request_control','control_request'}:
            return _control_request(conn, msg, out)
        if action in {'consume_control','next_control'}:
            return _consume_controls(conn, msg, out)
        if action in {'ack_control','acknowledge_control'}:
            return _ack_control(conn, msg, out)
        if action=='observation_stats':
            return {'ok':True, **crawl_observations.stats(_cache_bundle())}
        if action=='runtime_config':
            runtime=cfg.get('runtime',{})
            return {'ok':True,'heartbeat_seconds':int(runtime.get('heartbeat_seconds',20) or 20),
                    'lease_seconds':int(runtime.get('lease_seconds',180) or 180),
                    'watchdog_stall_seconds':int(runtime.get('watchdog_stall_seconds',180) or 180)}
        if action=='extension_refresh':
            return _refresh_request(conn, msg, out)
        if action=='extension_refresh_reloading':
            refresh_id=j.clean_text(msg.get('refresh_id') or '')
            row=conn.execute("SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)).fetchone()
            if row is None:return {'ok':False,'error':'refresh_not_found','refresh_id':refresh_id}
            if str(row['status']) == 'confirmed':return _refresh_response(row)
            if str(row['status']) == 'failed':return {'ok':False,**(_refresh_value(row) or {}), 'error':str(row['last_error'] or 'refresh_failed')}
            active, reason = _active_run(conn, int(row['browser_run_id'] or 0) or None)
            if reason == 'active_run':return {'ok':False,'error':reason,'active_run_id':int(active['browser_run_id'])}
            conn.execute("UPDATE extension_refresh_requests SET status='reloading',last_error='' WHERE refresh_id=?", (refresh_id,))
            event(conn, row['browser_run_id'], None, 'extension_refresh_reloading', f'refresh {refresh_id} invoking chrome.runtime.reload()',
                  {'refresh_id': refresh_id, 'expected_build': row['expected_build']}, out)
            conn.commit()
            row=conn.execute("SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)).fetchone()
            return _refresh_response(row)
        if action=='extension_refresh_failed':
            refresh_id=j.clean_text(msg.get('refresh_id') or '')
            reason=j.clean_text(msg.get('error') or 'extension refresh did not confirm the expected build')
            row=conn.execute("SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)).fetchone()
            if row is None:return {'ok':False,'error':'refresh_not_found','refresh_id':refresh_id}
            if str(row['status']) == 'confirmed':return _refresh_response(row)
            if str(row['status']) != 'confirmed':
                classification = {
                    'extension_unavailable_or_unreachable': 'extension_absent_disabled_or_unavailable',
                    'bridge_auth_or_configuration_failure': 'bridge_auth_or_configuration_failure',
                    'chrome_profile_binding_required': 'wrong_or_untargeted_chrome_profile_or_instance',
                }.get(reason, reason)
                diagnostics = {
                    'classification': classification,
                    'error': reason,
                    'expected_build': str(row['expected_build']),
                }
                conn.execute("UPDATE extension_refresh_requests SET status='failed',last_error=?,diagnostics_json=? WHERE refresh_id=?",
                             (reason, json.dumps(diagnostics, ensure_ascii=False), refresh_id))
                event(conn, row['browser_run_id'], None, 'extension_refresh_failed', reason,
                      {'refresh_id': refresh_id, 'expected_build': row['expected_build'], 'diagnostics': diagnostics}, out)
                conn.commit()
                row=conn.execute("SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)).fetchone()
            return {'ok':False,**(_refresh_value(row) or {}), 'error':reason}
        if action=='extension_refresh_status':
            refresh_id=j.clean_text(msg.get('refresh_id') or '')
            row=conn.execute("SELECT * FROM extension_refresh_requests WHERE refresh_id=?", (refresh_id,)).fetchone()
            if row is None:return {'ok':False,'error':'refresh_not_found','refresh_id':refresh_id}
            return _refresh_response(row)
        if action=='extension_build':
            return _report_extension_build(conn, msg, out)
        if action=='begin_run':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            if int(r['stop_requested'] or 0):return {'ok':False,'error':'stop_requested'}
            refresh=conn.execute(
                "SELECT * FROM extension_refresh_requests WHERE browser_run_id=? ORDER BY requested_at DESC LIMIT 1", (rid,)
            ).fetchone()
            if refresh is not None and not _refresh_value(refresh)['identity_confirmed']:
                return {'ok':False,'error':'extension_build_unconfirmed','refresh_id':refresh['refresh_id'],
                        'expected_build':refresh['expected_build'],'status':refresh['status']}
            now=j.now_iso(); expired=now
            conn.execute("""UPDATE browser_search_tasks SET status='queued',lease_owner='',worker_id='',lease_until=NULL,last_error=CASE WHEN last_error='' THEN 'reclaimed after stale bridge/extension lease' ELSE last_error END
               WHERE browser_run_id=? AND status='running' AND (lease_until IS NULL OR lease_until<?)""",(rid,expired))
            conn.execute("UPDATE browser_runs SET status='running',started_at=COALESCE(started_at,?),completed_at=NULL,last_progress_at=?,last_meaningful_progress_at=?,last_error='',worker_generation=worker_generation+1 WHERE browser_run_id=?",(now,now,now,rid));event(conn,rid,None,'run_started','normal Chrome platform-first run started',msg,out);conn.commit();return {'ok':True}
        if action=='platform_auth_result':
            rid=int(msg.get('run_id') or 0);platform=j.clean_text(msg.get('platform'));ok=bool(msg.get('authenticated'));reason=j.clean_text(msg.get('reason') or '');auth_state=_auth_state(msg,ok,reason)
            checked=j.now_iso()
            readiness = 'verified' if auth_state=='verified' else ('sign_in_required' if auth_state=='not_authenticated' else 'retryable')
            conn.execute("""UPDATE browser_platform_runs SET auth_status=?,auth_reason=?,auth_checked_at=?,
              readiness_state=?,readiness_reason=?,readiness_checked_at=?,worker_status=CASE WHEN ? THEN 'running' ELSE worker_status END,
              challenge_reason=CASE WHEN ? THEN '' ELSE challenge_reason END,
              resumed_at=CASE WHEN ? THEN ? ELSE resumed_at END
              WHERE browser_run_id=? AND platform=?""",
              (auth_state,reason,checked,readiness,reason,checked,int(auth_state=='verified'),int(auth_state=='verified'),int(auth_state=='verified'),checked,rid,platform))
            next_state = 'RUNNING' if auth_state == 'verified' else ('WAITING_FOR_HUMAN' if auth_state == 'not_authenticated' else 'SYSTEM_RETRYABLE')
            conn.execute("""UPDATE browser_platform_runs SET interaction_state=?,human_wait_reason=CASE WHEN ?='WAITING_FOR_HUMAN' THEN ? ELSE human_wait_reason END,
              last_checked_at=? WHERE browser_run_id=? AND platform=?""", (next_state, next_state, reason, checked, rid, platform))
            if auth_state != 'verified':
                tid=int(msg.get('task_id') or 0)
                task_status='auth_required' if auth_state=='not_authenticated' else 'deferred_by_platform'
                conn.execute("""UPDATE browser_search_tasks SET status=?,completed_at=?,last_error=?,lease_owner='',lease_until=NULL
                  WHERE browser_run_id=? AND platform=? AND status='running' AND (?=0 OR task_id=?)""",
                  (task_status,j.now_iso(),reason,rid,platform,tid,tid))
                conn.execute("""UPDATE browser_search_tasks SET status='deferred_by_platform',completed_at=NULL,last_error=?,lease_owner='',lease_until=NULL
                  WHERE browser_run_id=? AND platform=? AND status='queued'""",(reason,rid,platform))
            states=[x['auth_status'] for x in conn.execute("SELECT auth_status FROM browser_platform_runs WHERE browser_run_id=?",(rid,))]
            overall=_overall_auth_state(states)
            conn.execute("UPDATE browser_runs SET auth_status=?,last_progress_at=? WHERE browser_run_id=?",(overall,j.now_iso(),rid));event(conn,rid,None,'auth_verified' if ok else 'auth_failed',f'{platform}: {reason}',msg,out);conn.commit();return {'ok':True}
        if action=='platform_readiness':
            rid=int(msg.get('run_id') or 0);platform=j.clean_text(msg.get('platform'));status=j.clean_text(msg.get('status') or 'retryable').lower();reason=j.clean_text(msg.get('reason') or 'platform search surface not verified');now=j.now_iso()
            if status not in READINESS_STATES - {'unchecked'}: status='retryable'
            explicit_auth=j.clean_text(msg.get('auth_state') or '').lower()
            auth_status='verified' if status in {'verified','resumed'} else (explicit_auth if explicit_auth in AUTH_STATES - {'unchecked'} else 'unknown')
            if status=='sign_in_required': auth_status='not_authenticated'
            conn.execute("""UPDATE browser_platform_runs SET auth_status=?,auth_reason=?,auth_checked_at=?,readiness_state=?,readiness_reason=?,readiness_checked_at=?,worker_status=CASE WHEN ? THEN 'running' ELSE worker_status END,
              challenge_reason=CASE WHEN ? THEN '' ELSE challenge_reason END,
              resumed_at=CASE WHEN ? THEN ? ELSE resumed_at END
              WHERE browser_run_id=? AND platform=?""",(auth_status,reason,now,status,reason,now,int(status in {'verified','resumed'}),int(status in {'verified','resumed'}),int(status in {'verified','resumed'}),now,rid,platform))
            next_state = (
                'RUNNING' if status in {'verified', 'resumed'} else
                'WAITING_FOR_HUMAN' if status in {'challenged_cooldown', 'sign_in_required', 'user_action_required'} or auth_status == 'not_authenticated' else
                'SYSTEM_RETRYABLE' if status == 'retryable' else 'SYSTEM_UNVERIFIED'
            )
            conn.execute("""UPDATE browser_platform_runs SET interaction_state=?,human_wait_reason=CASE WHEN ?='WAITING_FOR_HUMAN' THEN ? ELSE human_wait_reason END,
              last_checked_at=? WHERE browser_run_id=? AND platform=?""", (next_state, next_state, reason, now, rid, platform))
            if status not in {'verified','resumed'}:
                tid=int(msg.get('task_id') or 0)
                conn.execute("""UPDATE browser_search_tasks SET status='deferred_by_platform',completed_at=NULL,last_error=?,lease_owner='',lease_until=NULL
                  WHERE browser_run_id=? AND platform=? AND status IN ('queued','running') AND (?=0 OR task_id=?)""",(reason,rid,platform,tid,tid))
                conn.execute("""UPDATE browser_search_tasks SET status='deferred_by_platform',completed_at=NULL,last_error=?,lease_owner='',lease_until=NULL
                  WHERE browser_run_id=? AND platform=? AND status='queued'""",(reason,rid,platform))
            states=[x['auth_status'] for x in conn.execute("SELECT auth_status FROM browser_platform_runs WHERE browser_run_id=?",(rid,))]
            conn.execute("UPDATE browser_runs SET auth_status=?,last_progress_at=? WHERE browser_run_id=?",(_overall_auth_state(states),now,rid))
            event(conn,rid,None,'platform_readiness',f'{platform}: {status} — {reason}',msg,out);conn.commit();return {'ok':True,'platform':platform,'readiness_state':status}
        if action=='pause_platform':
            rid=int(msg.get('run_id') or 0); platform=j.clean_text(msg.get('platform')); reason=j.clean_text(msg.get('reason') or 'platform challenge'); tid=int(msg.get('task_id') or 0)
            hours=float(cfg.get('runtime',{}).get('challenge_cooldown_hours',12) or 12)
            cooldown=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds')
            current=conn.execute("SELECT auth_status FROM browser_platform_runs WHERE browser_run_id=? AND platform=?",(rid,platform)).fetchone()
            requested_auth=j.clean_text(msg.get('auth_state') or '').lower()
            auth_status=requested_auth if requested_auth in AUTH_STATES - {'unchecked'} else ('verified' if current and current['auth_status']=='verified' else 'unknown')
            pause_at=j.now_iso()
            conn.execute("""UPDATE browser_platform_runs SET auth_status=?,auth_reason=?,cooldown_until=?,
              readiness_state='challenged_cooldown',readiness_reason=?,challenge_reason=?,worker_status='challenged',readiness_checked_at=?
              ,interaction_state='WAITING_FOR_HUMAN',human_wait_reason=?,human_wait_started_at=COALESCE(human_wait_started_at,?),last_checked_at=?
              WHERE browser_run_id=? AND platform=?""",(auth_status,reason,cooldown,reason,reason,pause_at,reason,pause_at,pause_at,rid,platform))
            active=conn.execute("""SELECT task_id FROM browser_search_tasks
              WHERE browser_run_id=? AND platform=? AND status='running' AND (?=0 OR task_id=?)""",(rid,platform,tid,tid)).fetchall()
            deferred_count=int(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND platform=? AND status='queued'",(rid,platform)).fetchone()[0])
            conn.execute("""UPDATE browser_search_tasks SET status='challenged',completed_at=?,challenge_reason=?,lease_owner='',lease_until=NULL
              WHERE browser_run_id=? AND platform=? AND status='running' AND (?=0 OR task_id=?)""",(j.now_iso(),reason,rid,platform,tid,tid))
            conn.execute("""UPDATE browser_search_tasks SET status='deferred_by_platform',completed_at=NULL,challenge_reason=?,lease_owner='',lease_until=NULL
              WHERE browser_run_id=? AND platform=? AND status='queued'""",(reason,rid,platform))
            if active:
                conn.execute("UPDATE browser_runs SET tasks_challenged=tasks_challenged+? WHERE browser_run_id=?",(len(active),rid))
                conn.execute("UPDATE browser_platform_runs SET tasks_challenged=tasks_challenged+? WHERE browser_run_id=? AND platform=?",(len(active),rid,platform))
            states=[x['auth_status'] for x in conn.execute("SELECT auth_status FROM browser_platform_runs WHERE browser_run_id=?",(rid,))]
            conn.execute("UPDATE browser_runs SET auth_status=?,last_progress_at=? WHERE browser_run_id=?",(_overall_auth_state(states),j.now_iso(),rid))
            event(conn,rid,None,'platform_paused',f'{platform}: {reason}',msg,out);conn.commit();return {'ok':True,'tasks_paused':len(active)+deferred_count,'tasks_challenged':len(active),'tasks_deferred':deferred_count}
        if action=='next_task':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            requested_platform=j.clean_text(msg.get('platform') or '')
            if int(r['stop_requested'] or 0):return {'ok':True,'stop':True,'platform':requested_platform}
            # The active atomic task checks stop_after_current after its last
            # detail. No new task may be leased after that latch is set.
            pstate=conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=? AND platform=?",(rid,requested_platform)).fetchone() if requested_platform else None
            if int(r['stop_after_current'] or 0) or (pstate is not None and (int(pstate['stop_after_current'] or 0) or int(pstate['emergency_stop'] or 0))):
                return {'ok':True,'stop':True,'stop_after_current':True}
            now=j.now_iso(); owner=j.clean_text(msg.get('worker_id') or f'run:{rid}')
            conn.execute("UPDATE browser_search_tasks SET status='queued',lease_owner='',worker_id='',lease_until=NULL WHERE browser_run_id=? AND status='running' AND lease_until IS NOT NULL AND lease_until<?",(rid,now))
            platform_clause = " AND platform=?" if requested_platform else ""
            running_args = (rid, requested_platform, owner, now) if requested_platform else (rid, owner, now)
            t=conn.execute(f"""SELECT * FROM browser_search_tasks
              WHERE browser_run_id=? AND status='running'
                {platform_clause}
                AND (lease_owner=? OR lease_until IS NULL OR lease_until<?)
              ORDER BY task_id LIMIT 1""",running_args).fetchone()
            if t is None and requested_platform:
                active_other=conn.execute("""SELECT task_id,worker_id,lease_owner FROM browser_search_tasks
                  WHERE browser_run_id=? AND platform=? AND status='running'
                    AND lease_until IS NOT NULL AND lease_until>? AND lease_owner<>?
                  ORDER BY task_id LIMIT 1""",(rid,requested_platform,now,owner)).fetchone()
                if active_other is not None:
                    return {'ok':True,'busy':True,'platform':requested_platform,'active_task_id':int(active_other['task_id'])}
            if t is None:
                wave_size=max(1,int(cfg.get('runtime',{}).get('platform_wave_size',3) or 3))
                platform_states={
                    row['platform']: row
                    for row in conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=?",(rid,)).fetchall()
                }
                queued=conn.execute(f"""SELECT * FROM browser_search_tasks
                  WHERE browser_run_id=? AND status='queued' {platform_clause}
                  ORDER BY {phase_order_sql()},COALESCE(NULLIF(effective_execution_rank,0),execution_rank),priority,task_id""",(rid, requested_platform) if requested_platform else (rid,)).fetchall()
                eligible=[]
                if requested_platform and pstate is not None and str(pstate['readiness_state']) == 'unchecked' and queued:
                    eligible=list(queued)
                for candidate in queued:
                    state=platform_states.get(candidate['platform'])
                    if not state or state['auth_status'] in {'not_authenticated','retryable','user_action_required','unknown'} or state['readiness_state'] in {'challenged_cooldown','sign_in_required','retryable','unknown','unverified','user_action_required'}:
                        continue
                    cooldown=str(state['cooldown_until'] or '')
                    if cooldown and cooldown>now:
                        continue
                    eligible.append(candidate)
                if eligible:
                    phase=min(eligible,key=lambda row: (0 if row['phase']=='A_FASTEST_DOOR_RECENT' else 1 if row['phase']=='B_REMAINING_CORE_RECENT' else 2 if row['phase']=='C_DEEP_BACKFILL' else 9))['phase']
                    phase_rows=[row for row in eligible if row['phase']==phase]
                    platform_candidates=[]
                    for platform in sorted({row['platform'] for row in phase_rows},key=lambda value: {'linkedin':0,'indeed':1,'glassdoor':2}.get(value,99)):
                        progress=conn.execute("""SELECT COUNT(*) FROM browser_search_tasks
                          WHERE browser_run_id=? AND platform=? AND phase=? AND status<>'queued'""",(rid,platform,phase)).fetchone()[0]
                        platform_candidates.append((int(progress)//wave_size,platform))
                    _,chosen_platform=min(platform_candidates,key=lambda item:(item[0],{'linkedin':0,'indeed':1,'glassdoor':2}.get(item[1],99)))
                    t=conn.execute(f"""SELECT * FROM browser_search_tasks
                      WHERE browser_run_id=? AND platform=? AND phase=? AND status='queued'
                      ORDER BY COALESCE(NULLIF(effective_execution_rank,0),execution_rank),priority,task_id LIMIT 1""",(rid,chosen_platform,phase)).fetchone()
            if not t:return {'ok':True,'done':True}
            if t['status']=='queued':
                lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180)
                claimed=conn.execute("UPDATE browser_search_tasks SET status='running',started_at=COALESCE(started_at,?),attempts=attempts+1,lease_owner=?,worker_id=?,worker_generation=worker_generation+1,lease_until=?,current_search_url=COALESCE(NULLIF(current_search_url,''),search_url),last_progress_at=? WHERE task_id=? AND status='queued'",(now,owner,owner,lease_time(lease_seconds),now,t['task_id']))
                if claimed.rowcount != 1:
                    conn.rollback()
                    return {'ok':True,'busy':True,'platform':requested_platform,'retry':True}
                event(conn,rid,t['task_id'],'task_started',f"{t['platform']}: {t['query_text']}",msg,out);conn.commit();t=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?",(t['task_id'],)).fetchone()
            conn.execute("UPDATE browser_runs SET current_task_id=?,last_progress_at=?,active_worker_count=(SELECT COUNT(DISTINCT worker_id) FROM browser_search_tasks WHERE browser_run_id=? AND status='running' AND worker_id<>'') WHERE browser_run_id=?",(t['task_id'],now,rid,rid))
            conn.execute("UPDATE browser_platform_runs SET worker_status='running',interaction_state='RUNNING',worker_id=?,worker_generation=worker_generation+1,worker_heartbeat_at=?,worker_started_at=COALESCE(worker_started_at,?),current_task_id=?,current_query=?,current_search_url=?,receiver_state='connected',chrome_available=1,last_meaningful_progress_at=?,last_checked_at=? WHERE browser_run_id=? AND platform=?",(owner,now,now,t['task_id'],t['query_text'],t['current_search_url'] or t['search_url'],now,now,rid,t['platform']))
            conn.commit()
            worker_row=conn.execute("SELECT worker_generation FROM browser_platform_runs WHERE browser_run_id=? AND platform=?",(rid,t['platform'])).fetchone()
            task_value={k:t[k] for k in t.keys()}
            task_value['worker_generation']=int(worker_row['worker_generation'] or 0) if worker_row else 0
            return {'ok':True,'task':task_value}
        if action=='record_result':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);source=j.clean_text(msg.get('source_site'));sid=j.clean_text(msg.get('source_job_id'));url=j.canonical_url(j.clean_text(msg.get('source_url') or ''))
            if not source or not (sid or url):return {'ok':False,'error':'insufficient_result_identity'}
            task=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=? AND browser_run_id=? AND platform=?",(tid,rid,source)).fetchone()
            if not task:return {'ok':False,'error':'task_not_found'}
            try: posted_age=float(msg['posted_age_days']) if msg.get('posted_age_days') is not None else None
            except (TypeError,ValueError): posted_age=None
            card=msg.get('card') if isinstance(msg.get('card'),dict) else {}
            selected,qa_sample,recall_reason,enrichment_priority=_recall_decision(
                strategy, source, j.clean_text(msg.get('title_hint') or card.get('title')),
                sid, url, j.clean_text(task['query_family']),
            )
            discovery,duplicate=upsert_card(conn,run_id=rid,task_id=tid,platform=source,source_job_id=sid,source_url=url,
                title_hint=j.clean_text(msg.get('title_hint') or card.get('title')),company_hint=j.clean_text(msg.get('company_hint') or card.get('company')),
                location_hint=j.clean_text(msg.get('location_hint') or card.get('location')),posted_text=j.clean_text(msg.get('posted_text') or card.get('posted_text')),
                posted_age_days=posted_age,card=card,eligible_for_detail=bool(msg.get('eligible_for_detail',True)),
                recall_selected=selected,recall_qa_sample=qa_sample,recall_reason=recall_reason,
                enrichment_priority=enrichment_priority,
                strategy_profile=j.clean_text(task['strategy_profile']),
                strategy_profile_version=j.clean_text(task['strategy_profile_version']),
                query_family=j.clean_text(task['query_family']), query_kind=j.clean_text(task['query_kind']),
                query_pass=j.clean_text(task['query_pass']), initial_order=int(task['initial_order'] or 0))
            if duplicate: conn.execute("UPDATE browser_search_tasks SET duplicate_sightings=duplicate_sightings+1 WHERE task_id=?",(tid,))
            pending_count=_pending_detail_count(conn,tid)
            event(conn,rid,tid,'result_discovered',f'{source}: {sid or url}',msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True,'duplicate':duplicate,'result_id':discovery.result_id,'detail_status':discovery.detail_status,'pending_count':int(pending_count)}
        if action=='next_pending_detail':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);owner=j.clean_text(msg.get('worker_id') or f'run:{rid}')
            lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180)
            run=get_run(conn,rid)
            discovery=claim_next_detail(conn,run_id=rid,task_id=tid,worker_id=owner,lease_seconds=lease_seconds,
                                        include_recall_negatives=bool(run and str(run['enrichment_mode'])=='all'))
            pending_count=_pending_detail_count(conn,tid)
            cached=None
            if discovery is not None and run is not None and str(run['mode']) not in {'acceptance','validation_micro','validation_sample'}:
                try:
                    current_hash=crawl_observations.card_hash(discovery.card,source_job_id=discovery.source_job_id,source_url=discovery.source_url,title=discovery.title_hint,company=discovery.company_hint,location=discovery.location_hint)
                    cached=crawl_observations.lookup(_cache_bundle(),platform=discovery.platform,source_job_id=discovery.source_job_id,source_url=discovery.source_url,current_card_hash=current_hash,source_build=_safe_source_build(),allow_reuse=True)
                except (OSError, ValueError, sqlite3.Error):
                    cached=None
            refresh_result_reconciliation(conn,rid,tid);conn.commit()
            detail=None if discovery is None else {**discovery.__dict__, 'cache_observation': cached}
            return {'ok':True,'done':discovery is None,'pending_count':int(pending_count),'detail':detail}
        if action=='detail_read':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);source=j.clean_text(msg.get('source_site'));sid=j.clean_text(msg.get('source_job_id'));url=j.canonical_url(j.clean_text(msg.get('source_url') or ''));now=j.now_iso()
            lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180)
            conn.execute("UPDATE browser_search_tasks SET detail_count_read=detail_count_read+1,last_progress_at=?,lease_until=? WHERE task_id=? AND browser_run_id=?",(now,lease_time(lease_seconds),tid,rid))
            conn.execute("UPDATE search_task_results SET last_seen_at=? WHERE result_id=? OR (task_id=? AND source_site=? AND source_job_id=? AND source_url=?)",(now,int(msg.get('result_id') or 0),tid,source,sid,url))
            event(conn,rid,tid,'detail_read',f'{source}: {sid or url}',msg,out);conn.commit();return {'ok':True}
        if action=='record_job':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);result_id=int(msg.get('result_id') or 0);raw=msg.get('job') or {}
            if not isinstance(raw,dict):return {'ok':False,'error':'invalid_job'}
            task=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=? AND browser_run_id=?",(tid,rid)).fetchone()
            if not task:return {'ok':False,'error':'task_not_found'}
            title=j.clean_text(raw.get('title'));company=j.clean_text(raw.get('company'));desc=j.strip_html(raw.get('description') or '')[:180000]
            url=j.canonical_url(j.clean_text(raw.get('canonical_url') or raw.get('url') or ''));sid=j.clean_text(raw.get('source_job_id') or '')
            if not title or not url:return {'ok':False,'error':'insufficient_job_identity'}
            evidence=msg.get('detail_evidence') if isinstance(msg.get('detail_evidence'),dict) else {}
            if not evidence and isinstance(raw.get('detail_evidence'),dict): evidence=raw['detail_evidence']
            discovery_url=url
            if result_id:
                observed=conn.execute("SELECT source_job_id,source_url FROM search_task_results WHERE result_id=? AND task_id=?",(result_id,tid)).fetchone()
                if observed is None:
                    return {'ok':False,'error':'result_not_found'}
                if observed['source_job_id'] and sid and str(observed['source_job_id']) != sid:
                    return {'ok':False,'error':'detail_identity_mismatch'}
                discovery_url=j.canonical_url(j.clean_text(observed['source_url'] or '')) or url
                acquisition=evidence.get('detail_acquisition') if isinstance(evidence.get('detail_acquisition'),dict) else {}
                mode=j.clean_text(acquisition.get('mode') or evidence.get('acquisition_mode') or '')
                if mode and mode not in {'search_pane','cache','user_reenrichment'}:
                    return {'ok':False,'error':'detail_acquisition_mode_not_allowed','mode':mode}
            unsafe=_unsafe_detail_reason(evidence,title,j.clean_text(raw.get('page_url') or url))
            if unsafe:
                if result_id: block_detail(conn,result_id,f"detail surface rejected as {unsafe}")
                event(conn,rid,tid,'unsafe_detail_rejected',f"{unsafe}: {title}",msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit()
                return {'ok':False,'error':'unsafe_detail_surface','surface':unsafe}
            if not desc:
                detail_status=fail_detail(conn,result_id,'detail identity had no substantive description',max_attempts=int(cfg.get('runtime',{}).get('watchdog_retries',3) or 3)) if result_id else 'RETRYABLE'
                event(conn,rid,tid,'detail_content_missing',f"{title}: substantive description missing",msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit()
                return {'ok':False,'error':'content_incomplete','detail_status':detail_status}
            source_site=j.clean_text(task['platform'])
            location=j.clean_text(raw.get('location') or '')
            if location.casefold() in {'[object object]', 'undefined', 'null'}:
                location=''
            remote_status=j.clean_text(raw.get('remote_status') or 'unknown').lower()
            if remote_status in {'remote','fully remote','100 remote','us remote'} and not (location or re.search(r'\bremote\b|work[ -]?from[ -]?home|\bwfh\b',desc,re.I)):
                remote_status='unknown'
            apply_candidate=j.canonical_url(j.clean_text(raw.get('apply_url') or ''))
            apply_url=apply_candidate if apply_candidate and apply_candidate != url else ''
            provenance={
                'source_type': 'board_detail',
                'detail_source_type': 'board_detail',
                'identity': 'observed_detail_identity',
                'card_metadata': 'search_card' if raw.get('search_card') else 'detail_surface',
                'description': 'observed_substantive_detail',
                'location': 'observed' if location else 'unknown',
                'location_source_type': 'board_detail',
                'remote': 'observed_detail_text' if remote_status in {'remote','fully remote','100 remote','us remote'} else 'unknown',
                'remote_source_type': 'board_detail',
                'salary': 'observed_detail_text' if j.clean_text(raw.get('salary_text') or '') else 'unknown',
                'salary_source_type': 'board_detail',
                'employment_type': 'observed_detail_text' if j.clean_text(raw.get('employment_type') or '') else 'unknown',
                'employment_type_source_type': 'board_detail',
                'posted_at': 'observed_search_card_or_detail' if j.clean_text(raw.get('posted_at') or '') else 'unknown',
                'posted_at_source_type': 'search_card' if raw.get('search_card') else 'board_detail',
                'requirements': 'employer_description_requirement_extraction',
                'requirements_source_type': 'board_detail',
                'application_destination': 'observed_distinct_destination' if apply_url else 'unknown_board_destination',
                'remote_filter_intent': bool(task['remote_required']),
            }
            job_raw={'browser_v3':True,'browser_run_id':rid,'browser_task_id':tid,'platform':source_site,'query_text':task['query_text'],'search_profile':task['search_profile'],'career_lane':task['career_lane'],'strategy_profile':task['strategy_profile'],'strategy_profile_version':task['strategy_profile_version'],'query_family':task['query_family'],'query_kind':task['query_kind'],'query_pass':task['query_pass'],'initial_order':task['initial_order'],'page_url':j.clean_text(raw.get('page_url') or url),'valid_through':j.clean_text(raw.get('valid_through') or ''),'remote_filter_intent':bool(task['remote_required']),'source_payload':raw,'_discovery_company':j.clean_text(raw.get('search_card',{}).get('company') if isinstance(raw.get('search_card'),dict) else ''),'discovery_url':discovery_url,'board_detail_url':url,'observed_board_apply_url':apply_candidate}
            job=j.Job(source_site=source_site,source_job_id=sid,canonical_url=url,apply_url=apply_url,title=title,company=company,location_raw=location,remote_status=remote_status,employment_type=j.clean_text(raw.get('employment_type') or ''),salary_text=j.clean_text(raw.get('salary_text') or ''),posted_at=j.clean_text(raw.get('posted_at') or ''),description=desc,category=j.clean_text(raw.get('category') or ''),tags=[j.clean_text(x) for x in(raw.get('tags') or []) if j.clean_text(x)],raw=job_raw)
            setattr(job,'_mode','deep');j.score_job(job,strategy,cfg.get('candidate',{}));ledger_status=store.upsert(job,commit=False)
            fields={'new':'jobs_new','updated':'jobs_updated','unchanged':'jobs_unchanged'}
            if ledger_status in fields:
                f=fields[ledger_status];conn.execute(f"UPDATE browser_search_tasks SET jobs_recorded=jobs_recorded+1,{f}={f}+1 WHERE task_id=?",(tid,));conn.execute(f"UPDATE browser_runs SET jobs_recorded=jobs_recorded+1,{f}={f}+1 WHERE browser_run_id=?",(rid,));conn.execute("UPDATE browser_platform_runs SET jobs_recorded=jobs_recorded+1 WHERE browser_run_id=? AND platform=?",(rid,source_site))
            if ledger_status=='new': conn.execute("UPDATE browser_search_tasks SET unique_jobs_recorded=unique_jobs_recorded+1 WHERE task_id=?",(tid,))
            else: conn.execute("UPDATE browser_search_tasks SET duplicate_sightings=duplicate_sightings+1 WHERE task_id=?",(tid,))
            jid=store.resolve_job_id(job)
            description_state='COMPLETE' if len(desc)>=250 else ('PARTIAL_TOO_SHORT' if desc else 'MISSING')
            content_state='COMPLETE' if description_state=='COMPLETE' else 'PARTIAL'
            enrichment_status='ENRICHED' if content_state=='COMPLETE' else 'PARTIAL'
            location_state='OBSERVED' if location else 'UNKNOWN'
            remote_state='OBSERVED' if provenance['remote'] != 'unknown' else 'UNKNOWN'
            apply_state='OBSERVED' if apply_url else 'UNKNOWN'
            conn.execute("""UPDATE jobs SET description_state=?,content_state=?,enrichment_status=?,enrichment_last_error='',
              location_evidence_state=?,remote_evidence_state=?,apply_destination_state=?,evidence_provenance_json=?
              WHERE job_id=?""",(description_state,content_state,enrichment_status,location_state,remote_state,apply_state,json.dumps(provenance,ensure_ascii=False),jid))
            result_evidence=(job.identity_evidence_state,job.detail_evidence_state,job.requirements_evidence_state,job.source_verification,job.application_destination_verification_state,job.evidence_readiness_state,json.dumps(job.evidence_missing,ensure_ascii=False),json.dumps(job.evidence_blocking,ensure_ascii=False))
            if result_id:
                finish_detail(conn,result_id,jid,content_state=content_state)
                conn.execute("""UPDATE search_task_results SET discovery_url=?,board_detail_url=?,observed_board_apply_url=?,
                  ats_requisition_url=?,verified_application_url=?,identity_evidence_state=?,detail_evidence_state=?,requirements_evidence_state=?,
                  source_verification_state=?,application_destination_verification_state=?,evidence_readiness_state=?,evidence_missing_json=?,evidence_blocking_json=?
                  WHERE result_id=?""",(discovery_url,url,apply_candidate,job.ats_requisition_url,job.verified_application_url,*result_evidence,result_id))
            else: conn.execute("""UPDATE search_task_results SET canonical_job_id=?,detail_read=1,detail_status=?,content_state=?,detail_completed_at=?,
              discovery_url=?,board_detail_url=?,observed_board_apply_url=?,ats_requisition_url=?,verified_application_url=?,
              identity_evidence_state=?,detail_evidence_state=?,requirements_evidence_state=?,source_verification_state=?,
              application_destination_verification_state=?,evidence_readiness_state=?,evidence_missing_json=?,evidence_blocking_json=?
              WHERE task_id=? AND source_site=? AND source_job_id=? AND source_url=?""",(jid,'COMPLETE' if content_state=='COMPLETE' else 'PARTIAL',content_state,j.now_iso(),discovery_url,url,apply_candidate,job.ats_requisition_url,job.verified_application_url,*result_evidence,tid,source_site,sid,url))
            acquisition_value=evidence.get('detail_acquisition') if isinstance(evidence.get('detail_acquisition'),dict) else {}
            conn.execute("UPDATE browser_search_tasks SET detail_acquisition_mode=? WHERE task_id=?", (j.clean_text(acquisition_value.get('mode') or evidence.get('acquisition_mode') or 'search_pane'), tid))
            cache_published=False
            if content_state == 'COMPLETE':
                try:
                    card_row=conn.execute("SELECT card_json,title_hint,company_hint,location_hint FROM search_task_results WHERE result_id=?",(result_id,)).fetchone() if result_id else None
                    cache_published=crawl_observations.publish(
                        _cache_bundle(), platform=source_site, source_job_id=sid, source_url=url,
                        card=json.loads(card_row['card_json'] or '{}') if card_row else {},
                        title=title, company=company, location=location, job={
                            'source_job_id':sid,'canonical_url':url,'title':title,'company':company,'location':location,
                            'remote_status':remote_status,'employment_type':j.clean_text(raw.get('employment_type') or ''),
                            'salary_text':j.clean_text(raw.get('salary_text') or ''),'posted_at':j.clean_text(raw.get('posted_at') or ''),
                            'description':desc,'valid_through':j.clean_text(raw.get('valid_through') or ''),
                        }, evidence={'detail_acquisition': evidence.get('detail_acquisition', {}) if isinstance(evidence, dict) else {}},
                        source_build=_safe_source_build(),
                    )
                except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
                    cache_published=False
            event(conn,rid,tid,'job_recorded',f'{ledger_status}: {job.title} — {job.company}',{'job_id':jid,'ledger_status':ledger_status,'recommendation':job.recommendation,'content_state':content_state,'apply_destination_state':apply_state,'cache_published':cache_published},out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True,'ledger_status':ledger_status,'job_id':jid,'recommendation':job.recommendation,'title':job.title,'company':job.company,'content_state':content_state,'enrichment_status':enrichment_status,'cache_published':cache_published}
        if action=='job_error':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);result_id=int(msg.get('result_id') or 0);message=j.clean_text(msg.get('message') or '')
            detail_status='FAILED'
            if result_id: detail_status=fail_detail(conn,result_id,message,max_attempts=int(cfg.get('runtime',{}).get('watchdog_retries',3) or 3))
            event(conn,rid,tid,'job_error',message,msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True,'detail_status':detail_status}
        if action=='detail_external_blocked':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);result_id=int(msg.get('result_id') or 0);message=j.clean_text(msg.get('message') or 'platform challenge')
            if result_id:block_detail(conn,result_id,message)
            event(conn,rid,tid,'detail_external_blocked',message,msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True}
        if action=='task_progress':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);seen=max(0,int(msg.get('results_seen') or 0));pages=max(0,int(msg.get('pages_visited') or 0));cp=msg.get('checkpoint') or {}
            stats=cp.get('card_stats') if isinstance(cp.get('card_stats'),dict) else {}
            lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180)
            now=j.now_iso(); requested=j.clean_text(cp.get('requested_search_url') or '')
            observed=j.clean_text(cp.get('observed_page_url') or cp.get('search_url') or '')
            context_status=j.clean_text(cp.get('context_status') or '')
            conn.execute("""UPDATE browser_search_tasks SET results_seen=MAX(results_seen,?),pages_visited=MAX(pages_visited,?),checkpoint_json=?,
              requested_search_url=CASE WHEN ?<>'' THEN ? ELSE requested_search_url END,
              observed_page_url=CASE WHEN ?<>'' THEN ? ELSE observed_page_url END,
              page_context_status=CASE WHEN ?<>'' THEN ? ELSE page_context_status END,
              current_search_url=?,page_number=MAX(page_number,?),scroll_generation=MAX(scroll_generation,?),last_page_fingerprint=?,last_source_job_id=?,last_progress_at=?,lease_until=?,
              cards_extracted=MAX(cards_extracted,?),cards_persistence_attempted=MAX(cards_persistence_attempted,?),cards_persistence_succeeded=MAX(cards_persistence_succeeded,?),cards_persistence_failed=MAX(cards_persistence_failed,?),duplicate_cards=MAX(duplicate_cards,?),pending_details=MAX(pending_details,?),details_failed=MAX(details_failed,?)
              WHERE task_id=? AND browser_run_id=?""",
              (seen,pages,json.dumps(cp,ensure_ascii=False),requested,requested,observed,observed,context_status,context_status,
               observed,int(cp.get('page_number') or pages),int(cp.get('scroll_generation') or 0),j.clean_text(cp.get('page_fingerprint') or ''),j.clean_text(cp.get('last_job_key') or ''),now,lease_time(lease_seconds),
               int(stats.get('extracted_cards') or 0),int(stats.get('persistence_attempted') or 0),int(stats.get('persistence_succeeded') or 0),int(stats.get('persistence_failed') or 0),int(stats.get('duplicate_cards') or 0),int(stats.get('pending_details') or 0),int(stats.get('details_failed') or 0),tid,rid))
            task_row=conn.execute("SELECT platform,worker_id,current_search_url,query_text FROM browser_search_tasks WHERE task_id=? AND browser_run_id=?",(tid,rid)).fetchone()
            conn.execute("UPDATE browser_runs SET last_progress_at=?,last_meaningful_progress_at=?,current_task_id=? WHERE browser_run_id=?",(now,now,tid,rid))
            if task_row:
                conn.execute("UPDATE browser_platform_runs SET worker_status='running',interaction_state='RUNNING',worker_id=?,worker_heartbeat_at=?,current_task_id=?,current_query=?,current_search_url=?,current_page_number=?,current_batch_size=?,last_meaningful_progress_at=?,last_progress_message=?,receiver_state='connected',chrome_available=1,last_checked_at=? WHERE browser_run_id=? AND platform=?",(task_row['worker_id'],now,tid,task_row['query_text'],task_row['current_search_url'],int(cp.get('page_number') or pages),int(stats.get('extracted_cards') or 0),now,j.clean_text(cp.get('last_job_key') or 'progress'),now,rid,task_row['platform']))
            conn.commit();return {'ok':True,'card_stats':stats,'context_status':context_status or 'UNVERIFIED'}
        if action=='heartbeat':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);platform=j.clean_text(msg.get('platform') or '');worker_id=j.clean_text(msg.get('worker_id') or '');lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180);now=j.now_iso();
            conn.execute("UPDATE browser_search_tasks SET last_progress_at=?,lease_until=? WHERE task_id=? AND browser_run_id=? AND (worker_id=? OR ?='')",(now,lease_time(lease_seconds),tid,rid,worker_id,worker_id))
            worker_status=j.clean_text(msg.get('worker_status') or 'running')
            meaningful=worker_status not in {'challenged','paused'}
            if platform:
                conn.execute("""UPDATE browser_platform_runs SET worker_heartbeat_at=?,worker_status=?,interaction_state=CASE
                  WHEN ? IN ('challenged','paused','sign_in_required') THEN 'WAITING_FOR_HUMAN'
                  WHEN ?='rechecking' THEN 'RECHECKING'
                  WHEN ?='running' AND interaction_state<>'WAITING_FOR_HUMAN' THEN 'RUNNING'
                  ELSE interaction_state END,
                  receiver_state='connected',chrome_available=1,
                  last_meaningful_progress_at=CASE WHEN ? THEN ? ELSE last_meaningful_progress_at END
                  ,last_checked_at=? WHERE browser_run_id=? AND platform=?""",(now,worker_status,worker_status,worker_status,worker_status,int(meaningful),now,now,rid,platform))
            conn.execute("UPDATE browser_runs SET last_progress_at=?,last_meaningful_progress_at=CASE WHEN ? THEN ? ELSE last_meaningful_progress_at END,current_task_id=? WHERE browser_run_id=?",(now,int(meaningful),now,tid or None,rid));conn.commit();return {'ok':True,'platform':platform,'worker_id':worker_id,'worker_status':worker_status}
        if action=='browser_event':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);message=j.clean_text(msg.get('message') or msg.get('event_type') or 'browser event');event(conn,rid,tid,j.clean_text(msg.get('event_type') or 'browser_event'),message,msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True}
        if action=='instrumentation_metrics':
            rid=int(msg.get('run_id') or 0) if msg.get('run_id') not in (None, '') else None
            return {'ok':True,'metrics':diagnostics.instrumentation(conn,rid)}
        if action=='worker_runtime':
            rid=int(msg.get('run_id') or 0);platform=j.clean_text(msg.get('platform') or '');worker_id=j.clean_text(msg.get('worker_id') or '');worker_status=j.clean_text(msg.get('worker_status') or 'running');now=j.now_iso();
            incoming_generation=int(msg.get('worker_generation') or 0)
            current_worker=conn.execute("SELECT worker_id,worker_generation FROM browser_platform_runs WHERE browser_run_id=? AND platform=?",(rid,platform)).fetchone()
            if incoming_generation and current_worker is not None and int(current_worker['worker_generation'] or 0) != incoming_generation:
                return {'ok':True,'stale':True,'platform':platform,'worker_id':worker_id,'worker_generation':incoming_generation}
            conn.execute("""UPDATE browser_platform_runs SET worker_id=?,worker_heartbeat_at=?,worker_status=?,receiver_state=?,chrome_available=?,owned_window=?,window_id=?,window_state=?,window_focused=?,search_tab_id=?,search_tab_url=?,current_search_url=?,last_progress_message=?,last_meaningful_progress_at=? WHERE browser_run_id=? AND platform=?""",(worker_id,now,worker_status,j.clean_text(msg.get('receiver_state') or 'connected'),int(bool(msg.get('chrome_available',True))),int(bool(msg.get('owned_window',True))),msg.get('window_id'),j.clean_text(msg.get('window_state') or ''),int(bool(msg.get('window_focused',False))),msg.get('search_tab_id'),j.clean_text(msg.get('search_tab_url') or ''),j.clean_text(msg.get('search_tab_url') or ''),j.clean_text(msg.get('message') or ''),now,rid,platform))
            next_state = 'WAITING_FOR_HUMAN' if worker_status in {'challenged','paused','sign_in_required'} else 'RECHECKING' if worker_status == 'rechecking' else 'RUNNING' if worker_status == 'running' else 'STOPPED' if worker_status == 'stopped' else 'COMPLETE' if worker_status == 'terminal' else 'INTERNAL_ERROR' if worker_status in {'failed','error'} else 'SYSTEM_RETRYABLE' if worker_status == 'retryable' else 'SYSTEM_UNVERIFIED' if worker_status == 'unverified' else 'STALE' if worker_status == 'stale' else 'IDLE'
            conn.execute("""UPDATE browser_platform_runs SET interaction_state=?,human_wait_reason=CASE WHEN ?='WAITING_FOR_HUMAN' AND human_wait_reason='' AND ?<>'' THEN ? ELSE human_wait_reason END,last_checked_at=? WHERE browser_run_id=? AND platform=?""",(next_state,next_state,j.clean_text(msg.get('message') or ''),j.clean_text(msg.get('message') or ''),now,rid,platform))
            if worker_status in {'terminal','stopped','challenged'}:
                conn.execute("UPDATE browser_platform_runs SET worker_completed_at=?,current_task_id=CASE WHEN ? IN ('terminal','stopped') THEN NULL ELSE current_task_id END WHERE browser_run_id=? AND platform=?", (now, worker_status, rid, platform))
            if worker_status in {'running','rechecking'}:
                conn.execute("UPDATE browser_runs SET status=CASE WHEN stop_requested=0 THEN 'running' ELSE status END,completed_at=NULL,last_progress_at=?,last_meaningful_progress_at=? WHERE browser_run_id=?",(now,now,rid))
            elif worker_status in {'challenged','paused'}:
                healthy_pending=conn.execute("""SELECT COUNT(*) FROM browser_search_tasks t
                  JOIN browser_platform_runs p ON p.browser_run_id=t.browser_run_id AND p.platform=t.platform
                  WHERE t.browser_run_id=? AND t.status IN ('queued','running')
                    AND p.worker_status NOT IN ('challenged','paused','sign_in_required')""",(rid,)).fetchone()[0]
                blocked_work=conn.execute("""SELECT COUNT(*) FROM browser_search_tasks
                  WHERE browser_run_id=? AND status IN ('challenged','deferred_by_platform','auth_required','incomplete','failed','stopped')""",(rid,)).fetchone()[0]
                if not healthy_pending and blocked_work:
                    conn.execute("UPDATE browser_runs SET status='partial',completed_at=COALESCE(completed_at,?),last_error=?,last_progress_at=? WHERE browser_run_id=? AND stop_requested=0",(now,f'{platform} supervisor paused: external platform action required',now,rid))
                else:
                    conn.execute("UPDATE browser_runs SET last_progress_at=? WHERE browser_run_id=?",(now,rid))
            else:
                healthy_pending=conn.execute("""SELECT COUNT(*) FROM browser_search_tasks t
                  JOIN browser_platform_runs p ON p.browser_run_id=t.browser_run_id AND p.platform=t.platform
                  WHERE t.browser_run_id=? AND t.status IN ('queued','running')
                    AND p.worker_status NOT IN ('challenged','paused','sign_in_required')""",(rid,)).fetchone()[0]
                blocked_work=conn.execute("""SELECT COUNT(*) FROM browser_search_tasks
                  WHERE browser_run_id=? AND status IN ('challenged','deferred_by_platform','auth_required','incomplete','failed','stopped')""",(rid,)).fetchone()[0]
                if worker_status in {'terminal','stopped'} and not healthy_pending and blocked_work and not int((conn.execute("SELECT stop_requested FROM browser_runs WHERE browser_run_id=?",(rid,)).fetchone() or {'stop_requested': 0})['stop_requested'] or 0):
                    conn.execute("UPDATE browser_runs SET status='partial',completed_at=COALESCE(completed_at,?),last_error=?,last_progress_at=? WHERE browser_run_id=?",(now,'platform supervisor paused: external platform action required',now,rid))
                else:
                    conn.execute("UPDATE browser_runs SET last_progress_at=?,last_meaningful_progress_at=? WHERE browser_run_id=?",(now,now,rid))
            conn.commit();return {'ok':True,'platform':platform,'worker_id':worker_id,'worker_status':worker_status}
        if action=='complete_task':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);status=j.clean_text(msg.get('status') or 'completed');reason=j.clean_text(msg.get('reason') or '');exhausted=1 if bool(msg.get('exhausted')) else 0
            if status=='completed': status='exhausted' if exhausted else 'incomplete'
            if status=='test_limit': status='incomplete'
            allowed={'exhausted','incomplete','challenged','failed','stopped','auth_required','paused'};status=status if status in allowed else 'incomplete'
            t=conn.execute("SELECT platform,status FROM browser_search_tasks WHERE task_id=? AND browser_run_id=?",(tid,rid)).fetchone()
            if not t:return {'ok':False,'error':'task_not_found'}
            if t['status'] not in {'queued','running'}:
                return {'ok':True,'already_terminal':True,'status':t['status']}
            now=j.now_iso();conn.execute("UPDATE browser_search_tasks SET status=?,completed_at=?,challenge_reason=?,last_error=?,exhausted=?,exhaustion_reason=?,safety_stop_reason=?,lease_owner='',lease_until=NULL,last_progress_at=? WHERE task_id=?",(status,now,reason if status=='challenged' else '',reason if status in {'failed','auth_required'} else '',1 if status=='exhausted' else 0,reason if status=='exhausted' else '',reason if status=='incomplete' else '',now,tid))
            platform=t['platform']
            if status=='exhausted':
                conn.execute("UPDATE browser_runs SET tasks_completed=tasks_completed+1 WHERE browser_run_id=?",(rid,));conn.execute("UPDATE browser_platform_runs SET tasks_completed=tasks_completed+1 WHERE browser_run_id=? AND platform=?",(rid,platform))
            elif status=='incomplete':
                conn.execute("UPDATE browser_runs SET tasks_incomplete=tasks_incomplete+1 WHERE browser_run_id=?",(rid,));conn.execute("UPDATE browser_platform_runs SET tasks_incomplete=tasks_incomplete+1 WHERE browser_run_id=? AND platform=?",(rid,platform))
            elif status=='challenged':
                conn.execute("UPDATE browser_runs SET tasks_challenged=tasks_challenged+1 WHERE browser_run_id=?",(rid,));conn.execute("UPDATE browser_platform_runs SET tasks_challenged=tasks_challenged+1 WHERE browser_run_id=? AND platform=?",(rid,platform))
            elif status=='failed':conn.execute("UPDATE browser_platform_runs SET tasks_failed=tasks_failed+1 WHERE browser_run_id=? AND platform=?",(rid,platform))
            if status=='stopped' and int(conn.execute("SELECT stop_after_current FROM browser_runs WHERE browser_run_id=?",(rid,)).fetchone()[0] or 0):
                conn.execute("UPDATE browser_runs SET stop_requested=1,current_task_id=NULL,last_error='stop after current job completed' WHERE browser_run_id=?",(rid,))
            event(conn,rid,tid,'task_'+status,reason,msg,out);conn.commit();return {'ok':True}
        if action=='should_stop':
            rid=int(msg.get('run_id') or 0);platform=j.clean_text(msg.get('platform') or '');r=get_run(conn,rid)
            pstate=conn.execute("SELECT stop_after_current,emergency_stop FROM browser_platform_runs WHERE browser_run_id=? AND platform=?",(rid,platform)).fetchone() if platform else None
            platform_stop=bool(pstate and (int(pstate['stop_after_current'] or 0) or int(pstate['emergency_stop'] or 0)))
            return {'ok':True,'stop':bool(r and int(r['stop_requested'] or 0)) or bool(pstate and int(pstate['emergency_stop'] or 0)),'stop_after_current':bool(r and int(r['stop_after_current'] or 0)) or platform_stop,'platform_stop_after_current':platform_stop}
        if action in {'request_stop','emergency_stop'}:
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            now=j.now_iso(); immediate=action=='emergency_stop'
            if immediate:
                active=conn.execute("SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? AND status='running'",(rid,)).fetchall()
                conn.execute("""UPDATE browser_search_tasks SET status='incomplete',completed_at=?,lease_owner='',lease_until=NULL,
                    safety_stop_reason='manual_emergency_stop',last_error='manual emergency stop',last_progress_at=?
                    WHERE browser_run_id=? AND status='running'""",(now,now,rid))
                conn.execute("UPDATE browser_runs SET status='stopped',stop_requested=1,stop_after_current=0,completed_at=?,current_task_id=NULL,tasks_incomplete=(SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='incomplete'),last_progress_at=?,last_error='manual emergency stop' WHERE browser_run_id=?",(now,rid,now,rid))
                for active_task in active: event(conn,rid,active_task['task_id'],'task_incomplete','manual emergency stop',msg,out)
            else:
                conn.execute("UPDATE browser_runs SET stop_after_current=1,last_error=? WHERE browser_run_id=?",('stop after current job requested',rid))
            event(conn,rid,None,action,'stop requested',msg,out);conn.commit();return {'ok':True,'immediate':immediate}
        if action=='finish_run':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            pending=conn.execute("SELECT COUNT(*) n FROM browser_search_tasks WHERE browser_run_id=? AND status IN ('queued','running')",(rid,)).fetchone()['n']
            bad=conn.execute("SELECT COUNT(*) n FROM browser_search_tasks WHERE browser_run_id=? AND status IN ('incomplete','challenged','failed','auth_required','deferred_by_platform','paused','stopped')",(rid,)).fetchone()['n']
            final='stopped' if int(r['stop_requested'] or 0) else ('partial' if pending or bad else 'completed')
            refresh_task_counters(conn,rid)
            conn.execute("UPDATE browser_runs SET status=?,completed_at=?,last_progress_at=?,current_task_id=NULL WHERE browser_run_id=?",(final,j.now_iso(),j.now_iso(),rid));event(conn,rid,None,'run_finished',final,{},out);conn.commit()
            try:j.export_all(store,out,strategy,cfg,'deep')
            except Exception as e:log(f'export warning: {e}')
            return {'ok':True,'status':final}
        if action=='run_status':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            refresh_task_counters(conn,rid);conn.commit();r=get_run(conn,rid)
            tasks=conn.execute("SELECT task_id,platform,phase,query_text,status,results_seen,detail_count_read,jobs_recorded,unique_jobs_recorded,duplicate_sightings,jobs_new,jobs_updated,jobs_unchanged,pages_visited,current_search_url,page_number,scroll_generation,last_page_fingerprint,last_source_job_id,challenge_reason,last_error,exhaustion_reason,safety_stop_reason,execution_rank,cards_extracted,cards_persistence_attempted,cards_persistence_succeeded,cards_persistence_failed,duplicate_cards,pending_details,details_failed FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id",(rid,)).fetchall();plats=conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=? ORDER BY CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 ELSE 2 END",(rid,)).fetchall();return {'ok':True,'run':{k:r[k] for k in r.keys()},'platforms':[{k:p[k] for k in p.keys()} for p in plats],'tasks':[{k:t[k] for k in t.keys()} for t in tasks]}
        if action=='task_status':
            tid=int(msg.get('task_id') or 0);t=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?",(tid,)).fetchone()
            if not t:return {'ok':False,'error':'task_not_found'}
            return {'ok':True,'task':{k:t[k] for k in t.keys()}}
        if action=='run_error':
            rid=int(msg.get('run_id') or 0);message=j.clean_text(msg.get('message') or 'extension error');conn.execute("UPDATE browser_runs SET last_error=?,last_progress_at=? WHERE browser_run_id=?",(message,j.now_iso(),rid));event(conn,rid,None,'run_error',message,msg,out);conn.commit();return {'ok':True}
        return {'ok':False,'error':'unknown_action','action':action}
    finally:store.close()
