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
from .candidate import readiness_warnings
from .config import PROJECT_ROOT, ConfigBundle, load_bundle
from .db import Database
from .doctor import run as run_doctor
from .orchestrator import chrome_path, launch_browser_run
from .run_now import ensure_dashboard, preflight
from .search_plan import compile_plan, compile_staged_plan
from .search_strategy import BANDS, staged_cadence_economics
from .version import PRODUCT_VERSION
from .extension_identity import expected_identity
from .provenance import identity_unchanged, release_identity, source_identity, _git, ProvenanceError


PRIMARY = ("linkedin", "indeed", "glassdoor")
TERMINAL_SUCCESS = {"COMPLETED_FULL", "COMPLETED_PARTIAL_EXTERNAL"}
TERMINAL_EXTERNAL = {"challenged", "auth_required", "deferred_by_platform"}
VALIDATION_WINDOW_COMPLETE = "VALIDATION_WINDOW_COMPLETE"
VALIDATION_STOP_GRACE_SECONDS = 60
VALIDATION_CLEANUP_RESERVE_SECONDS = 12
PRIMARY_RESUME_TIMEOUT_SECONDS = 240
PRIMARY_RESUME_STOP_AFTER_SECONDS = PRIMARY_RESUME_TIMEOUT_SECONDS - VALIDATION_STOP_GRACE_SECONDS


def _bounded_timeout(requested: int | float, deadline: float | None = None) -> int:
    """Return a timeout that leaves cleanup room, or refuse a late start."""
    requested_seconds = max(1, int(requested))
    if deadline is None:
        return requested_seconds
    remaining = float(deadline) - time.monotonic()
    available = int(remaining - VALIDATION_CLEANUP_RESERVE_SECONDS)
    if available < 1:
        raise RuntimeError("validation stage deadline expired; refusing to start new browser work")
    return min(requested_seconds, available)


def extension_build(root: Path = PROJECT_ROOT) -> str:
    manifest = json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    value = str(manifest.get("version_name") or "").strip()
    if not value:
        raise RuntimeError("extension manifest has no version_name build identity")
    return value


def extension_runtime_identity(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    value = expected_identity(root)
    metadata_path = root / "extension" / "build_meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    value["metadata"] = metadata
    value["metadata_matches"] = (
        metadata.get("extension_build") == value["extension_build"]
        and metadata.get("extension_version") == value["extension_version"]
        and metadata.get("runtime_digest") == value["runtime_digest"]
    )
    return value


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


def _prepare_validation_bundle(bundle: ConfigBundle) -> None:
    """Migrate an isolated stage before dashboard identity or task creation."""
    with _isolated_environment(bundle):
        preflight(bundle)


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


def _git_state() -> dict[str, Any]:
    state = source_identity(PROJECT_ROOT, require_upstream=True)
    state["origin_main_head"] = _git(PROJECT_ROOT, "rev-parse", "origin/main")
    state["tracked_dirty"] = any(not line.startswith("?? ") for line in state["status_lines"])
    state["tracked_status"] = "\n".join(line for line in state["status_lines"] if not line.startswith("?? "))
    return state


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
                       and identity.get("jobbot_version") == PRODUCT_VERSION)
    workspace = (active or {}).get("workspace", {}) if isinstance(active, dict) else {}
    window_id = workspace.get("workspace_window_id") or workspace.get("window_id")
    role_windows = workspace.get("role_tab_window_ids") or {}
    worker_windows = workspace.get("worker_tab_window_ids") or {}
    role_windows_ok = all(str(value) == str(window_id) for value in role_windows.values() if value is not None)
    worker_windows_ok = all(str(value) == str(window_id) for value in worker_windows.values() if value is not None)
    workspace_ok = bool(
        active and workspace.get("isolated") is True
        and int(workspace.get("ownership_violations", 0) or 0) == 0
        and role_windows_ok
        and worker_windows_ok
        and workspace.get("workspace_creation_method") != "unsafe_rendezvous_adoption"
        and int(workspace.get("focus_requests_by_jobbot", 0) or 0) == 0
        and int(workspace.get("workspace_recreation_count", 0) or 0) == 0
    )
    return {
        "identity": identity,
        "identity_ok": identity_ok,
        "summary": summary,
        "run": active,
        "live_refresh_ok": isinstance(summary, dict) and isinstance(active, dict),
        "workspace_isolation_ok": workspace_ok,
        "workspace_proof": {
            "window_id": window_id,
            "role_tab_window_ids": role_windows,
            "worker_tab_window_ids": worker_windows,
            "ownership_violations": int(workspace.get("ownership_violations", 0) or 0),
            "non_jobbot_tab_count": int(workspace.get("non_jobbot_tab_count", 0) or 0),
            "workspace_creation_method": workspace.get("workspace_creation_method", ""),
            "workspace_recreation_count": int(workspace.get("workspace_recreation_count", 0) or 0),
            "focus_requests_by_jobbot": int(workspace.get("focus_requests_by_jobbot", 0) or 0),
        },
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


def _performance_summary(conn, run_id: int | None) -> dict[str, Any]:
    """Summarize bounded worker timings without storing page content."""
    if run_id is None:
        return {"samples": 0, "operations": {}}
    rows = conn.execute(
        "SELECT payload_json FROM browser_events WHERE browser_run_id=? AND event_type='performance'",
        (run_id,),
    ).fetchall()
    groups: dict[tuple[str, str], list[float]] = {}
    throughput: dict[str, dict[str, float]] = {}
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
            operation = str(payload.get("operation") or "unknown")
            platform = str(payload.get("platform") or "all")
            value = float(payload.get("duration_ms"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if 0 <= value <= 900000:
            groups.setdefault((platform, operation), []).append(value)
            stats = throughput.setdefault(platform, {
                "card_count": 0.0, "card_duration_ms": 0.0,
                "detail_count": 0.0, "detail_duration_ms": 0.0,
            })
            if operation == "card_persist":
                stats["card_count"] += float(payload.get("cards", 0) or 0)
                stats["card_duration_ms"] += value
            elif operation == "detail_record":
                stats["detail_count"] += 1
                stats["detail_duration_ms"] += value

    def percentile(values: list[float], fraction: float) -> float:
        ordered = sorted(values)
        if len(ordered) == 1:
            return round(ordered[0], 2)
        position = (len(ordered) - 1) * fraction
        lower, upper = int(position), min(len(ordered) - 1, int(position) + 1)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)

    operations = {
        f"{platform}:{operation}": {
            "samples": len(values),
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
        }
        for (platform, operation), values in sorted(groups.items())
    }
    rates = {}
    for platform, stats in sorted(throughput.items()):
        card_minutes = stats["card_duration_ms"] / 60000
        detail_minutes = stats["detail_duration_ms"] / 60000
        rates[platform] = {
            "cards_per_minute": round(stats["card_count"] / card_minutes, 2) if card_minutes else 0.0,
            "canonical_details_per_minute": round(stats["detail_count"] / detail_minutes, 2) if detail_minutes else 0.0,
        }
    return {"samples": sum(item["samples"] for item in operations.values()), "operations": operations, "throughput": rates}


def _audit(bundle: ConfigBundle, run_id: int | None = None) -> dict[str, Any]:
    conn = Database(bundle).connect()
    try:
        audit = collect_audit(conn, run_id=run_id, strategy=bundle.strategy)
        audit["performance"] = _performance_summary(conn, run_id)
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

    result = {platform: {"candidate_links_total": 0, "candidate_links_in_scope": 0,
                         "candidate_links_outside_scope": 0, "persisted_in_scope": 0,
                         "persisted_outside_scope": 0, "contamination_persisted": 0,
                         "scope_found_events": 0, "scope_missing_events": 0}
              for platform in PRIMARY}
    conn = sqlite3.connect(bundle.database_path)
    conn.row_factory = sqlite3.Row
    try:
        identities: dict[str, dict[str, set[str]]] = {
            platform: {"in_ids": set(), "in_urls": set(), "out_ids": set(), "out_urls": set()}
            for platform in PRIMARY
        }
        for row in conn.execute(
            "SELECT platform,payload_json FROM browser_events e JOIN browser_search_tasks t ON t.task_id=e.task_id "
            "WHERE e.browser_run_id=? AND e.event_type='scope_diagnostics'", (run_id,),
        ):
            platform = str(row["platform"])
            if platform not in result:
                continue
            try:
                envelope = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                envelope = {}
            payload = envelope.get("payload", envelope)
            result[platform]["candidate_links_total"] += int(payload.get("candidate_links_total", 0) or 0)
            result[platform]["candidate_links_in_scope"] += int(payload.get("candidate_links_in_scope", 0) or 0)
            result[platform]["candidate_links_outside_scope"] += int(payload.get("candidate_links_outside_scope", 0) or 0)
            identities[platform]["in_ids"].update(str(value) for value in payload.get("in_scope_source_ids", []) if value)
            identities[platform]["in_urls"].update(_normalise_url(value) for value in payload.get("in_scope_urls", []) if value)
            identities[platform]["out_ids"].update(str(value) for value in payload.get("outside_scope_source_ids", []) if value)
            identities[platform]["out_urls"].update(_normalise_url(value) for value in payload.get("outside_scope_urls", []) if value)
        for row in conn.execute(
            "SELECT platform,COUNT(*) FROM browser_events e JOIN browser_search_tasks t ON t.task_id=e.task_id "
            "WHERE e.browser_run_id=? AND e.event_type='extraction_scope_missing' GROUP BY platform", (run_id,),
        ):
            if str(row[0]) in result:
                result[str(row[0])]["scope_missing_events"] = int(row[1])
        for platform in PRIMARY:
            result[platform]["attempted_pages"] = int(conn.execute(
                "SELECT COALESCE(SUM(pages_visited),0) FROM browser_search_tasks WHERE browser_run_id=? AND platform=?",
                (run_id, platform),
            ).fetchone()[0] or 0)
            result[platform]["scope_found_events"] = int(conn.execute(
                "SELECT COUNT(*) FROM browser_events e JOIN browser_search_tasks t ON t.task_id=e.task_id "
                "WHERE e.browser_run_id=? AND e.event_type='scope_diagnostics' AND t.platform=?",
                (run_id, platform),
            ).fetchone()[0] or 0)
            persisted = conn.execute(
                "SELECT source_job_id,source_url FROM search_task_results r "
                "JOIN browser_search_tasks t ON t.task_id=r.task_id "
                "WHERE r.browser_run_id=? AND t.platform=?",
                (run_id, platform),
            ).fetchall()
            outside_only_ids = identities[platform]["out_ids"] - identities[platform]["in_ids"]
            outside_only_urls = identities[platform]["out_urls"] - identities[platform]["in_urls"]
            result[platform]["outside_only_ids"] = sorted(outside_only_ids)
            result[platform]["outside_only_urls"] = sorted(outside_only_urls)
            outside_count = 0
            for persisted_row in persisted:
                sid = str(persisted_row[0] or "")
                url = _normalise_url(persisted_row[1])
                if sid in outside_only_ids or (url and url in outside_only_urls):
                    outside_count += 1
            result[platform]["persisted_outside_scope"] = outside_count
            result[platform]["contamination_persisted"] = outside_count
            result[platform]["persisted_in_scope"] = max(0, len(persisted) - outside_count)
    finally:
        conn.close()
    return result


def _normalise_url(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _record_validation_cutoff(bundle: ConfigBundle, run_id: int, reason: str) -> None:
    conn = Database(bundle).connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM browser_events WHERE browser_run_id=? AND event_type='validation_cutoff' LIMIT 1",
            (run_id,),
        ).fetchone()
        if not exists:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                (run_id, now, "validation_cutoff", "bounded validation window intentionally closed",
                 json.dumps({"classification": VALIDATION_WINDOW_COMPLETE, "reason": reason})),
            )
            conn.commit()
    finally:
        conn.close()


def _validation_metrics(bundle: ConfigBundle, run_id: int, audit: dict[str, Any]) -> dict[str, Any]:
    """Reconcile attempted validation work while leaving untouched work queued."""
    import sqlite3

    conn = sqlite3.connect(bundle.database_path)
    conn.row_factory = sqlite3.Row
    try:
        attempted = conn.execute(
            "SELECT * FROM browser_search_tasks WHERE browser_run_id=? AND (started_at IS NOT NULL OR status <> 'queued')",
            (run_id,),
        ).fetchall()
        task_states = {state: 0 for state in (
            "queued", "running", "exhausted", "incomplete", "challenged", "auth_required",
            "deferred_by_platform", "failed", "paused", "stopped",
        )}
        extracted = persistence_attempted = persisted = persistence_failed = duplicates = progress_tasks = 0
        for task in attempted:
            state = str(task["status"] or "")
            if state in task_states:
                task_states[state] += 1
            extracted += int(task["cards_extracted"] or 0)
            persistence_attempted += int(task["cards_persistence_attempted"] or 0)
            persisted += int(task["cards_persistence_succeeded"] or 0)
            persistence_failed += int(task["cards_persistence_failed"] or 0)
            duplicates += int(task["duplicate_cards"] or 0)
            if int(task["pages_visited"] or 0) > 0 or int(task["cards_extracted"] or 0) > 0:
                progress_tasks += 1
        result_rows = conn.execute(
            "SELECT r.detail_status,r.canonical_job_id FROM search_task_results r "
            "JOIN browser_search_tasks t ON t.task_id=r.task_id "
            "WHERE r.browser_run_id=? AND (t.started_at IS NOT NULL OR t.status <> 'queued')",
            (run_id,),
        ).fetchall()
        detail_states = {"PENDING": 0, "RUNNING": 0, "COMPLETE": 0, "RETRYABLE": 0,
                         "FAILED": 0, "EXTERNAL_BLOCKED": 0}
        # Canonical identifiers are durable JobBot strings (for example
        # ``J943D57AD3EB03C``), not SQLite integer rowids.
        canonical_ids: set[str] = set()
        for row in result_rows:
            status = str(row["detail_status"] or "")
            if status in detail_states:
                detail_states[status] += 1
            if row["canonical_job_id"]:
                canonical_ids.add(str(row["canonical_job_id"]))
        untouched_queued = int(conn.execute(
            "SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND started_at IS NULL AND status='queued'",
            (run_id,),
        ).fetchone()[0] or 0)
        untouched_exhausted = int(conn.execute(
            "SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND started_at IS NULL AND status='exhausted'",
            (run_id,),
        ).fetchone()[0] or 0)
        attempted_false_exhausted = int(conn.execute(
            "SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND started_at IS NOT NULL "
            "AND status='exhausted' AND pages_visited=0 AND results_seen=0",
            (run_id,),
        ).fetchone()[0] or 0)
        audit_metrics = _metrics(audit)
        task_failures = sum(task_states.get(state, 0) for state in ("failed", "incomplete", "running", "paused"))
        reconciliation_ok = extracted == persistence_attempted == persisted + persistence_failed and persistence_failed == 0
        audit_metrics.update({
            "attempted_tasks": len(attempted),
            "progress_tasks": progress_tasks,
            "untouched_queued": untouched_queued,
            "untouched_exhausted": untouched_exhausted,
            "attempted_false_exhausted": attempted_false_exhausted,
            "attempted_failed": task_states["failed"],
            "attempted_incomplete": task_states["incomplete"],
            "attempted_running": task_states["running"],
            "attempted_stopped": task_states["stopped"],
            "extracted": extracted,
            "persistence_attempted": persistence_attempted,
            "persisted": persisted,
            "persistence_failed": persistence_failed,
            "duplicates": duplicates,
            "detail_pending": detail_states["PENDING"],
            "detail_running": detail_states["RUNNING"],
            "detail_complete": detail_states["COMPLETE"],
            "detail_retryable": detail_states["RETRYABLE"],
            "detail_failed": detail_states["FAILED"],
            "detail_external_blocked": detail_states["EXTERNAL_BLOCKED"],
            "canonical_jobs_attempted": len(canonical_ids),
            "task_states": task_states,
            "reconciliation": {"ok": reconciliation_ok, "attempted_tasks": len(attempted), "task_failures": task_failures},
            "unexplained": int(audit_metrics.get("unexplained", 0) or 0) + task_failures,
        })
        return audit_metrics
    finally:
        conn.close()


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
        expected = extension_runtime_identity(bundle.root)
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        event_payload = payload.get("payload", payload) if isinstance(payload, dict) else {}
        if event_payload.get("build") == expected["extension_build"] and event_payload.get("runtime_digest") == expected["runtime_digest"]:
            return True
    return False


def _classify_supplemental(
    source_status: dict[str, Any],
    *,
    code: int,
    uncaught_error: str = "",
    records_by_source: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Classify source failures without hiding orchestration/programming errors."""
    records_by_source = records_by_source or {}
    sources: dict[str, Any] = {}
    internal_failed = bool(uncaught_error)
    for name, info in source_status.items():
        info = info if isinstance(info, dict) else {}
        attempted = True
        if bool(info.get("ok")) and info.get("complete", True) is not False:
            state = "completed"
        elif bool(info.get("ok")) and info.get("state") == "CAPPED_EXTERNAL_BOUNDARY":
            state = "CAPPED_EXTERNAL_BOUNDARY"
        elif bool(info.get("internal")) or str(info.get("state") or "").startswith("INTERNAL_"):
            state = "internal_failed"
        else:
            # legacy_engine catches individual HTTP/board failures and records
            # them here.  Those are isolated external failures, not pipeline
            # failures.  An uncaught exception is handled separately below.
            state = "external_failed"
        sources[str(name)] = {
            "attempted": attempted,
            "state": state,
            "completed": state == "completed",
            "external_failed": state in {"external_failed", "CAPPED_EXTERNAL_BOUNDARY"},
            "internal_failed": state == "internal_failed",
            "records_persisted": int(records_by_source.get(str(name), 0) or 0),
            "error": str(info.get("error") or ""),
        }
    if code != 0 and not sources:
        internal_failed = True
    if any(value["internal_failed"] for value in sources.values()):
        internal_failed = True
    if code != 0 and not any(value["external_failed"] for value in sources.values()) and not internal_failed:
        internal_failed = True
    if internal_failed:
        for value in sources.values():
            if value["state"] == "completed":
                continue
            value["state"] = "internal_failed"
            value["external_failed"] = False
            value["internal_failed"] = True
            if uncaught_error:
                value["error"] = uncaught_error
    return {
        "isolation_pass": not internal_failed,
        "internal_failed": internal_failed,
        "internal_error": uncaught_error,
        "sources": sources,
    }


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
    uncaught_error = ""
    try:
        code = int(legacy_engine.run_search(bundle.legacy_runtime(), bundle.strategy, "deep"))
    except Exception as exc:  # an isolated source failure must be visible, not fatal to primary state
        code = 1
        uncaught_error = f"{type(exc).__name__}: {exc}"
    error = uncaught_error or ("" if code == 0 else f"supplemental stage returned exit code {code}")
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
        source_status: dict[str, Any] = {}
        run_row = conn.execute("SELECT source_status_json FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if run_row:
            try:
                decoded = json.loads(run_row[0] or "{}")
                if isinstance(decoded, dict):
                    source_status = decoded
            except json.JSONDecodeError:
                source_status = {}
        for name, config in configured_sources.items():
            count = conn.execute(
                "SELECT COUNT(*) FROM source_occurrences WHERE source_site=?", (str(name),)
            ).fetchone()[0]
            if bool(config.get("enabled", True)) and str(name) not in source_status:
                source_status[str(name)] = {"ok": False, "error": "source status missing", "complete": False}
        if "ats_watch" in source_status:
            count = conn.execute("SELECT COUNT(*) FROM source_occurrences WHERE source_site IN ('ats_watch','ats')").fetchone()[0]
            source_status["ats_discovery"] = source_status["ats_watch"]
        records = {name: int(conn.execute(
            "SELECT COUNT(*) FROM source_occurrences WHERE source_site=?", (name,)
        ).fetchone()[0] or 0) for name in source_status}
    finally:
        conn.close()
    classified = _classify_supplemental(source_status, code=code, uncaught_error=uncaught_error, records_by_source=records)
    sources = classified["sources"]
    for name, config in configured_sources.items():
        sources.setdefault(str(name), {"attempted": False, "state": "disabled", "completed": False,
                                       "external_failed": False, "internal_failed": False,
                                       "records_persisted": 0, "error": ""})
        sources[str(name)]["enabled"] = bool(config.get("enabled", True))
    ats = bundle.runtime.get("ats_discovery", {})
    sources.setdefault("ats_discovery", {"attempted": False, "state": "disabled", "completed": False,
                                          "external_failed": False, "internal_failed": False,
                                          "records_persisted": 0, "error": ""})["enabled"] = bool(ats.get("enabled", False))
    return {"ok": classified["isolation_pass"], "isolation_pass": classified["isolation_pass"],
            "internal_failed": classified["internal_failed"], "exit_code": code, "error": error,
            "duration_seconds": round(time.monotonic() - started, 2), "sources": sources,
            "external_failures": [name for name, value in sources.items() if value.get("external_failed")]}


def _live_run(bundle: ConfigBundle, *, mode: str, platforms: list[str], timeout_seconds: int,
              stop_after_seconds: int | None = None, bridge_restart_after: int | None = None,
              validation_micro: bool = False, validation_sample: bool = False,
              sample_phases: tuple[str, ...] | None = None, sample_per_phase: int = 6,
              sample_bands: tuple[str, ...] | None = None,
              active_runs: list[tuple[ConfigBundle, int]] | None = None,
              deadline: float | None = None) -> dict[str, Any]:
    with _isolated_environment(bundle):
        _bounded_timeout(timeout_seconds, deadline)
        if validation_micro:
            run_id = browser_tasks.enqueue_validation(PROJECT_ROOT, platforms)
        elif validation_sample:
            run_id = browser_tasks.enqueue_validation_sample(
                PROJECT_ROOT, platforms, phases=sample_phases or (
                    "A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT", "C_DEEP_BACKFILL",
                ), per_phase_per_platform=sample_per_phase, sample_bands=sample_bands, bundle=bundle,
            )
        else:
            run_id = browser_tasks.enqueue_production(PROJECT_ROOT, mode, platforms)
        if active_runs is not None:
            active_runs.append((bundle, run_id))
        started = time.monotonic()
        try:
            effective_timeout = _bounded_timeout(timeout_seconds, deadline)
        except RuntimeError:
            browser_tasks.emergency_stop(PROJECT_ROOT, run_id)
            raise
        if deadline is not None:
            if stop_after_seconds is not None:
                stop_after_seconds = max(1, min(int(stop_after_seconds), max(1, effective_timeout - VALIDATION_STOP_GRACE_SECONDS)))
        outcome = launch_browser_run(
            bundle,
            run_id,
            wait=True,
            open_browser=True,
            timeout_seconds=effective_timeout,
            stop_after_seconds=stop_after_seconds,
            test_bridge_restart_after=bridge_restart_after,
            startup_timeout_seconds=45,
            dashboard_url=f"http://127.0.0.1:{int(bundle.runtime['runtime']['dashboard_port'])}/",
        )
    audit = _audit(bundle, run_id)
    validation_cutoff = False
    if outcome.status == "stopped" and stop_after_seconds is not None:
        _record_validation_cutoff(bundle, run_id, f"intentional {mode} validation window cutoff")
        validation_cutoff = True
    validation_metrics = _validation_metrics(bundle, run_id, audit)
    result = {
        "run_id": run_id,
        "outcome": outcome.__dict__,
        "duration_seconds": round(time.monotonic() - started, 2),
        "deadline_remaining_seconds": None if deadline is None else round(max(0.0, deadline - time.monotonic()), 2),
        "terminal_classification": audit.get("terminal_classification"),
        "metrics": _metrics(audit),
        "audit": audit,
        "validation_metrics": validation_metrics,
        "validation_cutoff": validation_cutoff,
        "validation_classification": VALIDATION_WINDOW_COMPLETE if validation_cutoff else audit.get("terminal_classification"),
        "scope": _scope_diagnostics(bundle, run_id),
        "extension_build_pass": _extension_build_seen(bundle, run_id),
        "bridge_restart_pass": int(outcome.bridge_restarts) >= 1 if bridge_restart_after is not None else None,
    }
    if active_runs is not None:
        active_runs[:] = [(active_bundle, active_id) for active_bundle, active_id in active_runs if active_id != run_id]
    return result


def _resume_live(bundle: ConfigBundle, run_id: int, timeout_seconds: int,
                 stop_after_seconds: int | None = None,
                 active_runs: list[tuple[ConfigBundle, int]] | None = None,
                 deadline: float | None = None) -> dict[str, Any]:
    _bounded_timeout(timeout_seconds, deadline)
    if active_runs is not None:
        active_runs.append((bundle, run_id))
    with _isolated_environment(bundle):
        browser_tasks.resume_run(PROJECT_ROOT, run_id)
        started = time.monotonic()
        try:
            effective_timeout = _bounded_timeout(timeout_seconds, deadline)
        except RuntimeError:
            browser_tasks.emergency_stop(PROJECT_ROOT, run_id)
            raise
        if deadline is not None:
            if stop_after_seconds is not None:
                stop_after_seconds = max(1, min(int(stop_after_seconds), max(1, effective_timeout - VALIDATION_STOP_GRACE_SECONDS)))
        outcome = launch_browser_run(bundle, run_id, wait=True, open_browser=True, timeout_seconds=effective_timeout,
                                     stop_after_seconds=stop_after_seconds, startup_timeout_seconds=45,
                                     dashboard_url=f"http://127.0.0.1:{int(bundle.runtime['runtime']['dashboard_port'])}/")
    audit = _audit(bundle, run_id)
    validation_cutoff = False
    if outcome.status == "stopped" and stop_after_seconds is not None:
        _record_validation_cutoff(bundle, run_id, "intentional resumed validation window cutoff")
        validation_cutoff = True
    validation_metrics = _validation_metrics(bundle, run_id, audit)
    result = {
        "run_id": run_id,
        "outcome": outcome.__dict__,
        "duration_seconds": round(time.monotonic() - started, 2),
        "deadline_remaining_seconds": None if deadline is None else round(max(0.0, deadline - time.monotonic()), 2),
        "terminal_classification": audit.get("terminal_classification"),
        "metrics": _metrics(audit),
        "audit": audit,
        "validation_metrics": validation_metrics,
        "validation_cutoff": validation_cutoff,
        "validation_classification": VALIDATION_WINDOW_COMPLETE if validation_cutoff else audit.get("terminal_classification"),
        "scope": _scope_diagnostics(bundle, run_id),
        "extension_build_pass": _extension_build_seen(bundle, run_id),
    }
    if active_runs is not None:
        active_runs[:] = [(active_bundle, active_id) for active_bundle, active_id in active_runs if active_id != run_id]
    return result


def _stage_pass(stage: dict[str, Any], *, bounded: bool = False, require_terminal: bool = True) -> bool:
    metrics = stage.get("validation_metrics", stage.get("metrics", {})) if bounded else stage.get("metrics", {})
    classification = stage.get("validation_classification", stage.get("terminal_classification")) if bounded else stage.get("terminal_classification")
    if bounded:
        natural_terminal = classification in TERMINAL_SUCCESS
        intentional_cutoff = classification == VALIDATION_WINDOW_COMPLETE and stage.get("validation_cutoff") is True
        if not (natural_terminal or intentional_cutoff):
            return False
    elif require_terminal and classification not in TERMINAL_SUCCESS:
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
    if bounded:
        if metrics.get("untouched_exhausted", 0) or metrics.get("attempted_false_exhausted", 0) or metrics.get("attempted_incomplete", 0) or metrics.get("attempted_failed", 0) or metrics.get("attempted_running", 0):
            return False
        if not metrics.get("attempted_tasks", 0) or not metrics.get("progress_tasks", 0):
            return False
    elif any(states.get(state, 0) for state in ("queued", "running", "incomplete", "paused", "stopped", "failed")):
        return False
    if any(values.get("contamination_persisted", values.get("contamination", 0)) or values.get("scope_missing_events", 0)
           or (values.get("attempted_pages", 0) and not values.get("scope_found_events", 0))
           for values in stage.get("scope", {}).values()):
        return False
    return True


def _phase_coverage_pass(phase_metrics: dict[str, dict[str, dict[str, int | bool]]]) -> bool:
    """Require real progress in every phase, allowing only fully blocked platforms to be exempt."""
    any_progress = False
    for phase in ("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT", "C_DEEP_BACKFILL"):
        platforms = phase_metrics.get(phase, {})
        if not platforms:
            return False
        phase_progress = sum(int(values.get("progress_tasks", 0) or 0) for values in platforms.values())
        any_progress = any_progress or phase_progress > 0
        if phase_progress > 0:
            continue
        if not all(bool(values.get("all_external_blocked")) for values in platforms.values()):
            return False
    return any_progress


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
    ok = bool(state["branch"]) and bool(state["clean_worktree"]) and bool(state.get("head_equals_upstream"))
    checks.append({"name": "commit provenance", "ok": ok, "detail": state})
    executable = chrome_path()
    chrome_ok = bool(executable)
    checks.append({"name": "normal Chrome", "ok": chrome_ok, "detail": executable or "not found"})
    required = [bundle.root / "extension" / name for name in ("manifest.json", "service_worker.js", "dashboard.html")]
    extension_ok = all(path.is_file() for path in required)
    checks.append({"name": "current unpacked extension", "ok": extension_ok, "detail": [str(x) for x in required if not x.is_file()]})
    try:
        build = extension_build(bundle.root)
        checks.append({"name": "manifest extension build", "ok": bool(build), "detail": build})
        identity = extension_runtime_identity(bundle.root)
        checks.append({"name": "extension runtime identity", "ok": bool(identity["metadata_matches"]), "detail": identity})
    except Exception as exc:
        checks.append({"name": "manifest extension build", "ok": False, "detail": str(exc)})
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
    candidate_warnings = readiness_warnings(bundle)
    checks.append({"name": "candidate readiness warnings", "ok": True, "detail": candidate_warnings})
    deterministic = _fixture_and_deterministic(bundle)
    checks.append({"name": "deterministic suite", "ok": deterministic["local_tests"]["returncode"] == 0, "detail": deterministic["local_tests"]})
    return {
        "ok": all(item["ok"] for item in checks),
        "git": state,
        "checks": checks,
        "deterministic": deterministic,
        "candidate_readiness_warnings": candidate_warnings,
    }


def _phase_counts(tasks: list[Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for task in tasks:
        phase = str(task.phase)
        platform = str(task.platform)
        counts.setdefault(phase, {})[platform] = counts.setdefault(phase, {}).get(platform, 0) + 1
    return counts


def _sampled_phase_counts(bundle: ConfigBundle, run_id: int) -> dict[str, dict[str, int]]:
    conn = Database(bundle).connect()
    try:
        counts: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT phase,platform,COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? GROUP BY phase,platform ORDER BY phase,platform",
            (run_id,),
        ):
            counts.setdefault(str(row[0]), {})[str(row[1])] = int(row[2])
        return counts
    finally:
        conn.close()


def _phase_execution_metrics(bundle: ConfigBundle, run_id: int) -> dict[str, dict[str, dict[str, int | bool]]]:
    """Return queued, started, progressed, persisted, and detail-complete proof by phase/platform."""
    conn = Database(bundle).connect()
    try:
        phases: dict[str, dict[str, dict[str, int | bool]]] = {}
        task_rows = conn.execute(
            "SELECT phase,platform,search_band,status,started_at,pages_visited,cards_extracted,cards_persistence_succeeded "
            "FROM browser_search_tasks WHERE browser_run_id=? ORDER BY phase,platform,task_id",
            (run_id,),
        ).fetchall()
        for row in task_rows:
            phase, platform, band = str(row[0]), str(row[1]), str(row[2] or "DEEP_TAIL")
            values = phases.setdefault(phase, {}).setdefault(platform, {
                "sampled_queued": 0, "started_tasks": 0, "progress_tasks": 0,
                "cards_persisted": 0, "details_complete": 0, "external_tasks": 0, "bands": {},
            })
            band_values = values["bands"].setdefault(band, {
                "sampled_queued": 0,
                "started_tasks": 0,
                "progress_tasks": 0,
                "cards_persisted": 0,
                "details_complete": 0,
                "external_tasks": 0,
            })
            values["sampled_queued"] = int(values["sampled_queued"]) + 1
            band_values["sampled_queued"] += 1
            started = bool(row[4])
            if started:
                values["started_tasks"] = int(values["started_tasks"]) + 1
                band_values["started_tasks"] += 1
            progressed = int(row[5] or 0) > 0 or int(row[6] or 0) > 0
            if progressed:
                values["progress_tasks"] = int(values["progress_tasks"]) + 1
                band_values["progress_tasks"] += 1
            values["cards_persisted"] = int(values["cards_persisted"]) + int(row[7] or 0)
            band_values["cards_persisted"] += int(row[7] or 0)
            if str(row[3]) in TERMINAL_EXTERNAL:
                values["external_tasks"] = int(values["external_tasks"]) + 1
                band_values["external_tasks"] += 1
        for phase, platforms in phases.items():
            for platform, values in platforms.items():
                values["details_complete"] = int(conn.execute(
                    "SELECT COUNT(*) FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id "
                    "WHERE r.browser_run_id=? AND t.phase=? AND t.platform=? AND r.detail_status='COMPLETE'",
                    (run_id, phase, platform),
                ).fetchone()[0] or 0)
                for band in values["bands"]:
                    values["bands"][band]["details_complete"] = int(conn.execute(
                        "SELECT COUNT(*) FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id "
                        "WHERE r.browser_run_id=? AND t.phase=? AND t.platform=? AND t.search_band=? AND r.detail_status='COMPLETE'",
                        (run_id, phase, platform, band),
                    ).fetchone()[0] or 0)
                values["all_external_blocked"] = bool(
                    int(values["sampled_queued"]) > 0
                    and int(values["external_tasks"]) == int(values["sampled_queued"])
                )
        return phases
    finally:
        conn.close()


def _band_execution_metrics(execution: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, int]]:
    result = {band: {
        "sampled_queued": 0,
        "started_tasks": 0,
        "progress_tasks": 0,
        "cards_persisted": 0,
        "details_complete": 0,
        "external_tasks": 0,
    } for band in BANDS}
    for platforms in execution.values():
        for values in platforms.values():
            for band, metrics in values.get("bands", {}).items():
                target = result.setdefault(band, {key: 0 for key in result[BANDS[0]]})
                for key in target:
                    target[key] += int(metrics.get(key, 0) or 0)
    return result


def _band_coverage_pass(execution: dict[str, dict[str, dict[str, Any]]], *, required_bands: tuple[str, ...] = BANDS) -> bool:
    evidence = _band_execution_metrics(execution)
    for band in required_bands:
        values = evidence[band]
        if int(values["progress_tasks"]) > 0:
            continue
        # A band blocked on every sampled task by a platform challenge/auth
        # state is externally exempt, but an unsampled or merely queued band
        # is not evidence of live strategy coverage.
        if (
            int(values["sampled_queued"]) > 0
            and int(values["external_tasks"]) == int(values["sampled_queued"])
        ):
            continue
        return False
    return True


def _write_report(report: dict[str, Any], report_dir: Path, *, prefix: str = "") -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}-latest" if prefix else "latest"
    latest_json = report_dir / f"{stem}.json"
    latest_md = report_dir / f"{stem}.md"
    latest_json.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    lines = ["# JobBot production validation", "", f"PROD_READY={str(bool(report.get('PROD_READY'))).lower()}",
             f"branch={report.get('branch')}", f"head={report.get('head')}", ""]
    for key in ("preflight", "run_now_coverage", "primary_live", "soak", "semi_production", "external_blockers", "internal_failures"):
        lines.extend([f"## {key}", "", "```json", json.dumps(report.get(key), indent=2, ensure_ascii=False, default=str), "```", ""])
    latest_md.write_text("\n".join(lines), encoding="utf-8")


def _run_semi_phase(
    bundle: ConfigBundle,
    *,
    phase: str,
    platforms: list[str],
    phase_seconds: int,
    active_runs: list[tuple[ConfigBundle, int]],
    stage_deadline: float | None = None,
    sample_band: str | None = None,
) -> dict[str, Any]:
    """Run one bounded phase window through the normal browser/bridge path."""
    intentional_stop = phase == "A_FASTEST_DOOR_RECENT"
    first_window = min(60, max(15, phase_seconds // 6)) if intentional_stop else None
    # A bounded window must request an orderly stop before its hard timeout.
    # The grace period lets the extension finish the current atomic task and
    # clear its run promise; using the same value for both caused an emergency
    # stop at the deadline and poisoned the next phase's worker startup.
    initial_stop_after = first_window if intentional_stop else phase_seconds
    initial_timeout = initial_stop_after + VALIDATION_STOP_GRACE_SECONDS
    initial = _live_run(
        bundle,
        mode="staged",
        platforms=platforms,
        timeout_seconds=initial_timeout,
        stop_after_seconds=initial_stop_after,
        validation_sample=True,
        sample_phases=(phase,),
        sample_per_phase=1 if sample_band else 6,
        sample_bands=(sample_band,) if sample_band else None,
        active_runs=active_runs,
        deadline=stage_deadline,
    )
    resumed = None
    final = initial
    if intentional_stop and initial["outcome"]["status"] == "stopped":
        remaining = max(30, phase_seconds - int(first_window or 0))
        resumed = _resume_live(
            bundle,
            int(initial["run_id"]),
            remaining + VALIDATION_STOP_GRACE_SECONDS,
            remaining if intentional_stop else None,
            active_runs,
            deadline=stage_deadline,
        )
        final = resumed
    execution = _phase_execution_metrics(bundle, int(final["run_id"]))
    return {
        "phase": phase,
        "initial": initial,
        "resume": resumed,
        "final": final,
        "execution": execution,
        "pass": _stage_pass(final, bounded=True),
        "stop_resume_pass": bool(initial["outcome"]["status"] == "stopped" and resumed),
    }


def run(*, semi_minutes: int = 30, stage: str = "full") -> int:
    if stage not in {"micro", "full"}:
        raise ValueError("stage must be micro or full")
    micro_only = stage == "micro"
    if micro_only:
        # The micro path deliberately reuses the primary live proof below and
        # returns before creating or touching soak/semi-production work.
        semi_minutes = 30
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
        "validation_stage": stage,
        "extension_build": extension_build(PROJECT_ROOT),
        "extension_runtime_digest": extension_runtime_identity(PROJECT_ROOT)["runtime_digest"],
        "external_blockers": [],
        "internal_failures": [],
    }
    source_snapshot = release_identity(PROJECT_ROOT, require_upstream=True)
    report.update({
        "branch": source_snapshot["branch"], "head": source_snapshot["head"],
        "tree": source_snapshot["tree"], "upstream_ref": source_snapshot["upstream_ref"],
        "upstream_sha": source_snapshot["upstream_sha"],
        "head_equals_upstream": source_snapshot["head_equals_upstream"],
        "clean_worktree": source_snapshot["clean_worktree"],
        "source_identity_start": source_snapshot,
        "product_version": source_snapshot["product_version"],
    })
    configured_tasks = compile_staged_plan(load_bundle(PROJECT_ROOT), list(PRIMARY))
    phase_counts = _phase_counts(configured_tasks)
    configured_by_platform = {platform: {
        "phase_a_fast_recent": sum(1 for task in configured_tasks if task.platform == platform and task.phase == "A_FASTEST_DOOR_RECENT"),
        "phase_b_remaining_recent": sum(1 for task in configured_tasks if task.platform == platform and task.phase == "B_REMAINING_CORE_RECENT"),
        "recent_a_plus_b": sum(1 for task in configured_tasks if task.platform == platform and task.phase in {"A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT"}),
        "deep_c": sum(1 for task in configured_tasks if task.platform == platform and task.phase == "C_DEEP_BACKFILL"),
        "staged_total": sum(1 for task in configured_tasks if task.platform == platform),
    } for platform in PRIMARY}
    report["run_now_coverage"] = {
        "configured_production_phase_counts": phase_counts,
        "configured_production_labels": {
            "phase_a_fast_recent_per_platform": configured_by_platform[PRIMARY[0]]["phase_a_fast_recent"],
            "phase_b_remaining_recent_per_platform": configured_by_platform[PRIMARY[0]]["phase_b_remaining_recent"],
            "total_recent_a_plus_b_per_platform": configured_by_platform[PRIMARY[0]]["recent_a_plus_b"],
            "deep_c_per_platform": configured_by_platform[PRIMARY[0]]["deep_c"],
            "staged_total_per_platform": configured_by_platform[PRIMARY[0]]["staged_total"],
            "big3_staged_total": sum(value["staged_total"] for value in configured_by_platform.values()),
        },
        "configured_production_by_platform": configured_by_platform,
        "configured_production_band_counts": {
            phase: {band: sum(1 for task in configured_tasks if task.phase == phase and task.search_band == band) for band in BANDS}
            for phase in ("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT", "C_DEEP_BACKFILL")
        },
        "configured_cadence_economics": staged_cadence_economics(configured_tasks),
        "live_sampled_phase_counts": {},
    }
    active_runs: list[tuple[ConfigBundle, int]] = []
    dashboard_bundles: list[tuple[ConfigBundle, str]] = []
    micro_deadline = time.monotonic() + 300 if micro_only else None
    try:
        state = _git_state()
        report.update(state)
        micro_bundle = _isolated_bundle(validation_db, validation_out, _free_port())
        report["preflight"] = _preflight(micro_bundle)
        if not report["preflight"]["ok"]:
            report["internal_failures"].append("preflight failed; live stages were not started")
            return 1
        url, _ = ensure_dashboard(micro_bundle, open_browser=False)
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
                deadline=micro_deadline,
            )
        if primary["outcome"]["status"] == "stopped":
            primary["resume"] = _resume_live(
                micro_bundle,
                int(primary["run_id"]),
                PRIMARY_RESUME_TIMEOUT_SECONDS,
                PRIMARY_RESUME_STOP_AFTER_SECONDS,
                active_runs,
                deadline=micro_deadline,
            )
            final_primary = primary["resume"]
        else:
            primary["resume"] = None
            final_primary = primary
        primary["stop_resume_pass"] = bool(primary["outcome"]["status"] == "stopped" and primary["resume"] and _stage_pass(final_primary, bounded=True))
        primary["dashboard"] = _dashboard_probe(micro_bundle, url)
        primary["pass"] = bool(primary["stop_resume_pass"] and primary["bridge_restart_pass"] is True
                                and primary["dashboard"]["identity_ok"]
                                and primary["dashboard"]["live_refresh_ok"] and primary["dashboard"]["workspace_isolation_ok"]
                                and _stage_pass(final_primary, bounded=True))
        report["primary_live"] = primary
        if primary["outcome"]["status"] == "extension_unresponsive":
            report["internal_failures"].append(
                "Chrome extension did not start the validation run within 45 seconds; reload the unpacked extension in chrome://extensions and rerun validator"
            )
        if not primary.get("extension_build_pass"):
            report["internal_failures"].append(
            f"loaded extension did not report manifest build {extension_build(PROJECT_ROOT)}; reload the unpacked extension in chrome://extensions and rerun validator"
            )
        for platform, values in final_primary.get("scope", {}).items():
            if values.get("scope_missing_events") or values.get("contamination_persisted"):
                report["internal_failures"].append(f"{platform}: live result scope failed")
        for platform, values in final_primary.get("audit", {}).get("platforms", {}).items():
            if any(values.get(state, 0) for state in TERMINAL_EXTERNAL):
                report["external_blockers"].append({"stage": "primary_live", "platform": platform,
                                                     "states": {state: values.get(state, 0) for state in TERMINAL_EXTERNAL}})
        if not primary["pass"]:
            report["internal_failures"].append("primary live micro-validation failed")
            return 1
        if micro_only:
            report["validation_stage"] = "micro"
            report["micro_pass"] = True
            return 0

        soak_bundle = _isolated_bundle(soak_db, soak_out, _free_port())
        _prepare_validation_bundle(soak_bundle)
        soak_url, _ = ensure_dashboard(soak_bundle, open_browser=False)
        dashboard_bundles.append((soak_bundle, soak_url))
        report["dashboard_url"] = soak_url
        soak_started = time.monotonic()
        soak_deadline = soak_started + 900
        with _isolated_environment(soak_bundle):
            soak = _live_run(soak_bundle, mode="staged_recent", platforms=list(PRIMARY), timeout_seconds=900,
                             stop_after_seconds=60, bridge_restart_after=30, validation_sample=True,
                             sample_phases=("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT"), sample_per_phase=6,
                             active_runs=active_runs, deadline=soak_deadline)
            if soak["outcome"]["status"] == "stopped":
                soak["resume"] = _resume_live(soak_bundle, int(soak["run_id"]), 840, 780, active_runs, deadline=soak_deadline)
                final_soak = soak["resume"]
            else:
                soak["resume"] = None
                final_soak = soak
            soak["supplemental"] = _run_supplemental(soak_bundle, int(soak["run_id"]))
        soak["sampled_phase_counts"] = _sampled_phase_counts(soak_bundle, int(soak["run_id"]))
        report["run_now_coverage"]["live_sampled_phase_counts"]["soak"] = soak["sampled_phase_counts"]
        soak_execution = _phase_execution_metrics(soak_bundle, int(final_soak["run_id"]))
        soak["execution_evidence"] = soak_execution
        soak["band_execution_evidence"] = _band_execution_metrics(soak_execution)
        soak["band_coverage_pass"] = _band_coverage_pass(soak_execution, required_bands=("GOLD",))
        soak["band_coverage_required"] = ["GOLD"]
        soak["bands_reached"] = [band for band, values in soak["band_execution_evidence"].items()
                                  if int(values.get("progress_tasks", 0) or 0) > 0]
        soak["duration_seconds_total"] = round(time.monotonic() - soak_started, 2)
        soak["dashboard"] = _dashboard_probe(soak_bundle, soak_url)
        soak["pass"] = bool(soak["outcome"]["status"] == "stopped" and soak["resume"] and
                             soak["bridge_restart_pass"] is True and
                             _stage_pass(final_soak, bounded=True) and soak["supplemental"]["isolation_pass"] and
                             soak["band_coverage_pass"] and
                             soak["dashboard"]["identity_ok"] and soak["dashboard"]["live_refresh_ok"]
                             and soak["dashboard"]["workspace_isolation_ok"])
        report["soak"] = soak
        if soak["supplemental"].get("external_failures"):
            report["external_blockers"].append({"stage": "soak_supplemental", "error": soak["supplemental"]["error"],
                                                 "sources": soak["supplemental"].get("sources", {})})
        if soak["outcome"]["status"] == "extension_unresponsive":
            report["internal_failures"].append("Chrome extension did not start the soak run; reload the unpacked extension and rerun validator")
        if not soak["pass"]:
            report["internal_failures"].append("15-minute isolated soak failed")
            return 1

        semi_bundle = _isolated_bundle(semi_db, semi_out, _free_port())
        _prepare_validation_bundle(semi_bundle)
        semi_url, _ = ensure_dashboard(semi_bundle, open_browser=False)
        dashboard_bundles.append((semi_bundle, semi_url))
        report["dashboard_url"] = semi_url
        semi_started = time.monotonic()
        semi_deadline = semi_started + semi_minutes * 60
        # Five controlled representative probes make band proof deterministic
        # without waiting for a long GOLD task to naturally drain into later
        # bands.  The probes still use compiled SearchTasks and the normal
        # browser/bridge path; only the validation queue is sampled.
        probe_count = 5
        grace_budget = probe_count * VALIDATION_STOP_GRACE_SECONDS + 60
        phase_seconds = max(30, (semi_minutes * 60 - grace_budget) // probe_count)
        phase_runs: dict[str, Any] = {}
        with _isolated_environment(semi_bundle):
            probes = (
                ("A_FASTEST_DOOR_RECENT", "GOLD"),
                ("A_FASTEST_DOOR_RECENT", "SILVER"),
                ("B_REMAINING_CORE_RECENT", "GROWTH"),
                ("B_REMAINING_CORE_RECENT", "HEDGE"),
                ("C_DEEP_BACKFILL", "DEEP_TAIL"),
            )
            for phase, band in probes:
                phase_runs[f"{phase}:{band}"] = _run_semi_phase(
                    semi_bundle,
                    phase=phase,
                    platforms=list(PRIMARY),
                    phase_seconds=phase_seconds,
                    active_runs=active_runs,
                    stage_deadline=semi_deadline,
                    sample_band=band,
                )
            last_run_id = int(phase_runs["C_DEEP_BACKFILL:DEEP_TAIL"]["final"]["run_id"])
            supplemental = _run_supplemental(semi_bundle, last_run_id)
        sampled_phase_counts: dict[str, dict[str, int]] = {}
        execution_evidence: dict[str, Any] = {}
        phase_pass: dict[str, bool] = {}
        for probe_key, phase_result in phase_runs.items():
            run_id = int(phase_result["final"]["run_id"])
            for phase, platforms in _sampled_phase_counts(semi_bundle, run_id).items():
                for platform, count in platforms.items():
                    sampled_phase_counts.setdefault(phase, {})[platform] = sampled_phase_counts.setdefault(phase, {}).get(platform, 0) + count
            for phase, platforms in phase_result["execution"].items():
                target_phase = execution_evidence.setdefault(phase, {})
                for platform, values in platforms.items():
                    target = target_phase.setdefault(platform, {
                        "sampled_queued": 0, "started_tasks": 0, "progress_tasks": 0,
                        "cards_persisted": 0, "details_complete": 0, "external_tasks": 0,
                        "bands": {},
                    })
                    for key in ("sampled_queued", "started_tasks", "progress_tasks", "cards_persisted", "details_complete", "external_tasks"):
                        target[key] += int(values.get(key, 0) or 0)
                    for band_name, band_values in values.get("bands", {}).items():
                        band_target = target["bands"].setdefault(band_name, {
                            "sampled_queued": 0, "started_tasks": 0, "progress_tasks": 0,
                            "cards_persisted": 0, "details_complete": 0, "external_tasks": 0,
                        })
                        for key in band_target:
                            band_target[key] += int(band_values.get(key, 0) or 0)
            phase = phase_result["phase"]
            phase_pass[phase] = phase_pass.get(phase, True) and bool(phase_result["pass"])
        band_evidence = _band_execution_metrics(execution_evidence)
        semi = {
            "run_id": last_run_id,
            "phase_runs": phase_runs,
            "sampled_phase_counts": sampled_phase_counts,
            "execution_evidence": execution_evidence,
            "band_execution_evidence": band_evidence,
            "band_coverage_pass": _band_coverage_pass(execution_evidence),
            "phase_pass": phase_pass,
            "supplemental": supplemental,
        }
        report["run_now_coverage"]["live_sampled_phase_counts"]["semi_production"] = sampled_phase_counts
        report["run_now_coverage"]["semi_execution_evidence"] = execution_evidence
        semi["duration_seconds_total"] = round(time.monotonic() - semi_started, 2)
        semi["dashboard"] = _dashboard_probe(semi_bundle, semi_url)
        semi["pass"] = bool(
            all(phase_pass.values())
            and _phase_coverage_pass(execution_evidence)
            and _band_coverage_pass(execution_evidence)
            and semi["supplemental"]["isolation_pass"]
            and semi["dashboard"]["identity_ok"]
            and semi["dashboard"]["live_refresh_ok"]
            and semi["dashboard"]["workspace_isolation_ok"]
        )
        report["semi_production"] = semi
        if semi["supplemental"].get("external_failures"):
            report["external_blockers"].append({"stage": "semi_supplemental", "error": semi["supplemental"]["error"],
                                                 "sources": semi["supplemental"].get("sources", {})})
        if any(
            phase_result["final"]["outcome"]["status"] == "extension_unresponsive"
            for phase_result in phase_runs.values()
        ):
            report["internal_failures"].append("Chrome extension did not start a semi-production phase; reload the unpacked extension and rerun validator")
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
        try:
            source_end = release_identity(PROJECT_ROOT, require_upstream=True)
            report["source_identity_end"] = source_end
            report["source_unchanged"] = identity_unchanged(PROJECT_ROOT, source_snapshot)
            if not report["source_unchanged"]:
                report["internal_failures"].append("source tree or extension runtime identity changed during validation")
        except ProvenanceError as exc:
            report["source_identity_end"] = {"error": str(exc)}
            report["source_unchanged"] = False
            report["internal_failures"].append(f"source provenance unavailable at validation end: {exc}")
        report["PROD_READY"] = bool(report.get("PROD_READY") and not report.get("internal_failures"))
        _write_report(report, report_dir, prefix="micro" if micro_only else "")
        print(f"Validation database: {validation_db}")
        print(f"Soak database: {soak_db}")
        print(f"Semi-production database: {semi_db}")
        print(f"Dashboard: {report.get('dashboard_url') or 'not started'}")
        report_name = "micro-latest.json" if micro_only else "latest.json"
        print(f"Report: {report_dir / report_name}")
        print(f"PROD_READY={str(report['PROD_READY']).lower()}")
