from __future__ import annotations

import json
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import browser_tasks
from .config import ConfigBundle


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    status: str
    bridge_restarts: int


def chrome_path() -> str | None:
    if sys.platform == "darwin":
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(mac) if mac.is_file() else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for base in (Path.home() / "AppData/Local/Google/Chrome/Application", Path("C:/Program Files/Google/Chrome/Application")):
            candidate = base / "chrome.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _open_chrome(url: str) -> None:
    if sys.platform == "darwin":
        # LaunchServices' background flag prevents a long read-only crawl from
        # activating Chrome and stealing the user's current workspace.
        subprocess.Popen(["open", "-g", "-a", "Google Chrome", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    executable = chrome_path()
    if not executable:
        raise RuntimeError("normal installed Google Chrome was not found")
    subprocess.Popen([executable, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _free_high_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health(port: int, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health", headers={"X-JobBot-Token": token}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_status(bundle: ConfigBundle, run_id: int) -> str:
    conn = sqlite3.connect(bundle.database_path)
    try:
        row = conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
        return str(row[0]) if row else "missing"
    finally:
        conn.close()


def launch_browser_run(bundle: ConfigBundle, run_id: int, *, wait: bool = True, open_browser: bool = True) -> RunOutcome:
    runtime = bundle.runtime["runtime"]
    restarts_allowed = int(runtime["bridge_restart_limit"])
    token = secrets.token_urlsafe(48)
    port = _free_high_port()
    extension_id = (bundle.root / "config" / "EXTENSION_ID.txt").read_text(encoding="utf-8").strip()
    log_path = bundle.output_dir / "logs" / f"run_{run_id}_bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    restart_count = 0
    process: subprocess.Popen[bytes] | None = None
    ready_dir = tempfile.TemporaryDirectory(prefix="jobbot-bridge-")
    ready_path = Path(ready_dir.name) / "ready.json"
    log_handle = log_path.open("ab")

    def start() -> subprocess.Popen[bytes]:
        if ready_path.exists():
            ready_path.unlink()
        proc = subprocess.Popen(
            [sys.executable, "-m", "jobbot.bridge.server", "--port", str(port), "--token", token, "--ready-file", str(ready_path)],
            cwd=bundle.root, stdout=log_handle, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"loopback bridge exited during startup; see {log_path}")
            if ready_path.is_file():
                health = _health(port, token)
                if health.get("ok"):
                    return proc
            time.sleep(0.1)
        proc.terminate()
        raise RuntimeError(f"loopback bridge health check timed out; see {log_path}")

    try:
        process = start()
        url = (
            f"chrome-extension://{extension_id}/dashboard.html?autorun=1&run_id={run_id}"
            f"&bridge_port={port}&bridge_token={urllib.parse.quote(token)}"
        )
        if open_browser:
            _open_chrome(url)
        else:
            print(url)
        if not wait:
            return RunOutcome(run_id, _run_status(bundle, run_id), restart_count)
        while True:
            status = _run_status(bundle, run_id)
            if status in {"completed", "partial", "stopped", "failed", "missing"}:
                return RunOutcome(run_id, status, restart_count)
            if process.poll() is not None:
                if restart_count >= restarts_allowed:
                    return RunOutcome(run_id, "bridge_failed", restart_count)
                restart_count += 1
                process = start()
            time.sleep(2)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log_handle.close()
        ready_dir.cleanup()


def enqueue(bundle: ConfigBundle, mode: str, platforms: list[str] | None = None) -> int:
    return browser_tasks.enqueue_production(bundle.root, mode, platforms)


def resume(bundle: ConfigBundle, run_id: int | None = None) -> int:
    return browser_tasks.resume_run(bundle.root, run_id)
