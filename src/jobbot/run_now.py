from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigBundle
from .db import Database
from .dashboard import database_identity
from .orchestrator import chrome_path, open_dashboard_workspace
from .search_plan import compile_staged_and_write
from .version import PRODUCT_VERSION
from .provenance import release_identity


@dataclass(frozen=True)
class PreflightResult:
    task_count: int
    database_path: Path
    dashboard_url: str


def assert_production_release(bundle: ConfigBundle) -> None:
    """Fail closed unless the exact checked-out code was fully validated."""
    production_db = (bundle.root / "data" / "jobs.sqlite3").resolve()
    production_out = (bundle.root / "out").resolve()
    if bundle.database_path.resolve() != production_db or bundle.output_dir.resolve() != production_out:
        raise RuntimeError("production guard refused non-production database/output binding")
    if int(bundle.runtime["runtime"]["dashboard_port"]) != 8765:
        raise RuntimeError("production guard refused non-production dashboard port")
    report_path = bundle.root / "out" / "production-validation" / "latest.json"
    if not report_path.is_file():
        raise RuntimeError("production guard refused start: no latest full validation report exists")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"production guard refused unreadable validation report: {exc}") from exc
    current = release_identity(bundle.root)
    if not current["clean_worktree"]:
        raise RuntimeError("production guard refused start: relevant worktree is not clean")
    manifest = json.loads((bundle.root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    build = str(manifest.get("version_name") or "")
    if report.get("PROD_READY") is not True or report.get("internal_failures") != []:
        raise RuntimeError("production guard refused start: latest validation is not PROD_READY with zero internal failures")
    if str(report.get("head") or "") != current["head"]:
        raise RuntimeError(f"production guard refused stale validation: report HEAD {report.get('head')} != current HEAD {current['head']}")
    if str(report.get("tree") or report.get("source_tree") or "") != current["tree"]:
        raise RuntimeError("production guard refused stale validation: source tree identity differs")
    if report.get("clean_worktree") is not True or report.get("head_equals_upstream") is not True:
        raise RuntimeError("production guard refused validation without a clean pushed source tree")
    if report.get("source_unchanged") is not True:
        raise RuntimeError("production guard refused validation without an unchanged source identity")
    certified = report.get("source_identity_end") or report.get("source_identity_start") or {}
    for field in ("head", "tree", "upstream_ref", "upstream_sha"):
        if str(certified.get(field) or "") != str(current.get(field) or ""):
            raise RuntimeError(f"production guard refused validation: certified {field} differs")
    if str(report.get("extension_build") or report.get("validated_extension_build") or "") != build:
        raise RuntimeError("production guard refused stale extension build validation")
    if str(report.get("extension_runtime_digest") or "") != current["extension_runtime_digest"]:
        raise RuntimeError("production guard refused stale extension runtime bytes")
    if os.environ.get("JOBBOT_DATABASE_PATH") and Path(os.environ["JOBBOT_DATABASE_PATH"]).resolve() != production_db:
        raise RuntimeError("production guard refused inherited database override")
    if os.environ.get("JOBBOT_OUTPUT_DIR") and Path(os.environ["JOBBOT_OUTPUT_DIR"]).resolve() != production_out:
        raise RuntimeError("production guard refused inherited output override")
    if os.environ.get("JOBBOT_DASHBOARD_PORT") and str(os.environ["JOBBOT_DASHBOARD_PORT"]) != "8765":
        raise RuntimeError("production guard refused inherited dashboard port override")
    if Database(bundle).integrity_check() != "ok":
        raise RuntimeError("production guard refused database with failed integrity_check")


def preflight(bundle: ConfigBundle, platforms: list[str] | None = None) -> PreflightResult:
    migration = Database(bundle).migrate()
    if migration.integrity_after != "ok":
        raise RuntimeError(f"database integrity check failed: {migration.integrity_after}")
    if not chrome_path():
        raise RuntimeError("normal installed Google Chrome was not found")
    required = (
        bundle.root / "extension" / "manifest.json",
        bundle.root / "extension" / "service_worker.js",
        bundle.root / "extension" / "dashboard.html",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"extension files missing: {', '.join(missing)}")
    tasks, _ = compile_staged_and_write(bundle, platforms)
    if not tasks or any(task.max_results is not None for task in tasks):
        raise RuntimeError("staged production search plan is empty or contains a result cap")
    port = int(bundle.runtime["runtime"]["dashboard_port"])
    return PreflightResult(len(tasks), bundle.database_path, f"http://127.0.0.1:{port}/")


def _dashboard_json(url: str, endpoint: str) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(url + endpoint, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _dashboard_identity(url: str) -> dict[str, object] | None:
    return _dashboard_json(url, "api/identity")


def _legacy_dashboard_detected(url: str) -> bool:
    payload = _dashboard_json(url, "api/summary")
    return bool(payload is not None and "total" in payload)


def ensure_dashboard(bundle: ConfigBundle, *, open_browser: bool = True) -> tuple[str, bool]:
    port = int(bundle.runtime["runtime"]["dashboard_port"])
    url = f"http://127.0.0.1:{port}/"
    started = False
    conn = Database(bundle).connect()
    try:
        expected = {
            "jobbot_version": PRODUCT_VERSION, "workspace_root": str(bundle.root.resolve()),
            "resolved_database_path": str(bundle.database_path.resolve()),
            "database_identity": database_identity(conn, bundle),
        }
    finally:
        conn.close()
    actual = _dashboard_identity(url)
    if actual is not None:
        mismatch = [key for key, value in expected.items() if str(actual.get(key, "")) != value]
        if mismatch:
            raise RuntimeError(
                "dashboard identity mismatch; refusing to reuse it. "
                f"expected_db={expected['resolved_database_path']} expected_workspace={expected['workspace_root']} "
                f"actual_db={actual.get('resolved_database_path','unknown')} actual_workspace={actual.get('workspace_root','unknown')} "
                f"actual_version={actual.get('jobbot_version','unknown')} pid={actual.get('pid','unknown')}. "
                f"Stop the known JobBot dashboard and restart with: {sys.executable} -m jobbot dashboard --port {port}"
            )
    elif _legacy_dashboard_detected(url):
        raise RuntimeError(
            "a dashboard-like process occupies the requested port but does not expose identity; "
            f"refusing to reuse or kill it. expected_db={expected['resolved_database_path']} port={port}. "
            f"Restart a known JobBot dashboard with: {sys.executable} -m jobbot dashboard --port {port}"
        )
    else:
        log_path = bundle.output_dir / "logs" / "dashboard.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        child_env = os.environ.copy()
        child_env.update({
            "JOBBOT_DATABASE_PATH": str(bundle.database_path),
            "JOBBOT_OUTPUT_DIR": str(bundle.output_dir),
            "JOBBOT_DASHBOARD_PORT": str(port),
        })
        with log_path.open("ab") as log_handle:
            subprocess.Popen(
                [sys.executable, "-m", "jobbot", "dashboard", "--no-open", "--port", str(port)],
                cwd=bundle.root, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True,
                env=child_env,
            )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _dashboard_identity(url) is None:
            time.sleep(0.15)
        actual = _dashboard_identity(url)
        if actual is None:
            raise RuntimeError(f"dashboard did not start; see {log_path}")
        mismatch = [key for key, value in expected.items() if str(actual.get(key, "")) != value]
        if mismatch:
            raise RuntimeError(f"new dashboard identity mismatch: expected {expected}, actual {actual}; see {log_path}")
        started = True
    if open_browser:
        open_dashboard_workspace(bundle, url)
    return url, started
