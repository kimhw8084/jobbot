from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__, browser_tasks, legacy_engine
from .application import add_note, history, mark
from .acquisition.coordinator import acquire as run_acquisition
from .acquisition.providers import JSONFileProvider, JSONLProvider
from .audit import collect as collect_audit, render_terminal, write_reports
from .config import PROJECT_ROOT, load_bundle
from .dashboard import serve as serve_dashboard
from .db import Database
from .doctor import run as run_doctor
from .exports import export_all
from .funnel import analyze
from .ledger import open_ledger
from .orchestrator import _open_chrome, enqueue, launch_browser_run, refresh_extension, resume
from .run_now import ensure_dashboard, preflight
from .runtime_binding import bind_chrome_profile, sync_extension, stable_extension_root
from .watch import DEEP, RECENT, SUPPLEMENTAL, WatchScheduler
from .search_plan import compile_and_write, compile_staged_and_write, plan_counts
from .validator import run as run_validator


def _bundle():
    return load_bundle(PROJECT_ROOT)


def _connection(bundle):
    Database(bundle).migrate()
    return Database(bundle).connect()


def run_supplemental_stage(bundle, run_id: int | None, mode: str = "deep") -> bool:
    """Run configured supplemental/ATS retrieval as an isolated, visible stage."""
    conn = _connection(bundle)
    try:
        conn.execute("INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                     (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "supplemental_stage_started", "configured supplemental and ATS stage started", json.dumps({"mode": mode})))
        conn.commit()
    finally:
        conn.close()
    try:
        code = int(legacy_engine.run_search(bundle.legacy_runtime(), bundle.strategy, mode))
    except Exception as exc:
        code = 1
        message = f"supplemental stage failed: {type(exc).__name__}: {exc}"
    else:
        message = "supplemental stage completed" if code == 0 else f"supplemental stage returned exit code {code}"
    conn = _connection(bundle)
    try:
        conn.execute("INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                     (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "supplemental_stage_completed" if code == 0 else "supplemental_stage_failed", message, json.dumps({"mode": mode, "exit_code": code})))
        conn.commit()
    finally:
        conn.close()
    return code == 0


def command_search_plan(args: argparse.Namespace) -> int:
    bundle = _bundle()
    if args.mode == "staged":
        tasks, paths = compile_staged_and_write(bundle, args.platform or None, open_browser=args.open)
    else:
        tasks, paths = compile_and_write(bundle, args.mode, args.platform or None, open_browser=args.open)
    print(json.dumps(plan_counts(tasks), indent=2))
    for path in paths.values():
        print(path)
    return 0


def command_doctor(_args: argparse.Namespace) -> int:
    ok, checks = run_doctor(_bundle())
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    return 0 if ok else 1


def command_run(args: argparse.Namespace) -> int:
    bundle = _bundle()
    compile_and_write(bundle, args.mode, args.platform or None)
    run_id = enqueue(bundle, args.mode, args.platform or None)
    print(f"Enqueued browser run {run_id}", flush=True)
    if args.enqueue_only:
        return 0
    outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=not args.no_open)
    print(f"Browser run {run_id}: {outcome.status}; bridge restarts={outcome.bridge_restarts}")
    if not args.primary_only and outcome.status in {"completed", "partial"}:
        run_supplemental_stage(bundle, run_id, args.mode)
    return 0 if outcome.status == "completed" else 2


def command_run_now(args: argparse.Namespace) -> int:
    bundle = _bundle()
    check = preflight(bundle, args.platform or None, require_runtime_binding=True)
    dashboard_url, dashboard_started = ensure_dashboard(bundle, open_browser=not args.no_open)
    print(
        f"RUN NOW preflight passed: tasks={check.task_count} database={check.database_path}\n"
        f"Dashboard: {dashboard_url} ({'started' if dashboard_started else 'already running'})",
        flush=True,
    )
    run_id = enqueue(bundle, "staged", args.platform or None)
    print(f"Enqueued uncapped staged production browser run {run_id}", flush=True)
    if args.enqueue_only:
        return 0
    outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=not args.no_open)
    print(f"Browser run {run_id}: {outcome.status}; bridge restarts={outcome.bridge_restarts}", flush=True)
    # The bridge refreshes exports at terminal state. Preserve useful partial
    # output and distinguish external platform blockers with exit code 2.
    supplemental_ok = True
    if outcome.status in {"completed", "partial"}:
        supplemental_ok = run_supplemental_stage(bundle, run_id, "deep")
    return 0 if outcome.status == "completed" and supplemental_ok else 2


def command_acquire(args: argparse.Namespace) -> int:
    bundle = _bundle()
    if args.provider not in {"jsonl-file", "json-file"}:
        print("managed-http is an injected-transport adapter boundary; R1 exposes no live HTTP transport.", file=sys.stderr)
        return 2
    if not args.path:
        print("--path is required for the jsonl-file provider.", file=sys.stderr)
        return 2
    provider_type = JSONFileProvider if args.provider == "json-file" else JSONLProvider
    provider = provider_type(Path(args.path), run_id=args.provider_run_id or "")
    result = run_acquisition(bundle, provider, mode=args.mode, platforms=args.platform or None)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 2


def command_watch(args: argparse.Namespace) -> int:
    bundle = _bundle()
    scheduler_conn = _connection(bundle)
    runtime = bundle.runtime["runtime"]
    scheduler = WatchScheduler(scheduler_conn, recent_hours=int(runtime.get("watch_recent_hours", 6)),
                               deep_hours=int(runtime.get("watch_deep_hours", 24)),
                               supplemental_hours=int(runtime.get("watch_supplemental_hours", 6)))
    ensure_dashboard(bundle, open_browser=not args.no_open)
    # Invoking RUN_CONTINUOUS is the explicit restart operation for a latched
    # STOP_SEARCH. A running watcher never calls this path again by itself.
    scheduler.resume()
    try:
        while True:
            if scheduler.stopped():
                print("Watch is STOPPED; continuous automation will not restart itself.", flush=True)
                return 0
            plan = scheduler.due_plan()
            if not plan:
                if args.once:
                    return 0
                time.sleep(max(1, args.poll_seconds))
                if scheduler.stopped():
                    print("Watch stopped during wait; exiting without starting new work.", flush=True)
                continue
            has_browser = RECENT in plan or DEEP in plan
            run_id: int | None = None
            outcome = None
            browser_ok = True
            if has_browser:
                preflight(bundle, args.platform or None, require_runtime_binding=True)
                if RECENT in plan and DEEP in plan:
                    browser_mode = "staged"
                elif RECENT in plan:
                    browser_mode = "staged_recent"
                else:
                    browser_mode = "staged_deep"
                run_id = enqueue(bundle, browser_mode, args.platform or None)
                scheduler.start(plan, run_id)
                scheduler.bind_run(run_id)
                if args.enqueue_only:
                    print(f"Enqueued watch plan {plan} as browser run {run_id}; watcher remains checkpointed.", flush=True)
                    return 0
                outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=not args.no_open)
                if outcome.status in {"completed", "partial"}:
                    audit_conn = _connection(bundle)
                    try:
                        current_audit = collect_audit(audit_conn, run_id, bundle.strategy)
                    finally:
                        audit_conn.close()
                    browser_ok = current_audit["reconciliation"]["ok"] and current_audit["terminal_classification"] in {"COMPLETED_FULL", "COMPLETED_PARTIAL_EXTERNAL"}
                else:
                    browser_ok = False
            supplemental_ran = False
            supplemental_ok = True
            if SUPPLEMENTAL in plan:
                supplemental_ran = True
                supplemental_ok = run_supplemental_stage(bundle, run_id, "deep")
            ok = browser_ok and supplemental_ok
            scheduler.finish(plan, success=ok, supplemental_ran=supplemental_ran,
                             supplemental_success=supplemental_ok,
                             stopped=bool(outcome and outcome.status == "stopped"),
                             error="watch cycle incomplete" if not ok else "")
            print(f"Watch plan {plan}: browser={outcome.status if outcome else 'not-run'} supplemental={'ok' if supplemental_ok else 'failed'}", flush=True)
            if args.once:
                return 0 if ok else 2
            if not ok and not scheduler.stopped():
                time.sleep(max(1, args.poll_seconds))
    except KeyboardInterrupt:
        scheduler.stop("watch interrupted by user")
        raise
    finally:
        scheduler_conn.close()


def command_resume(args: argparse.Namespace) -> int:
    bundle = _bundle()
    run_id = resume(bundle, args.run_id)
    print(f"Resuming browser run {run_id}", flush=True)
    if args.enqueue_only:
        return 0
    outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=not args.no_open)
    print(f"Browser run {run_id}: {outcome.status}; bridge restarts={outcome.bridge_restarts}")
    return 0 if outcome.status == "completed" else 2


def command_re_enrich(args: argparse.Namespace) -> int:
    result = browser_tasks.requeue_missing_enrichment(_bundle().root, args.run_id, args.platform)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_stop(args: argparse.Namespace) -> int:
    bundle = _bundle()
    code = browser_tasks.emergency_stop(bundle.root, args.run_id) if args.emergency else browser_tasks.request_stop(bundle.root, args.run_id)
    conn = _connection(bundle)
    try:
        WatchScheduler(conn).stop("STOP_SEARCH requested")
    finally:
        conn.close()
    return code


def command_dashboard(args: argparse.Namespace) -> int:
    serve_dashboard(_bundle(), port=args.port, open_browser=not args.no_open)
    return 0


def command_audit(_args: argparse.Namespace) -> int:
    bundle = _bundle(); conn = _connection(bundle)
    try:
        audit = collect_audit(conn, strategy=bundle.strategy)
    finally:
        conn.close()
    print(render_terminal(audit))
    for path in write_reports(audit, bundle.output_dir).values():
        print(path)
    return 0


def command_export(args: argparse.Namespace) -> int:
    bundle = _bundle(); conn = _connection(bundle)
    try:
        paths = export_all(conn, bundle.output_dir, batch_size=args.batch_size)
    finally:
        conn.close()
    for path in paths.values(): print(path)
    return 0


def command_application(args: argparse.Namespace) -> int:
    bundle = _bundle(); conn = _connection(bundle)
    try:
        if args.application_command == "mark":
            event = mark(conn, args.job_id, args.status, notes=args.notes, source="cli"); print(json.dumps(event.__dict__, indent=2))
        elif args.application_command == "note":
            event = add_note(conn, args.job_id, args.note, source="cli"); print(json.dumps(event.__dict__, indent=2))
        else:
            for event in history(conn, args.job_id): print(json.dumps(event.__dict__, ensure_ascii=False))
    finally:
        conn.close()
    return 0


def command_funnel(_args: argparse.Namespace) -> int:
    bundle = _bundle(); conn = _connection(bundle)
    try: rows = analyze(conn)
    finally: conn.close()
    print("dimension\tvalue\tapplications\tsuccesses\traw_rate\tadjusted_rate\tstable")
    for row in rows:
        print(f"{row.dimension}\t{row.value}\t{row.applications}\t{row.successes}\t{row.raw_rate:.3f}\t{row.adjusted_rate:.3f}\t{row.stable}")
    return 0


def command_acceptance(args: argparse.Namespace) -> int:
    if not args.use_production:
        os.environ.setdefault("JOBBOT_DATABASE_PATH", "data/acceptance.sqlite3")
        os.environ.setdefault("JOBBOT_OUTPUT_DIR", "out/acceptance")
    bundle = _bundle()
    run_id = browser_tasks.enqueue_gate(bundle.root, args.platform, args.days, args.max_results)
    print(f"Enqueued {args.platform} acceptance run {run_id}", flush=True)
    if args.enqueue_only: return 0
    outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=not args.no_open)
    print(json.dumps(outcome.__dict__, indent=2))
    return 0 if outcome.status == "completed" else 2


def command_import(args: argparse.Namespace) -> int:
    return browser_tasks.import_database(_bundle().root, Path(args.path))


def command_validate_production(args: argparse.Namespace) -> int:
    return run_validator(semi_minutes=args.semi_minutes, stage=args.stage)


def command_refresh_extension(args: argparse.Namespace) -> int:
    result = refresh_extension(_bundle(), timeout_seconds=args.timeout, open_browser=not args.no_open)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") and result.get("identity_confirmed") is True else 2


def command_sync_extension(_args: argparse.Namespace) -> int:
    info = sync_extension(PROJECT_ROOT)
    print(json.dumps({
        "ok": True,
        "classification": "deployment_source_current",
        "deployment": info.as_dict(),
    }, indent=2, ensure_ascii=False))
    return 0


def command_bootstrap_extension(args: argparse.Namespace) -> int:
    info = sync_extension(PROJECT_ROOT)
    binding = bind_chrome_profile(args.profile_directory)
    result = {
        "ok": True,
        "classification": "bootstrap_complete_load_unpacked_once",
        "deployment": info.as_dict(),
        "chrome_profile_binding": binding,
        "next_step": f"In the targeted Chrome profile, load unpacked: {stable_extension_root()}",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.no_open:
        _open_chrome("chrome://extensions/")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="jobbot", description=f"JobBot v{__version__} local remote-career search")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor"); doctor.set_defaults(func=command_doctor)
    plan = sub.add_parser("search-plan"); plan.add_argument("--mode", choices=("fast", "deep", "staged"), default="deep"); plan.add_argument("--platform", action="append", choices=browser_tasks.PLATFORMS); plan.add_argument("--open", action="store_true"); plan.set_defaults(func=command_search_plan)
    run = sub.add_parser("run"); run.add_argument("--mode", choices=("fast", "deep"), default="fast"); run.add_argument("--platform", action="append", choices=browser_tasks.PLATFORMS); run.add_argument("--enqueue-only", action="store_true"); run.add_argument("--no-open", action="store_true"); run.add_argument("--primary-only", action="store_true"); run.set_defaults(func=command_run)
    run_now = sub.add_parser("run-now"); run_now.add_argument("--platform", action="append", choices=browser_tasks.PLATFORMS); run_now.add_argument("--enqueue-only", action="store_true"); run_now.add_argument("--no-open", action="store_true"); run_now.set_defaults(func=command_run_now)
    acquire = sub.add_parser("acquire", help="ingest through acquisition-v2 using an offline provider file")
    acquire.add_argument("--provider", choices=("jsonl-file", "json-file", "managed-http"), default="jsonl-file")
    acquire.add_argument("--path", help="JSON/JSONL fixture/import file; required for a file provider")
    acquire.add_argument("--provider-run-id", default="")
    acquire.add_argument("--mode", choices=("staged", "staged_recent", "staged_deep", "fast", "deep"), default="staged")
    acquire.add_argument("--platform", action="append", choices=browser_tasks.PLATFORMS)
    acquire.set_defaults(func=command_acquire)
    watch = sub.add_parser("watch"); watch.add_argument("--platform", action="append", choices=browser_tasks.PLATFORMS); watch.add_argument("--once", action="store_true"); watch.add_argument("--enqueue-only", action="store_true"); watch.add_argument("--poll-seconds", type=int, default=60); watch.add_argument("--no-open", action="store_true"); watch.set_defaults(func=command_watch)
    resume_p = sub.add_parser("resume"); resume_p.add_argument("--run-id", type=int); resume_p.add_argument("--enqueue-only", action="store_true"); resume_p.add_argument("--no-open", action="store_true"); resume_p.set_defaults(func=command_resume)
    enrich = sub.add_parser("re-enrich", help="queue user-invoked detail re-enrichment for durable identity-only discoveries"); enrich.add_argument("--run-id", type=int); enrich.add_argument("--platform", action="append", choices=browser_tasks.PLATFORMS); enrich.set_defaults(func=command_re_enrich)
    stop = sub.add_parser("stop"); stop.add_argument("--run-id", type=int); stop.add_argument("--emergency", action="store_true"); stop.set_defaults(func=command_stop)
    dashboard = sub.add_parser("dashboard"); dashboard.add_argument("--port", type=int); dashboard.add_argument("--no-open", action="store_true"); dashboard.set_defaults(func=command_dashboard)
    audit = sub.add_parser("audit"); audit.set_defaults(func=command_audit)
    export = sub.add_parser("export"); export.add_argument("--batch-size", type=int, default=20); export.set_defaults(func=command_export)
    application = sub.add_parser("application"); app_sub = application.add_subparsers(dest="application_command", required=True)
    app_mark = app_sub.add_parser("mark"); app_mark.add_argument("job_id"); app_mark.add_argument("status"); app_mark.add_argument("--notes", default=""); app_mark.set_defaults(func=command_application)
    app_note = app_sub.add_parser("note"); app_note.add_argument("job_id"); app_note.add_argument("note"); app_note.set_defaults(func=command_application)
    app_history = app_sub.add_parser("history"); app_history.add_argument("job_id"); app_history.set_defaults(func=command_application)
    funnel = sub.add_parser("funnel"); funnel.set_defaults(func=command_funnel)
    acceptance = sub.add_parser("acceptance"); acceptance.add_argument("--platform", choices=browser_tasks.PLATFORMS, required=True); acceptance.add_argument("--days", type=int, default=7); acceptance.add_argument("--max-results", type=int, default=20); acceptance.add_argument("--enqueue-only", action="store_true"); acceptance.add_argument("--no-open", action="store_true"); acceptance.add_argument("--use-production", action="store_true"); acceptance.set_defaults(func=command_acceptance)
    importer = sub.add_parser("import-db"); importer.add_argument("path"); importer.set_defaults(func=command_import)
    validator = sub.add_parser("validate-production"); validator.add_argument("--stage", choices=("micro", "full"), default="full"); validator.add_argument("--semi-minutes", type=int, default=30); validator.set_defaults(func=command_validate_production)
    refresh = sub.add_parser("refresh-extension", help="refresh the installed unpacked extension and confirm its manifest build"); refresh.add_argument("--timeout", type=float, default=45); refresh.add_argument("--no-open", action="store_true"); refresh.set_defaults(func=command_refresh_extension)
    sync = sub.add_parser("sync-extension", help="copy the exact integrated extension source to the stable machine-local deployment")
    sync.set_defaults(func=command_sync_extension)
    bootstrap = sub.add_parser("bootstrap-extension", help="perform the one-time stable-source and Chrome-profile binding bootstrap")
    bootstrap.add_argument("--profile-directory", required=True, help="Chrome profile directory from chrome://version, for example Default or Profile 1")
    bootstrap.add_argument("--no-open", action="store_true")
    bootstrap.set_defaults(func=command_bootstrap_extension)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted; persistent search state remains resumable.", file=sys.stderr); return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr); return 1
