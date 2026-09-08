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
from .orchestrator import chrome_path
from .search_plan import compile_staged_and_write


@dataclass(frozen=True)
class PreflightResult:
    task_count: int
    database_path: Path
    dashboard_url: str


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
            "jobbot_version": "3.2.1", "workspace_root": str(bundle.root.resolve()),
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
        deadline = time.monotonic() + 20
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
        if sys.platform == "darwin":
            # RUN NOW is a background worker; opening its local dashboard must
            # not interrupt the user's active Mac application.
            subprocess.Popen(["open", "-g", "-a", "Google Chrome", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([chrome_path() or "google-chrome", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return url, started
