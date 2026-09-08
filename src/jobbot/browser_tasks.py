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
from .search_plan import build_search_url, compile_plan, compile_staged_plan, normalize_search_query

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


def search_url(platform: str, query: str, days: int) -> str:
    return build_search_url(platform, normalize_search_query(query), days)


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
    if mode == "staged":
        planned = compile_staged_plan(bundle, chosen)
    elif mode == "staged_recent":
        planned = compile_staged_plan(bundle, chosen, ("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT"))
    elif mode == "staged_deep":
        planned = compile_staged_plan(bundle, chosen, ("C_DEEP_BACKFILL",))
    else:
        planned = compile_plan(bundle, mode, chosen)
    tasks = [{
        "task_key": task.task_key, "platform": task.platform, "query_text": task.query,
        "window_days": task.age_days, "search_profile": task.profile,
        "career_lane": task.lane, "resume_variant": task.resume_variant,
        "priority": task.priority, "search_url": task.search_url,
        "execution_rank": task.execution_rank, "phase": task.phase,
    } for task in planned]
    store = j.PrecisionStore(db); init_browser_schema(store.conn)
    now = j.now_iso()
    cur = store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, mode, ",".join(chosen), "queued", now,
         f"Platform-first exhaustive search. {len(tasks)} persistent tasks; no strategic result-count limit."),
    )
    rid = int(cur.lastrowid)
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
              search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,task_key,phase
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, t["platform"], t["query_text"], 1, t["window_days"], "date", t["search_url"], None, "queued", now,
             t["search_profile"], t["career_lane"], t["resume_variant"], t["priority"], t["execution_rank"], 1, t["task_key"], t["phase"]),
        )
    store.conn.commit(); store.close()
    return rid


def enqueue_gate(base: Path, platform: str = "indeed", days: int = 7, max_results: int = 20) -> int:
    queries = ["patient enrollment specialist", "patient access specialist", "healthcare operations coordinator"]
    db, _, _, _, _ = paths(base)
    store = j.PrecisionStore(db); init_browser_schema(store.conn); now = j.now_iso()
    rid = int(store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, "acceptance", platform, "queued", now, "Acceptance: auth + pagination + multi-query"),
    ).lastrowid)
    store.conn.execute("INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)", (rid, platform, len(queries)))
    for q in queries:
        store.conn.execute(
            """INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,status,created_at,search_profile,career_lane,priority)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, platform, q, 1, days, "date", search_url(platform, q, days), max_results, "queued", now,
             "acceptance-smoke", "healthcare_access", 0),
        )
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
    rid = int(store.conn.execute(
        "INSERT INTO browser_runs(version,mode,platform,status,created_at,notes) VALUES(?,?,?,?,?,?)",
        (V3_VERSION, "validation_micro", ",".join(chosen), "queued", now, "Bounded <=5 minute production proof; runtime bounds the validation; production retrieval remains uncapped."),
    ).lastrowid)
    store.conn.executemany("INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,1)", [(rid, p) for p in chosen])
    for platform in chosen:
        query = "patient enrollment specialist"
        store.conn.execute(
            """INSERT INTO browser_search_tasks(
              browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,status,created_at,
              search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,task_key,phase
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, platform, query, 1, 7, "date", search_url(platform, query, 7), max_results, "queued", now,
             "validation-micro", "HEALTHCARE_OPS_ACCESS", "enrollment_operations", 0, 1, 1,
             f"VALIDATION|{platform}|patient enrollment specialist", "A_FASTEST_DOOR_RECENT"),
        )
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
    exists = store.conn.execute("SELECT 1 FROM browser_runs WHERE browser_run_id=?", (rid,)).fetchone()
    if not exists:
        store.close(); raise RuntimeError(f"browser run not found: {rid}")
    now = j.now_iso()
    store.conn.execute(
        """UPDATE browser_search_tasks
           SET status='queued', completed_at=NULL, lease_owner='', lease_until=NULL,
               last_error='', safety_stop_reason=''
           WHERE browser_run_id=? AND (status IN ('running','stopped') OR
             (status='incomplete' AND safety_stop_reason NOT LIKE 'Acceptance limit reached%'))""", (rid,)
    )
    store.conn.execute(
        """UPDATE search_task_results SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL,
             detail_error=CASE WHEN detail_error='' THEN 'requeued after run interruption' ELSE detail_error END
           WHERE browser_run_id=? AND detail_status='RUNNING'""", (rid,)
    )
    store.conn.execute(
        """UPDATE browser_search_tasks SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,
             challenge_reason='',last_error=''
           WHERE browser_run_id=? AND status IN ('auth_required','deferred_by_platform') AND platform IN (
             SELECT platform FROM browser_platform_runs WHERE browser_run_id=? AND auth_status='not_authenticated'
           )""", (rid, rid)
    )
    store.conn.execute(
        """UPDATE browser_search_tasks SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,
             challenge_reason='',last_error=''
           WHERE browser_run_id=? AND status IN ('challenged','deferred_by_platform') AND platform IN (
             SELECT platform FROM browser_platform_runs
             WHERE browser_run_id=? AND COALESCE(cooldown_until,'')<=?
           )""", (rid, rid, now)
    )
    store.conn.execute(
        """UPDATE search_task_results SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL
           WHERE browser_run_id=? AND detail_status='EXTERNAL_BLOCKED' AND task_id IN (
             SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? AND status='queued'
           )""", (rid, rid)
    )
    store.conn.execute(
        "UPDATE browser_platform_runs SET auth_status='unchecked',auth_reason='' WHERE browser_run_id=? AND platform IN (SELECT DISTINCT platform FROM browser_search_tasks WHERE browser_run_id=? AND status='queued')",
        (rid, rid),
    )
    store.conn.execute(
        """UPDATE browser_runs SET status='queued', completed_at=NULL, stop_requested=0,
           stop_after_current=0, last_error='', last_progress_at=? WHERE browser_run_id=?""", (now, rid)
    )
    store.conn.commit(); store.close(); return int(rid)


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
    tasks=iter_strategy_tasks(strategy,'deep',list(PLATFORMS))
    unique_searches=set()
    for s0 in strategy.get('searches',[]):
        if not s0.get('enabled',True): continue
        days0=int(s0.get('bootstrap_backfill_days',30) or 30)
        for x in s0.get('keywords',[]):
            q0=j.clean_text(x).lower()
            if q0: unique_searches.add((q0,days0))
    keyword_count=len(unique_searches)
    expected=keyword_count*3
    assert len(tasks)==expected,(len(tasks),expected)
    assert all(t['search_url'].startswith('https://') for t in tasks)
    assert all(t['platform'] in PLATFORMS for t in tasks)
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
    print(f"V3 SELF-TEST PASSED — {keyword_count} researched keywords -> {expected} exhaustive Big-3 tasks in deep mode")
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
