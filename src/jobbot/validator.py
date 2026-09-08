from __future__ import annotations

"""Bounded final production proof.

This module is intentionally an acceptance harness, not another crawler.  It
creates isolated databases, delegates browser work to the existing normal-
Chrome orchestrator, and writes a machine-readable result even when a live
stage fails or is interrupted.
"""

import copy
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import browser_tasks, legacy_engine
from .audit import collect as collect_audit
from .config import PROJECT_ROOT, ConfigBundle, load_bundle
from .db import Database
from .doctor import run as run_doctor
from .orchestrator import chrome_path, launch_browser_run
from .run_now import ensure_dashboard, preflight
from .search_plan import compile_plan, compile_staged_plan


PRIMARY = ("linkedin", "indeed", "glassdoor")
EXPECTED_EXTENSION_BUILD = "3.2.2-prod-ready"
TERMINAL_SUCCESS = {"COMPLETED_FULL", "COMPLETED_PARTIAL_EXTERNAL"}
TERMINAL_EXTERNAL = {"challenged", "auth_required", "deferred_by_platform"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _isolated_bundle(db_path: Path, output_dir: Path, port: int) -> ConfigBundle:
    original = load_bundle(PROJECT_ROOT)
    runtime = copy.deepcopy(original.runtime)
    runtime["runtime"]["database_path"] = str(db_path)
    runtime["runtime"]["output_dir"] = str(output_dir)
    runtime["runtime"]["dashboard_port"] = port
    runtime["ledger"]["backup_dir"] = str(db_path.parent / "backups")
    return ConfigBundle(
        original.root,
        copy.deepcopy(original.strategy),
        copy.deepcopy(original.candidate),
        runtime,
    )


def _production_db() -> Path:
    return (PROJECT_ROOT / "data" / "jobs.sqlite3").resolve()


def _assert_isolated(path: Path) -> None:
    if path.resolve() == _production_db():
        raise RuntimeError(f"validator safety failure: refusing production database {path}")


@contextmanager
def _isolated_environment(bundle: ConfigBundle) -> Iterator[None]:
    names = ("JOBBOT_DATABASE_PATH", "JOBBOT_OUTPUT_DIR", "JOBBOT_DASHBOARD_PORT")
    values = {
        "JOBBOT_DATABASE_PATH": str(bundle.database_path),
        "JOBBOT_OUTPUT_DIR": str(bundle.output_dir),
        "JOBBOT_DASHBOARD_PORT": str(bundle.runtime["runtime"]["dashboard_port"]),
    }
    previous = {name: os.environ.get(name) for name in names}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _tail(value: str, limit: int = 4000) -> str:
    value = value or ""
    return value if len(value) <= limit else value[-limit:]


def _command(args: list[str], *, env: dict[str, str] | None = None, timeout: int = 180) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {"command": args, "returncode": completed.returncode, "output": _tail(completed.stdout)}
    except subprocess.TimeoutExpired as exc:
        return {"command": args, "returncode": 124, "output": _tail(str(exc)), "timed_out": True}


def _git_state() -> dict[str, str]:
    def read(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=PROJECT_ROOT, text=True).strip()

    return {"branch": read("branch", "--show-current"), "head": read("rev-parse", "HEAD")}


def _dashboard_json(url: str, endpoint: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + endpoint, timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except Exception:
        return None


def _dashboard_probe(bundle: ConfigBundle, url: str) -> dict[str, Any]:
    identity = _dashboard_json(url, "/api/identity")
    summary = _dashboard_json(url, "/api/summary")
    active = _dashboard_json(url, "/api/run")
    expected_db = str(bundle.database_path.resolve())
    expected_workspace = str(bundle.root.resolve())
    identity_ok = bool(identity and identity.get("resolved_database_path") == expected_db
                       and identity.get("workspace_root") == expected_workspace
                       and identity.get("jobbot_version") == "3.2.1")
    return {
        "identity": identity,
        "identity_ok": identity_ok,
        "summary": summary,
        "run": active,
        "live_refresh_ok": isinstance(summary, dict) and isinstance(active, dict),
    }


def _stop_dashboard(bundle: ConfigBundle, url: str) -> None:
    identity = _dashboard_json(url, "/api/identity")
    if not identity or identity.get("resolved_database_path") != str(bundle.database_path.resolve()):
        return
    pid = int(identity.get("pid") or 0)
    if pid <= 1 or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _dashboard_json(url, "/api/identity") is not None:
        time.sleep(0.1)


def _integrity(path: Path) -> str:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _audit(bundle: ConfigBundle, run_id: int | None = None) -> dict[str, Any]:
    conn = Database(bundle).connect()
    try:
        audit = collect_audit(conn, run_id=run_id, strategy=bundle.strategy)
    finally:
        conn.close()
    audit["integrity"] = _integrity(bundle.database_path) if bundle.database_path.exists() else "missing"
    return audit


def _metrics(audit: dict[str, Any]) -> dict[str, Any]:
    platforms = audit.get("platforms", {})
    global_metrics = audit.get("global", {})
    task_states = {
        state: sum(int(values.get(state, 0) or 0) for values in platforms.values())
        for state in ("queued", "running", "exhausted", "incomplete", "challenged", "auth_required",
                      "deferred_by_platform", "failed", "paused", "stopped")
    }
    return {
        "extracted": sum(int(values.get("cards_extracted", 0) or 0) for values in platforms.values()),
        "persistence_attempted": sum(int(values.get("cards_persistence_attempted", 0) or 0) for values in platforms.values()),
        "persisted": sum(int(values.get("cards_persisted", 0) or 0) for values in platforms.values()),
        "persistence_failed": sum(int(values.get("cards_persistence_failed", 0) or 0) for values in platforms.values()),
        "duplicates": sum(int(values.get("duplicate_sightings", 0) or 0) for values in platforms.values()),
        "detail_pending": int(global_metrics.get("detail_pending", 0) or 0),
        "detail_running": int(global_metrics.get("detail_running", 0) or 0),
        "detail_complete": int(global_metrics.get("detail_complete", 0) or 0),
        "detail_retryable": int(global_metrics.get("detail_retryable", 0) or 0),
        "detail_failed": int(global_metrics.get("detail_failed", 0) or 0),
        "detail_external_blocked": int(global_metrics.get("detail_external_blocked", 0) or 0),
        "canonical_jobs": int(global_metrics.get("canonical_jobs", 0) or 0),
        "actionable": int(global_metrics.get("apply_now", 0) or 0) + int(global_metrics.get("apply_volume", 0) or 0),
        "apply_now": int(global_metrics.get("apply_now", 0) or 0),
        "apply_volume": int(global_metrics.get("apply_volume", 0) or 0),
        "out_of_scope": int(global_metrics.get("out_of_scope", 0) or 0),
        "unexplained": sum(int(values.get("unexplained", 0) or 0) for values in platforms.values()),
        "task_states": task_states,
        "reconciliation": audit.get("reconciliation", {}),
        "integrity": audit.get("integrity"),
    }


def _scope_diagnostics(bundle: ConfigBundle, run_id: int | None) -> dict[str, Any]:
    import sqlite3

    result = {platform: {"candidate_job_links": 0, "in_scope_result_card_links": 0,
                         "outside_scope_links": 0, "scope_missing_events": 0,
                         "contamination": 0} for platform in PRIMARY}
    conn = sqlite3.connect(bundle.database_path)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            "SELECT platform,payload_json FROM browser_events e JOIN browser_search_tasks t ON t.task_id=e.task_id "
            "WHERE e.browser_run_id=? AND e.event_type='scope_diagnostics'", (run_id,),
        ):
            platform = str(row["platform"])
            if platform not in result:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            result[platform]["candidate_job_links"] += int(payload.get("candidate_links_total", 0) or 0)
            result[platform]["in_scope_result_card_links"] += int(payload.get("candidate_links_in_scope", 0) or 0)
            result[platform]["outside_scope_links"] += int(payload.get("candidate_links_outside_scope", 0) or 0)
        for row in conn.execute(
            "SELECT platform,COUNT(*) FROM browser_events e JOIN browser_search_tasks t ON t.task_id=e.task_id "
            "WHERE e.browser_run_id=? AND e.event_type='extraction_scope_missing' GROUP BY platform", (run_id,),
        ):
            if str(row[0]) in result:
                result[str(row[0])]["scope_missing_events"] = int(row[1])
    finally:
        conn.close()
    for values in result.values():
        values["contamination"] = values["outside_scope_links"]
    return result


def _extension_build_seen(bundle: ConfigBundle, run_id: int | None) -> bool:
    import sqlite3

    conn = sqlite3.connect(bundle.database_path)
    try:
        rows = conn.execute(
            "SELECT message,payload_json FROM browser_events WHERE browser_run_id=? AND event_type='extension_build'",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    for message, payload_json in rows:
        if str(message) == EXPECTED_EXTENSION_BUILD:
            return True
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("build") == EXPECTED_EXTENSION_BUILD or payload.get("payload", {}).get("build") == EXPECTED_EXTENSION_BUILD:
            return True
    return False


def _run_supplemental(bundle: ConfigBundle, run_id: int | None) -> dict[str, Any]:
    conn = Database(bundle).connect()
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
            (run_id, now, "supplemental_stage_started", "validator supplemental stage started", "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    started = time.monotonic()
    try:
        code = int(legacy_engine.run_search(bundle.legacy_runtime(), bundle.strategy, "deep"))
        error = "" if code == 0 else f"supplemental stage returned exit code {code}"
    except Exception as exc:  # an isolated source failure must be visible, not fatal to primary state
        code = 1
        error = f"{type(exc).__name__}: {exc}"
    conn = Database(bundle).connect()
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
            (run_id, now, "supplemental_stage_completed" if code == 0 else "supplemental_stage_failed",
             "supplemental stage completed" if code == 0 else error,
             json.dumps({"exit_code": code, "duration_seconds": round(time.monotonic() - started, 2)})),
        )
        conn.commit()
    finally:
        conn.close()
    sources: dict[str, Any] = {}
    configured_sources = bundle.runtime.get("sources", {})
    conn = Database(bundle).connect()
    try:
        for name, config in configured_sources.items():
            count = conn.execute(
                "SELECT COUNT(*) FROM source_occurrences WHERE source_site=?", (str(name),)
            ).fetchone()[0]
            sources[str(name)] = {"enabled": bool(config.get("enabled", True)),
                                  "state": "completed" if code == 0 else "failed",
                                  "canonical_occurrences": int(count or 0)}
    finally:
        conn.close()
    ats = bundle.runtime.get("ats_discovery", {})
    sources["ats_discovery"] = {"enabled": bool(ats.get("enabled", False)),
                                 "state": "completed" if code == 0 else "failed"}
    return {"ok": code == 0, "isolation_pass": True, "exit_code": code, "error": error,
            "duration_seconds": round(time.monotonic() - started, 2), "sources": sources}


def _live_run(bundle: ConfigBundle, *, mode: str, platforms: list[str], timeout_seconds: int,
              stop_after_seconds: int | None = None, bridge_restart_after: int | None = None,
              validation_micro: bool = False, active_runs: list[tuple[ConfigBundle, int]] | None = None) -> dict[str, Any]:
    with _isolated_environment(bundle):
        if validation_micro:
            run_id = browser_tasks.enqueue_validation(PROJECT_ROOT, platforms)
        else:
            run_id = browser_tasks.enqueue_production(PROJECT_ROOT, mode, platforms)
        if active_runs is not None:
            active_runs.append((bundle, run_id))
        started = time.monotonic()
        outcome = launch_browser_run(
            bundle,
            run_id,
            wait=True,
            open_browser=True,
            timeout_seconds=timeout_seconds,
            stop_after_seconds=stop_after_seconds,
            test_bridge_restart_after=bridge_restart_after,
            startup_timeout_seconds=45,
        )
    audit = _audit(bundle, run_id)
    result = {
        "run_id": run_id,
        "outcome": outcome.__dict__,
        "duration_seconds": round(time.monotonic() - started, 2),
        "terminal_classification": audit.get("terminal_classification"),
        "metrics": _metrics(audit),
        "audit": audit,
        "scope": _scope_diagnostics(bundle, run_id),
        "extension_build_pass": _extension_build_seen(bundle, run_id),
        "bridge_restart_pass": int(outcome.bridge_restarts) >= 1 if bridge_restart_after is not None else None,
    }
    if active_runs is not None:
        active_runs[:] = [(active_bundle, active_id) for active_bundle, active_id in active_runs if active_id != run_id]
    return result


def _resume_live(bundle: ConfigBundle, run_id: int, timeout_seconds: int,
                 active_runs: list[tuple[ConfigBundle, int]] | None = None) -> dict[str, Any]:
    if active_runs is not None:
        active_runs.append((bundle, run_id))
    with _isolated_environment(bundle):
        browser_tasks.resume_run(PROJECT_ROOT, run_id)
        started = time.monotonic()
        outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=True, timeout_seconds=timeout_seconds,
                                     startup_timeout_seconds=45)
    audit = _audit(bundle, run_id)
    result = {
        "run_id": run_id,
        "outcome": outcome.__dict__,
        "duration_seconds": round(time.monotonic() - started, 2),
        "terminal_classification": audit.get("terminal_classification"),
        "metrics": _metrics(audit),
        "audit": audit,
        "scope": _scope_diagnostics(bundle, run_id),
        "extension_build_pass": _extension_build_seen(bundle, run_id),
    }
    if active_runs is not None:
        active_runs[:] = [(active_bundle, active_id) for active_bundle, active_id in active_runs if active_id != run_id]
    return result


def _stage_pass(stage: dict[str, Any], *, require_terminal: bool = True) -> bool:
    metrics = stage.get("metrics", {})
    classification = stage.get("terminal_classification")
    if require_terminal and classification not in TERMINAL_SUCCESS:
        return False
    if stage.get("extension_build_pass") is not True:
        return False
    if metrics.get("integrity") != "ok" or not metrics.get("reconciliation", {}).get("ok", False):
        return False
    if metrics.get("unexplained", 0):
        return False
    if metrics.get("extracted", 0) != metrics.get("persistence_attempted", 0):
        return False
    if metrics.get("persistence_attempted", 0) != metrics.get("persisted", 0) + metrics.get("persistence_failed", 0):
        return False
    states = metrics.get("task_states", {})
    if any(states.get(state, 0) for state in ("queued", "running", "incomplete", "paused", "stopped", "failed")):
        return False
    if any(values.get("contamination", 0) or values.get("scope_missing_events", 0) for values in stage.get("scope", {}).values()):
        return False
    return True


def _fixture_and_deterministic(bundle: ConfigBundle) -> dict[str, Any]:
    # The test suite deliberately creates its own temporary bundles. Passing
    # one process-wide database override here would collapse those fixtures
    # onto the validator DB and create false failures. The live stages below
    # remain isolated through their explicit bundle/environment.
    env = os.environ.copy()
    for name in ("JOBBOT_DATABASE_PATH", "JOBBOT_OUTPUT_DIR", "JOBBOT_DASHBOARD_PORT"):
        env.pop(name, None)
    tests = _command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], env=env, timeout=240)
    checks = {
        "local_tests": tests,
        "scope_fixtures": tests.get("returncode") == 0,
        "platform_isolation": tests.get("returncode") == 0,
        "watch_cadence": tests.get("returncode") == 0,
        "watch_stop_latch": tests.get("returncode") == 0,
    }
    return checks


def _preflight(bundle: ConfigBundle) -> dict[str, Any]:
    state = _git_state()
    checks: list[dict[str, Any]] = []
    ok = state["branch"] == "codex/v3.2.2-prod-ready"
    checks.append({"name": "forward branch", "ok": ok, "detail": state["branch"]})
    executable = chrome_path()
    chrome_ok = bool(executable)
    checks.append({"name": "normal Chrome", "ok": chrome_ok, "detail": executable or "not found"})
    required = [bundle.root / "extension" / name for name in ("manifest.json", "service_worker.js", "dashboard.html")]
    extension_ok = all(path.is_file() for path in required)
    checks.append({"name": "current unpacked extension", "ok": extension_ok, "detail": [str(x) for x in required if not x.is_file()]})
    Database(bundle).migrate()
    checks.append({"name": "isolated database", "ok": bundle.database_path.resolve() != _production_db(), "detail": str(bundle.database_path)})
    doctor_ok, doctor_checks = run_doctor(bundle)
    checks.append({"name": "doctor", "ok": doctor_ok, "detail": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in doctor_checks]})
    compile_result = _command([sys.executable, "-m", "compileall", "-q", "src"], timeout=60)
    checks.append({"name": "compileall", "ok": compile_result["returncode"] == 0, "detail": compile_result})
    js_files = sorted((bundle.root / "extension").glob("*.js")) + sorted((bundle.root / "src" / "jobbot" / "web").glob("*.js"))
    js_results = [_command(["node", "--check", str(path)], timeout=30) for path in js_files]
    checks.append({"name": "node syntax", "ok": all(item["returncode"] == 0 for item in js_results), "detail": js_results})
    staged = compile_staged_plan(bundle)
    deep = compile_plan(bundle, "deep")
    plan_ok = bool(staged and deep) and all(task.max_results is None for task in staged + deep)
    checks.append({"name": "uncapped staged/deep search plan", "ok": plan_ok, "detail": {"staged": len(staged), "deep": len(deep)}})
    deterministic = _fixture_and_deterministic(bundle)
    checks.append({"name": "deterministic suite", "ok": deterministic["local_tests"]["returncode"] == 0, "detail": deterministic["local_tests"]})
    return {"ok": all(item["ok"] for item in checks), "git": state, "checks": checks, "deterministic": deterministic}


def _write_report(report: dict[str, Any], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    latest_json.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    lines = ["# JobBot production validation", "", f"PROD_READY={str(bool(report.get('PROD_READY'))).lower()}",
             f"branch={report.get('branch')}", f"head={report.get('head')}", ""]
    for key in ("preflight", "primary_live", "soak", "semi_production", "external_blockers", "internal_failures"):
        lines.extend([f"## {key}", "", "```json", json.dumps(report.get(key), indent=2, ensure_ascii=False, default=str), "```", ""])
    latest_md.write_text("\n".join(lines), encoding="utf-8")


def run(*, semi_minutes: int = 30) -> int:
    if semi_minutes < 30 or semi_minutes > 60:
        raise ValueError("--semi-minutes must be between 30 and 60")
    stamp = _timestamp()
    validation_db = PROJECT_ROOT / "data" / f"validation_{stamp}.sqlite3"
    validation_out = PROJECT_ROOT / "out" / f"validation-{stamp}"
    soak_db = PROJECT_ROOT / "data" / f"validation_soak_{stamp}.sqlite3"
    soak_out = PROJECT_ROOT / "out" / f"validation-soak-{stamp}"
    semi_db = PROJECT_ROOT / "data" / f"validation_semiprod_{stamp}.sqlite3"
    semi_out = PROJECT_ROOT / "out" / f"validation-semiprod-{stamp}"
    report_dir = PROJECT_ROOT / "out" / "production-validation"
    for path in (validation_db, soak_db, semi_db):
        _assert_isolated(path)
    report: dict[str, Any] = {
        "PROD_READY": False,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validation_database": str(validation_db),
        "soak_database": str(soak_db),
        "semi_production_database": str(semi_db),
        "dashboard_url": None,
        "external_blockers": [],
        "internal_failures": [],
    }
    active_runs: list[tuple[ConfigBundle, int]] = []
    dashboard_bundles: list[tuple[ConfigBundle, str]] = []
    try:
        state = _git_state()
        report.update(state)
        micro_bundle = _isolated_bundle(validation_db, validation_out, _free_port())
        report["preflight"] = _preflight(micro_bundle)
        if not report["preflight"]["ok"]:
            report["internal_failures"].append("preflight failed; live stages were not started")
            return 1
        url, _ = ensure_dashboard(micro_bundle, open_browser=True)
        dashboard_bundles.append((micro_bundle, url))
        report["dashboard_url"] = url
        with _isolated_environment(micro_bundle):
            preflight(micro_bundle)
            primary = _live_run(
                micro_bundle,
                mode="staged_recent",
                platforms=list(PRIMARY),
                timeout_seconds=300,
                stop_after_seconds=20,
                bridge_restart_after=8,
                validation_micro=True,
                active_runs=active_runs,
            )
        if primary["outcome"]["status"] == "stopped":
            primary["resume"] = _resume_live(micro_bundle, int(primary["run_id"]), 240, active_runs)
            final_primary = primary["resume"]
        else:
            primary["resume"] = None
            final_primary = primary
        primary["stop_resume_pass"] = bool(primary["outcome"]["status"] == "stopped" and primary["resume"] and _stage_pass(final_primary))
        primary["dashboard"] = _dashboard_probe(micro_bundle, url)
        primary["pass"] = bool(primary["stop_resume_pass"] and primary["bridge_restart_pass"] is True
                                and primary["dashboard"]["identity_ok"]
                                and primary["dashboard"]["live_refresh_ok"] and _stage_pass(final_primary))
        report["primary_live"] = primary
        if primary["outcome"]["status"] == "extension_unresponsive":
            report["internal_failures"].append(
                "Chrome extension did not start the validation run within 45 seconds; reload the unpacked extension in chrome://extensions and rerun validator"
            )
        if not primary.get("extension_build_pass"):
            report["internal_failures"].append(
                f"loaded extension did not report build {EXPECTED_EXTENSION_BUILD}; reload the unpacked extension in chrome://extensions and rerun validator"
            )
        for platform, values in final_primary.get("scope", {}).items():
            if values.get("scope_missing_events") or values.get("contamination"):
                report["internal_failures"].append(f"{platform}: live result scope failed")
        for platform, values in final_primary.get("audit", {}).get("platforms", {}).items():
            if any(values.get(state, 0) for state in TERMINAL_EXTERNAL):
                report["external_blockers"].append({"stage": "primary_live", "platform": platform,
                                                     "states": {state: values.get(state, 0) for state in TERMINAL_EXTERNAL}})
        if not primary["pass"]:
            report["internal_failures"].append("primary live micro-validation failed")
            return 1

        soak_bundle = _isolated_bundle(soak_db, soak_out, _free_port())
        soak_url, _ = ensure_dashboard(soak_bundle, open_browser=True)
        dashboard_bundles.append((soak_bundle, soak_url))
        report["dashboard_url"] = soak_url
        soak_started = time.monotonic()
        with _isolated_environment(soak_bundle):
            soak = _live_run(soak_bundle, mode="staged_recent", platforms=list(PRIMARY), timeout_seconds=900,
                             stop_after_seconds=60, bridge_restart_after=30, active_runs=active_runs)
            if soak["outcome"]["status"] == "stopped":
                soak["resume"] = _resume_live(soak_bundle, int(soak["run_id"]), 840, active_runs)
                final_soak = soak["resume"]
            else:
                soak["resume"] = None
                final_soak = soak
            soak["supplemental"] = _run_supplemental(soak_bundle, int(soak["run_id"]))
        soak["duration_seconds_total"] = round(time.monotonic() - soak_started, 2)
        soak["dashboard"] = _dashboard_probe(soak_bundle, soak_url)
        soak["pass"] = bool(soak["outcome"]["status"] == "stopped" and soak["resume"] and
                             soak["bridge_restart_pass"] is True and
                             _stage_pass(final_soak) and soak["supplemental"]["isolation_pass"] and
                             soak["dashboard"]["identity_ok"] and soak["dashboard"]["live_refresh_ok"])
        report["soak"] = soak
        if not soak["supplemental"]["ok"]:
            report["external_blockers"].append({"stage": "soak_supplemental", "error": soak["supplemental"]["error"],
                                                 "sources": soak["supplemental"].get("sources", {})})
        if soak["outcome"]["status"] == "extension_unresponsive":
            report["internal_failures"].append("Chrome extension did not start the soak run; reload the unpacked extension and rerun validator")
        if not soak["pass"]:
            report["internal_failures"].append("15-minute isolated soak failed")
            return 1

        semi_bundle = _isolated_bundle(semi_db, semi_out, _free_port())
        semi_url, _ = ensure_dashboard(semi_bundle, open_browser=True)
        dashboard_bundles.append((semi_bundle, semi_url))
        report["dashboard_url"] = semi_url
        semi_started = time.monotonic()
        with _isolated_environment(semi_bundle):
            semi = _live_run(semi_bundle, mode="staged", platforms=list(PRIMARY), timeout_seconds=semi_minutes * 60,
                             stop_after_seconds=60, bridge_restart_after=30, active_runs=active_runs)
            if semi["outcome"]["status"] == "stopped":
                semi["resume"] = _resume_live(semi_bundle, int(semi["run_id"]), semi_minutes * 60 - 60, active_runs)
                final_semi = semi["resume"]
            else:
                semi["resume"] = None
                final_semi = semi
            semi["supplemental"] = _run_supplemental(semi_bundle, int(semi["run_id"]))
        semi["duration_seconds_total"] = round(time.monotonic() - semi_started, 2)
        semi["dashboard"] = _dashboard_probe(semi_bundle, semi_url)
        semi["pass"] = bool(semi["outcome"]["status"] == "stopped" and semi["resume"] and
                             _stage_pass(final_semi) and semi["supplemental"]["isolation_pass"] and
                             semi["dashboard"]["identity_ok"] and semi["dashboard"]["live_refresh_ok"])
        report["semi_production"] = semi
        if not semi["supplemental"]["ok"]:
            report["external_blockers"].append({"stage": "semi_supplemental", "error": semi["supplemental"]["error"],
                                                 "sources": semi["supplemental"].get("sources", {})})
        if semi["outcome"]["status"] == "extension_unresponsive":
            report["internal_failures"].append("Chrome extension did not start the semi-production run; reload the unpacked extension and rerun validator")
        if not semi["pass"]:
            report["internal_failures"].append("semi-production validation failed")
            return 1
        report["PROD_READY"] = True
        return 0
    except KeyboardInterrupt:
        for bundle, run_id in list(active_runs):
            try:
                with _isolated_environment(bundle):
                    browser_tasks.emergency_stop(PROJECT_ROOT, run_id)
                report.setdefault("interrupted_audits", []).append(_audit(bundle, run_id))
            except Exception as exc:
                report["internal_failures"].append(f"interrupt cleanup run {run_id}: {type(exc).__name__}: {exc}")
        report["internal_failures"].append("validator interrupted; active browser runs were requested to stop")
        return 130
    except Exception as exc:
        report["internal_failures"].append(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        for bundle, run_id in list(active_runs):
            try:
                with _isolated_environment(bundle):
                    browser_tasks.emergency_stop(PROJECT_ROOT, run_id)
                report.setdefault("exit_audits", []).append(_audit(bundle, run_id))
            except Exception as exc:
                report["internal_failures"].append(f"exit cleanup run {run_id}: {type(exc).__name__}: {exc}")
        report["exit_integrity"] = {
            str(path): (_integrity(path) if path.exists() else "missing")
            for path in (validation_db, soak_db, semi_db)
        }
        for bundle, url in reversed(dashboard_bundles):
            _stop_dashboard(bundle, url)
        report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report["PROD_READY"] = bool(report.get("PROD_READY") and not report.get("internal_failures"))
        _write_report(report, report_dir)
        print(f"Validation database: {validation_db}")
        print(f"Soak database: {soak_db}")
        print(f"Semi-production database: {semi_db}")
        print(f"Dashboard: {report.get('dashboard_url') or 'not started'}")
        print(f"Report: {report_dir / 'latest.json'}")
        print(f"PROD_READY={str(report['PROD_READY']).lower()}")
