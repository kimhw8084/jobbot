#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from .. import legacy_engine as j
from .. import browser_tasks as v3
from ..discoveries import block_detail, claim_next_detail, fail_detail, finish_detail, upsert_card
from ..query_yield import mark_definition_completed, mark_definition_started, record_task_yield
from ..search_strategy import BAND_ORDER, detail_priority, query_yield_estimate

BASE = PROJECT_ROOT
j.VERSION=v3.V3_VERSION; j.c.VERSION=v3.V3_VERSION


def log(msg:str)->None: print(f"[jobbot-rpc] {msg}",file=sys.stderr,flush=True)

def lease_time(seconds:int=180)->str:
    return (datetime.now(timezone.utc)+timedelta(seconds=max(1,int(seconds)))).isoformat(timespec='seconds')

MUTATING_ACTIONS = frozenset({
    'begin_run', 'platform_auth_result', 'pause_platform', 'next_task',
    'retry_platform', 'record_result', 'next_pending_detail', 'detail_read',
    'record_job', 'job_error', 'detail_external_blocked', 'task_progress',
    'heartbeat', 'browser_event', 'complete_task', 'request_stop',
    'emergency_stop', 'finish_run', 'run_error',
})


def open_store(*, defer_commits: bool = False):
    db,out,_,cfg,strategy=v3.paths(BASE);store=j.PrecisionStore(db);v3.init_browser_schema(store.conn)
    if defer_commits:
        store.conn.defer_commits = True
    return store,cfg,strategy,out


def _normalized_payload(msg: dict[str, Any]) -> str:
    """Return a deterministic request identity without transport secrets."""
    value = {str(k): v for k, v in msg.items() if str(k) != 'request_id'}
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def request_payload_hash(msg: dict[str, Any]) -> str:
    return hashlib.sha256(_normalized_payload(msg).encode('utf-8')).hexdigest()


def _receipt_metadata(msg: dict[str, Any]) -> tuple[int | None, int | None]:
    def integer(name: str) -> int | None:
        try:
            value = int(msg.get(name) or 0)
            return value or None
        except (TypeError, ValueError):
            return None
    return integer('run_id'), integer('task_id')


def _replay_or_collision(conn, msg: dict[str, Any]) -> dict[str, Any] | None:
    request_id = str(msg.get('request_id') or '').strip()
    if not request_id:
        return None
    row = conn.execute("SELECT * FROM rpc_receipts WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        return None
    if str(row['action']) != str(msg.get('action') or '') or str(row['payload_hash']) != request_payload_hash(msg):
        return {'ok': False, 'error': 'idempotency_collision', 'request_id': request_id}
    try:
        response = json.loads(row['response_json'] or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        response = {'ok': False, 'error': 'corrupt_rpc_receipt'}
    if not isinstance(response, dict):
        response = {'ok': False, 'error': 'corrupt_rpc_receipt'}
    response['replayed'] = True
    return response


def _begin_receipt(conn, msg: dict[str, Any]) -> dict[str, Any] | None:
    request_id = str(msg.get('request_id') or '').strip()
    if not request_id:
        return None
    if len(request_id) > 200:
        raise ValueError('request_id too long')
    replay = _replay_or_collision(conn, msg)
    if replay is not None:
        return replay
    run_id, task_id = _receipt_metadata(msg)
    conn.execute(
        """INSERT INTO rpc_receipts(request_id,action,payload_hash,status,response_json,run_id,task_id,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (request_id, str(msg.get('action') or ''), request_payload_hash(msg), 'PENDING', '{}', run_id, task_id, j.now_iso()),
    )
    return None


def _finish_receipt(conn, msg: dict[str, Any], response: dict[str, Any]) -> None:
    request_id = str(msg.get('request_id') or '').strip()
    if not request_id:
        return
    conn.execute(
        "UPDATE rpc_receipts SET status='COMMITTED',response_json=?,completed_at=? WHERE request_id=?",
        (json.dumps(response, ensure_ascii=False, separators=(',', ':')), j.now_iso(), request_id),
    )

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

def platform_order_sql()->str:return "CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 WHEN 'glassdoor' THEN 2 ELSE 99 END"

def phase_order_sql()->str:return "CASE phase WHEN 'A_FASTEST_DOOR_RECENT' THEN 0 WHEN 'B_REMAINING_CORE_RECENT' THEN 1 WHEN 'C_DEEP_BACKFILL' THEN 2 ELSE 9 END"

def task_selection_key(conn, row):
    """Keep band/order protection, then use learned ROI only when eligible."""
    learned_roi = 0.0
    stats = conn.execute(
        "SELECT * FROM query_yield_stats WHERE platform=? AND normalized_query=? AND search_band=? AND window_class=? AND window_days=?",
        (row['platform'], str(row['query_text']).casefold(), row['search_band'], row['window_class'], row['window_days']),
    ).fetchone()
    if stats is not None:
        estimate = query_yield_estimate(dict(stats))
        if estimate['sample_eligible']:
            learned_roi = float(estimate['conservative_new_actionable_per_minute'])
    return (BAND_ORDER.get(str(row['search_band'] or 'DEEP_TAIL'), 99), -learned_roi,
            int(row['execution_rank'] or 0), int(row['priority'] or 99), int(row['task_id']))

def platform_circuit(conn, platform: str):
    return conn.execute("SELECT * FROM platform_state WHERE platform=?", (platform,)).fetchone()

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

def backfill_task_yields(conn, rid: int, observed_at: str | None = None) -> int:
    """Finalize exhausted-task yield after all attempt timing is durable."""
    recorded = 0
    for row in conn.execute(
        "SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? AND status='exhausted' AND yield_recorded_at IS NULL",
        (rid,),
    ).fetchall():
        recorded += int(record_task_yield(conn, int(row['task_id']), observed_at) or 0)
    return recorded

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
        "SELECT COUNT(*) FROM search_task_results WHERE browser_run_id=? AND task_id=? AND detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED')",
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

def _handle_once(msg:dict[str,Any], active_store=None)->dict[str,Any]:
    action=str(msg.get('action') or '')
    owns_store = active_store is None
    store,cfg,strategy,out=(open_store() if active_store is None else active_store)
    conn=store.conn
    try:
        if action=='ping': return {'ok':True,'version':v3.V3_VERSION,'bridge':'loopback'}
        if action=='runtime_config':
            runtime=cfg.get('runtime',{})
            return {'ok':True,'heartbeat_seconds':int(runtime.get('heartbeat_seconds',20) or 20),
                    'lease_seconds':int(runtime.get('lease_seconds',180) or 180),
                    'watchdog_stall_seconds':int(runtime.get('watchdog_stall_seconds',180) or 180),
                    'primary_navigation_min_gap_ms':int(runtime.get('primary_navigation_min_gap_ms',900) or 900),
                    'primary_dom_quiet_ms':int(runtime.get('primary_dom_quiet_ms',800) or 800),
                    'primary_dom_quiet_timeout_ms':int(runtime.get('primary_dom_quiet_timeout_ms',5000) or 5000),
                    'primary_scroll_wait_ms':int(runtime.get('primary_scroll_wait_ms',900) or 900),
                    'primary_detail_transition_min_gap_ms':int(runtime.get('primary_detail_transition_min_gap_ms',900) or 900),
                    'primary_transient_retry_limit':int(runtime.get('primary_transient_retry_limit',2) or 2),
                    'primary_transient_backoff_seconds':int(runtime.get('primary_transient_backoff_seconds',2) or 2)}
        if action=='begin_run':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            if int(r['stop_requested'] or 0):return {'ok':False,'error':'stop_requested'}
            now=j.now_iso(); expired=now
            conn.execute("""UPDATE browser_search_tasks SET status='queued',lease_owner='',lease_until=NULL,last_error=CASE WHEN last_error='' THEN 'reclaimed after stale bridge/extension lease' ELSE last_error END
               WHERE browser_run_id=? AND status='running' AND (lease_until IS NULL OR lease_until<?)""",(rid,expired))
            conn.execute("UPDATE browser_runs SET status='running',started_at=COALESCE(started_at,?),completed_at=NULL,last_progress_at=?,last_error='' WHERE browser_run_id=?",(now,now,rid));event(conn,rid,None,'run_started','normal Chrome platform-first run started',msg,out);conn.commit();return {'ok':True}
        if action=='platform_auth_result':
            rid=int(msg.get('run_id') or 0);platform=j.clean_text(msg.get('platform'));ok=bool(msg.get('authenticated'));reason=j.clean_text(msg.get('reason') or '')
            conn.execute("UPDATE browser_platform_runs SET auth_status=?,auth_reason=?,auth_checked_at=? WHERE browser_run_id=? AND platform=?",('verified' if ok else 'not_authenticated',reason,j.now_iso(),rid,platform))
            conn.execute("""INSERT INTO platform_state(platform,auth_status,auth_reason,last_auth_checked_at,last_error)
                VALUES(?,?,?,?,?) ON CONFLICT(platform) DO UPDATE SET auth_status=excluded.auth_status,auth_reason=excluded.auth_reason,last_auth_checked_at=excluded.last_auth_checked_at,last_error=excluded.last_error""",
                (platform,'verified' if ok else 'auth_required',reason,j.now_iso(),'' if ok else reason))
            if not ok:
                tid=int(msg.get('task_id') or 0)
                conn.execute("""UPDATE browser_search_tasks SET status='auth_required',completed_at=?,last_error=?,lease_owner='',lease_until=NULL
                  WHERE browser_run_id=? AND platform=? AND status='running' AND (?=0 OR task_id=?)""",(j.now_iso(),reason,rid,platform,tid,tid))
                conn.execute("""UPDATE browser_search_tasks SET status='deferred_by_platform',completed_at=NULL,last_error=?,lease_owner='',lease_until=NULL
                  WHERE browser_run_id=? AND platform=? AND status='queued'""",(reason,rid,platform))
            states=[x['auth_status'] for x in conn.execute("SELECT auth_status FROM browser_platform_runs WHERE browser_run_id=?",(rid,))]
            overall='verified' if states and all(x=='verified' for x in states) else ('partial' if any(x=='verified' for x in states) else 'not_authenticated')
            conn.execute("UPDATE browser_runs SET auth_status=?,last_progress_at=? WHERE browser_run_id=?",(overall,j.now_iso(),rid));event(conn,rid,None,'auth_verified' if ok else 'auth_failed',f'{platform}: {reason}',msg,out);conn.commit();return {'ok':True}
        if action=='pause_platform':
            rid=int(msg.get('run_id') or 0); platform=j.clean_text(msg.get('platform')); reason=j.clean_text(msg.get('reason') or 'platform challenge'); tid=int(msg.get('task_id') or 0)
            hours=float(cfg.get('runtime',{}).get('challenge_cooldown_hours',12) or 12)
            cooldown=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds')
            conn.execute("UPDATE browser_platform_runs SET auth_status='challenged',auth_reason=?,cooldown_until=? WHERE browser_run_id=? AND platform=?",(reason,cooldown,rid,platform))
            conn.execute("""INSERT INTO platform_state(platform,auth_status,auth_reason,last_auth_checked_at,challenged_at,cooldown_until,last_error)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform) DO UPDATE SET auth_status='challenged',auth_reason=excluded.auth_reason,last_auth_checked_at=excluded.last_auth_checked_at,challenged_at=excluded.challenged_at,cooldown_until=excluded.cooldown_until,last_error=excluded.last_error""",
                (platform,'challenged',reason,j.now_iso(),j.now_iso(),cooldown,reason))
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
            event(conn,rid,None,'platform_paused',f'{platform}: {reason}',msg,out);conn.commit();return {'ok':True,'tasks_paused':len(active)+deferred_count,'tasks_challenged':len(active),'tasks_deferred':deferred_count}
        if action=='next_task':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid)
            if not r:return {'ok':False,'error':'run_not_found'}
            if int(r['stop_requested'] or 0):return {'ok':True,'stop':True}
            if int(r['stop_after_current'] or 0):return {'ok':True,'stop':True,'stop_after_current':True}
            now=j.now_iso(); owner=j.clean_text(msg.get('worker_id') or f'run:{rid}')
            conn.execute("UPDATE browser_search_tasks SET status='queued',lease_owner='',lease_until=NULL WHERE browser_run_id=? AND status='running' AND lease_until IS NOT NULL AND lease_until<?",(rid,now))
            t=conn.execute(f"""SELECT * FROM browser_search_tasks
              WHERE browser_run_id=? AND status='running'
                AND (lease_owner=? OR lease_until IS NULL OR lease_until<?)
              ORDER BY task_id LIMIT 1""",(rid,owner,now)).fetchone()
            if t is None:
                wave_size=max(1,int(cfg.get('runtime',{}).get('platform_wave_size',3) or 3))
                platform_states={
                    row['platform']: row
                    for row in conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=?",(rid,)).fetchall()
                }
                queued=conn.execute(f"""SELECT * FROM browser_search_tasks
                  WHERE browser_run_id=? AND status='queued'
                  ORDER BY CASE WHEN phase IN ('A_FASTEST_DOOR_RECENT','B_REMAINING_CORE_RECENT') THEN 0 ELSE 1 END,
                           CASE search_band WHEN 'GOLD' THEN 0 WHEN 'SILVER' THEN 1 WHEN 'GROWTH' THEN 2 WHEN 'HEDGE' THEN 3 ELSE 4 END,
                           execution_rank,priority,task_id""",(rid,)).fetchall()
                eligible=[]
                for candidate in queued:
                    state=platform_states.get(candidate['platform'])
                    if not state or state['auth_status'] in {'challenged','not_authenticated'}:
                        continue
                    global_state=platform_circuit(conn,candidate['platform'])
                    if global_state and global_state['auth_status'] in {'challenged','auth_required'}:
                        global_cooldown=str(global_state['cooldown_until'] or '')
                        if not global_cooldown or global_cooldown>now:
                            continue
                    cooldown=str(state['cooldown_until'] or '')
                    if cooldown and cooldown>now:
                        continue
                    eligible.append(candidate)
                if eligible:
                    recent_rows=[row for row in eligible if row['phase'] in {'A_FASTEST_DOOR_RECENT','B_REMAINING_CORE_RECENT'}]
                    deep_rows=[row for row in eligible if row['phase']=='C_DEEP_BACKFILL']
                    # Older acceptance/task fixtures may not carry a phase.
                    # Preserve their runnable behavior while keeping the
                    # production A/B-before-C grouping for staged tasks.
                    phase_rows=recent_rows or deep_rows or eligible
                    platform_candidates=[]
                    for platform in sorted({row['platform'] for row in phase_rows},key=lambda value: {'linkedin':0,'indeed':1,'glassdoor':2}.get(value,99)):
                        progress=conn.execute("""SELECT COUNT(*) FROM browser_search_tasks
                          WHERE browser_run_id=? AND platform=? AND status<>'queued'""",(rid,platform)).fetchone()[0]
                        platform_candidates.append((int(progress)//wave_size,platform))
                    _,chosen_platform=min(platform_candidates,key=lambda item:(item[0],{'linkedin':0,'indeed':1,'glassdoor':2}.get(item[1],99)))
                    platform_queue=[row for row in phase_rows if row['platform']==chosen_platform]
                    t=min(platform_queue,key=lambda row: task_selection_key(conn,row)) if platform_queue else None
            if not t:return {'ok':True,'done':True}
            if t['status']=='queued':
                lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180)
                conn.execute("UPDATE browser_search_tasks SET status='running',started_at=COALESCE(started_at,?),attempts=attempts+1,lease_owner=?,lease_until=?,current_search_url=COALESCE(NULLIF(current_search_url,''),search_url),last_progress_at=? WHERE task_id=?",(now,owner,lease_time(lease_seconds),now,t['task_id']));mark_definition_started(conn, t, now);event(conn,rid,t['task_id'],'task_started',f"{t['platform']}: {t['query_text']}",msg,out);conn.commit();t=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?",(t['task_id'],)).fetchone()
                conn.execute("UPDATE browser_runs SET current_task_id=?,last_progress_at=? WHERE browser_run_id=?",(t['task_id'],now,rid));conn.commit()
            return {'ok':True,'task':{k:t[k] for k in t.keys()}}
        if action=='retry_platform':
            platform=j.clean_text(msg.get('platform')); reason=j.clean_text(msg.get('reason') or 'manual human retry requested')
            now=j.now_iso()
            conn.execute("""INSERT INTO platform_state(platform,auth_status,auth_reason,cooldown_until,manual_retry_requested_at,last_error)
                VALUES(?,?,?,?,?,?) ON CONFLICT(platform) DO UPDATE SET auth_status='unchecked',auth_reason='',cooldown_until=NULL,manual_retry_requested_at=excluded.manual_retry_requested_at,last_error=''""",
                (platform,'unchecked','',None,now,''))
            conn.execute("UPDATE browser_platform_runs SET auth_status='unchecked',auth_reason='',cooldown_until=NULL WHERE platform=?",(platform,))
            conn.execute("UPDATE browser_search_tasks SET status='queued',completed_at=NULL,challenge_reason='',last_error='',lease_owner='',lease_until=NULL WHERE platform=? AND status IN ('challenged','auth_required','deferred_by_platform')",(platform,))
            conn.commit(); return {'ok':True,'platform':platform,'reason':reason}
        if action=='record_result':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);source=j.clean_text(msg.get('source_site'));sid=j.clean_text(msg.get('source_job_id'));url=j.canonical_url(j.clean_text(msg.get('source_url') or ''))
            if not source or not (sid or url):return {'ok':False,'error':'insufficient_result_identity'}
            task=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=? AND browser_run_id=? AND platform=?",(tid,rid,source)).fetchone()
            if not task:return {'ok':False,'error':'task_not_found'}
            try: posted_age=float(msg['posted_age_days']) if msg.get('posted_age_days') is not None else None
            except (TypeError,ValueError): posted_age=None
            card=msg.get('card') if isinstance(msg.get('card'),dict) else {}
            priority, priority_reason = detail_priority(
                title=j.clean_text(msg.get('title_hint') or card.get('title')),
                query_text=task['query_text'], search_band=task['search_band'],
                posted_age_days=posted_age, strategy=strategy,
            )
            discovery,duplicate=upsert_card(conn,run_id=rid,task_id=tid,platform=source,source_job_id=sid,source_url=url,
                title_hint=j.clean_text(msg.get('title_hint') or card.get('title')),company_hint=j.clean_text(msg.get('company_hint') or card.get('company')),
                location_hint=j.clean_text(msg.get('location_hint') or card.get('location')),posted_text=j.clean_text(msg.get('posted_text') or card.get('posted_text')),
                posted_age_days=posted_age,card=card,eligible_for_detail=bool(msg.get('eligible_for_detail',True)),
                detail_priority=priority,detail_priority_reason=priority_reason)
            if duplicate: conn.execute("UPDATE browser_search_tasks SET duplicate_sightings=duplicate_sightings+1 WHERE task_id=?",(tid,))
            pending_count=conn.execute("SELECT COUNT(*) FROM search_task_results WHERE task_id=? AND detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED')",(tid,)).fetchone()[0]
            event(conn,rid,tid,'result_discovered',f'{source}: {sid or url}',msg,out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True,'duplicate':duplicate,'result_id':discovery.result_id,'detail_status':discovery.detail_status,'pending_count':int(pending_count)}
        if action=='next_pending_detail':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);owner=j.clean_text(msg.get('worker_id') or f'run:{rid}')
            lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180)
            discovery=claim_next_detail(conn,run_id=rid,task_id=tid,worker_id=owner,lease_seconds=lease_seconds)
            pending_count=conn.execute("SELECT COUNT(*) FROM search_task_results WHERE task_id=? AND detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED')",(tid,)).fetchone()[0]
            refresh_result_reconciliation(conn,rid,tid);conn.commit()
            return {'ok':True,'done':discovery is None,'pending_count':int(pending_count),'detail':None if discovery is None else discovery.__dict__}
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
            source_site=j.clean_text(task['platform'])
            remote_status=j.clean_text(raw.get('remote_status') or ('remote' if int(task['remote_required'] or 0) else 'unknown'))
            job=j.Job(source_site=source_site,source_job_id=sid,canonical_url=url,apply_url=j.canonical_url(j.clean_text(raw.get('apply_url') or url)),title=title,company=company,location_raw=j.clean_text(raw.get('location') or 'Remote'),remote_status=remote_status,employment_type=j.clean_text(raw.get('employment_type') or ''),salary_text=j.clean_text(raw.get('salary_text') or ''),posted_at=j.clean_text(raw.get('posted_at') or ''),description=desc,category=j.clean_text(raw.get('category') or ''),tags=[j.clean_text(x) for x in(raw.get('tags') or []) if j.clean_text(x)],raw={'browser_v3':True,'browser_run_id':rid,'browser_task_id':tid,'platform':source_site,'query_text':task['query_text'],'search_profile':task['search_profile'],'career_lane':task['career_lane'],'page_url':j.clean_text(raw.get('page_url') or url),'valid_through':j.clean_text(raw.get('valid_through') or ''),'remote_filter_evidence':bool(task['remote_required']),'source_payload':raw})
            setattr(job,'_mode','deep');j.score_job(job,strategy,cfg.get('candidate',{}));ledger_status=store.upsert(job,commit=False)
            fields={'new':'jobs_new','updated':'jobs_updated','unchanged':'jobs_unchanged'}
            if ledger_status in fields:
                f=fields[ledger_status];conn.execute(f"UPDATE browser_search_tasks SET jobs_recorded=jobs_recorded+1,{f}={f}+1 WHERE task_id=?",(tid,));conn.execute(f"UPDATE browser_runs SET jobs_recorded=jobs_recorded+1,{f}={f}+1 WHERE browser_run_id=?",(rid,));conn.execute("UPDATE browser_platform_runs SET jobs_recorded=jobs_recorded+1 WHERE browser_run_id=? AND platform=?",(rid,source_site))
            if ledger_status=='new': conn.execute("UPDATE browser_search_tasks SET unique_jobs_recorded=unique_jobs_recorded+1 WHERE task_id=?",(tid,))
            else: conn.execute("UPDATE browser_search_tasks SET duplicate_sightings=duplicate_sightings+1 WHERE task_id=?",(tid,))
            jid=store.resolve_job_id(job)
            description_state='COMPLETE' if len(desc)>=250 else ('PARTIAL_TOO_SHORT' if desc else 'MISSING')
            conn.execute("UPDATE jobs SET description_state=? WHERE job_id=?",(description_state,jid))
            if result_id: finish_detail(conn,result_id,jid)
            else: conn.execute("UPDATE search_task_results SET canonical_job_id=?,detail_read=1,detail_status='COMPLETE',detail_completed_at=? WHERE task_id=? AND source_site=? AND source_job_id=? AND source_url=?",(jid,j.now_iso(),tid,source_site,sid,url))
            event(conn,rid,tid,'job_recorded',f'{ledger_status}: {job.title} — {job.company}',{'job_id':jid,'ledger_status':ledger_status,'recommendation':job.recommendation},out);refresh_result_reconciliation(conn,rid,tid);conn.commit();return {'ok':True,'ledger_status':ledger_status,'job_id':jid,'recommendation':job.recommendation,'title':job.title,'company':job.company}
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
            now=j.now_iso(); conn.execute("""UPDATE browser_search_tasks SET results_seen=MAX(results_seen,?),pages_visited=MAX(pages_visited,?),checkpoint_json=?,current_search_url=?,page_number=MAX(page_number,?),scroll_generation=MAX(scroll_generation,?),last_page_fingerprint=?,last_source_job_id=?,last_progress_at=?,lease_until=?,cards_extracted=MAX(cards_extracted,?),cards_persistence_attempted=MAX(cards_persistence_attempted,?),cards_persistence_succeeded=MAX(cards_persistence_succeeded,?),cards_persistence_failed=MAX(cards_persistence_failed,?),duplicate_cards=MAX(duplicate_cards,?),pending_details=MAX(pending_details,?),details_failed=MAX(details_failed,?) WHERE task_id=? AND browser_run_id=?""",(seen,pages,json.dumps(cp,ensure_ascii=False),j.clean_text(cp.get('search_url') or ''),int(cp.get('page_number') or pages),int(cp.get('scroll_generation') or 0),j.clean_text(cp.get('page_fingerprint') or ''),j.clean_text(cp.get('last_job_key') or ''),now,lease_time(lease_seconds),int(stats.get('extracted_cards') or 0),int(stats.get('persistence_attempted') or 0),int(stats.get('persistence_succeeded') or 0),int(stats.get('persistence_failed') or 0),int(stats.get('duplicate_cards') or 0),int(stats.get('pending_details') or 0),int(stats.get('details_failed') or 0),tid,rid));refresh_result_reconciliation(conn,rid,tid);conn.execute("UPDATE browser_runs SET last_progress_at=?,current_task_id=? WHERE browser_run_id=?",(now,tid,rid));conn.commit();return {'ok':True,'card_stats':stats}
        if action=='heartbeat':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);lease_seconds=int(cfg.get('runtime',{}).get('lease_seconds',180) or 180);now=j.now_iso();conn.execute("UPDATE browser_search_tasks SET last_progress_at=?,lease_until=? WHERE task_id=? AND browser_run_id=?",(now,lease_time(lease_seconds),tid,rid));conn.execute("UPDATE browser_runs SET last_progress_at=?,current_task_id=? WHERE browser_run_id=?",(now,tid,rid));conn.commit();return {'ok':True}
        if action=='browser_event':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);event_type=j.clean_text(msg.get('event_type') or 'browser_event');message=j.clean_text(msg.get('message') or event_type or 'browser event')
            if event_type=='task_active_time' and isinstance(msg.get('payload'),dict) and msg['payload'].get('metric_scope')=='task_attempt':
                try: active_ms=max(0,int(float(msg['payload'].get('task_active_browser_ms',0) or 0)))
                except (TypeError,ValueError): active_ms=0
                conn.execute("UPDATE browser_search_tasks SET task_active_browser_ms=task_active_browser_ms+? WHERE task_id=? AND browser_run_id=?",(active_ms,tid,rid))
            event(conn,rid,tid,event_type,message,msg,out);refresh_result_reconciliation(conn,rid,tid)
            if event_type == 'task_active_time':
                # complete_task changes terminal state but intentionally does
                # not finalize yield until this final attempt timing arrives.
                # The yield marker makes duplicate timing events harmless.
                record_task_yield(conn, tid, j.now_iso())
            conn.commit();return {'ok':True}
        if action=='complete_task':
            rid=int(msg.get('run_id') or 0);tid=int(msg.get('task_id') or 0);status=j.clean_text(msg.get('status') or 'completed');reason=j.clean_text(msg.get('reason') or '');exhausted=1 if bool(msg.get('exhausted')) else 0
            if status=='completed': status='exhausted' if exhausted else 'incomplete'
            if status=='test_limit': status='incomplete'
            allowed={'exhausted','incomplete','challenged','failed','stopped','auth_required','paused'};status=status if status in allowed else 'incomplete'
            t=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=? AND browser_run_id=?",(tid,rid)).fetchone()
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
            mark_definition_completed(conn, t, status, now)
            if status=='stopped':
                conn.execute("UPDATE browser_runs SET stop_requested=1,stop_after_current=0,last_error=? WHERE browser_run_id=?",(reason or 'stop after current requested',rid))
            event(conn,rid,tid,'task_'+status,reason,msg,out);conn.commit();return {'ok':True}
        if action=='should_stop':
            rid=int(msg.get('run_id') or 0);r=get_run(conn,rid);return {'ok':True,'stop':bool(r and int(r['stop_requested'] or 0)),'stop_after_current':bool(r and int(r['stop_after_current'] or 0))}
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
            # Crash-style recovery for the narrow gap between complete_task
            # and the final task_active_time event.
            backfill_task_yields(conn, rid, j.now_iso())
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
            tasks=conn.execute("SELECT task_id,platform,phase,canonical_title,query_text,search_band,cadence_hours,status,results_seen,detail_count_read,jobs_recorded,unique_jobs_recorded,duplicate_sightings,jobs_new,jobs_updated,jobs_unchanged,pages_visited,current_search_url,page_number,scroll_generation,last_page_fingerprint,last_source_job_id,challenge_reason,last_error,exhaustion_reason,safety_stop_reason,execution_rank,cards_extracted,cards_persistence_attempted,cards_persistence_succeeded,cards_persistence_failed,duplicate_cards,pending_details,details_failed FROM browser_search_tasks WHERE browser_run_id=? ORDER BY task_id",(rid,)).fetchall();plats=conn.execute("SELECT * FROM browser_platform_runs WHERE browser_run_id=? ORDER BY CASE platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 ELSE 2 END",(rid,)).fetchall();return {'ok':True,'run':{k:r[k] for k in r.keys()},'platforms':[{k:p[k] for k in p.keys()} for p in plats],'tasks':[{k:t[k] for k in t.keys()} for t in tasks]}
        if action=='task_status':
            tid=int(msg.get('task_id') or 0);t=conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=?",(tid,)).fetchone()
            if not t:return {'ok':False,'error':'task_not_found'}
            return {'ok':True,'task':{k:t[k] for k in t.keys()}}
        if action=='run_error':
            rid=int(msg.get('run_id') or 0);message=j.clean_text(msg.get('message') or 'extension error');conn.execute("UPDATE browser_runs SET last_error=?,last_progress_at=? WHERE browser_run_id=?",(message,j.now_iso(),rid));event(conn,rid,None,'run_error',message,msg,out);conn.commit();return {'ok':True}
        return {'ok':False,'error':'unknown_action','action':action}
    finally:
        if owns_store:
            store.close()


def handle(msg: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one RPC, durably replaying identified mutations exactly once.

    Direct unit callers without a request ID retain legacy semantics for
    compatibility. The HTTP bridge rejects such mutation requests before they
    reach this function, so every browser mutation is receipt-protected.
    """
    action = str(msg.get('action') or '')
    request_id = str(msg.get('request_id') or '').strip()
    if action not in MUTATING_ACTIONS or not request_id:
        return _handle_once(msg)
    store, cfg, strategy, out = open_store(defer_commits=True)
    conn = store.conn
    try:
        conn.execute('BEGIN IMMEDIATE')
        replay = _begin_receipt(conn, msg)
        if replay is not None:
            conn.rollback()
            return replay
        response = _handle_once(msg, (store, cfg, strategy, out))
        if not isinstance(response, dict):
            response = {'ok': False, 'error': 'invalid_response'}
        _finish_receipt(conn, msg, response)
        conn.durable_commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        store.close()
