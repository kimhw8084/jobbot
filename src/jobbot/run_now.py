from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigBundle
from .db import Database
from .orchestrator import chrome_path
from .search_plan import compile_and_write


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
    tasks, _ = compile_and_write(bundle, "fast", platforms)
    if not tasks or any(task.max_results is not None for task in tasks):
        raise RuntimeError("fast production search plan is empty or contains a result cap")
    port = int(bundle.runtime["runtime"]["dashboard_port"])
    return PreflightResult(len(tasks), bundle.database_path, f"http://127.0.0.1:{port}/")


def _dashboard_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/summary", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and "total" in payload
    except Exception:
        return False


def ensure_dashboard(bundle: ConfigBundle, *, open_browser: bool = True) -> tuple[str, bool]:
    port = int(bundle.runtime["runtime"]["dashboard_port"])
    url = f"http://127.0.0.1:{port}/"
    started = False
    if not _dashboard_healthy(url):
        log_path = bundle.output_dir / "logs" / "dashboard.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_handle:
            subprocess.Popen(
                [sys.executable, "-m", "jobbot", "dashboard", "--no-open", "--port", str(port)],
                cwd=bundle.root, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True,
            )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not _dashboard_healthy(url):
            time.sleep(0.15)
        if not _dashboard_healthy(url):
            raise RuntimeError(f"dashboard did not start; see {log_path}")
        started = True
    if open_browser:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Google Chrome", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([chrome_path() or "google-chrome", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return url, started
