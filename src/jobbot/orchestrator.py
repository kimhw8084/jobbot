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
from .extension_identity import expected_identity


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
        # Pass the rendezvous URL to LaunchServices as an actual URL operand.
        # Arguments after `open --args` become Chrome argv and never navigate
        # the existing normal profile; -g preserves background delivery.
        try:
            subprocess.run(
                ["open", "-g", "-a", "Google Chrome", url],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"macOS Google Chrome URL delivery failed (open exit {exc.returncode})"
            ) from exc
        except OSError as exc:
            raise RuntimeError("macOS Google Chrome URL delivery could not start") from exc
        return
    executable = chrome_path()
    if not executable:
        raise RuntimeError("normal installed Google Chrome was not found")
    subprocess.Popen([executable, "--new-window", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_dashboard_workspace(bundle: ConfigBundle, dashboard_url: str) -> None:
    extension_id = (bundle.root / "config" / "EXTENSION_ID.txt").read_text(encoding="utf-8").strip()
    manifest = json.loads((bundle.root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    identity = expected_identity(bundle.root)
    url = (f"chrome-extension://{extension_id}/dashboard.html?dashboard_url="
           f"{urllib.parse.quote(dashboard_url, safe='')}&expected_build={urllib.parse.quote(str(manifest.get('version_name') or ''))}"
           f"&expected_version={urllib.parse.quote(str(manifest.get('version') or ''))}"
           f"&expected_runtime_digest={urllib.parse.quote(identity['runtime_digest'])}")
    _open_chrome(url)


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


def launch_browser_run(bundle: ConfigBundle, run_id: int, *, wait: bool = True, open_browser: bool = True,
                       timeout_seconds: float | None = None, stop_after_seconds: float | None = None,
                       test_bridge_restart_after: float | None = None,
                       startup_timeout_seconds: float | None = None,
                       dashboard_url: str | None = None,
                       validation_stage_id: str | None = None) -> RunOutcome:
    runtime = bundle.runtime["runtime"]
    restarts_allowed = int(runtime["bridge_restart_limit"])
    token = secrets.token_urlsafe(48)
    port = _free_high_port()
    extension_id = (bundle.root / "config" / "EXTENSION_ID.txt").read_text(encoding="utf-8").strip()
    manifest = json.loads((bundle.root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    expected_build = str(manifest.get("version_name") or "")
    expected_version = str(manifest.get("version") or "")
    expected_runtime_digest = expected_identity(bundle.root)["runtime_digest"]
    log_path = bundle.output_dir / "logs" / f"run_{run_id}_bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    restart_count = 0
    started_monotonic = time.monotonic()
    stop_sent = False
    bridge_restart_sent = False
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
            f"&expected_build={urllib.parse.quote(expected_build)}&expected_version={urllib.parse.quote(expected_version)}"
            f"&expected_runtime_digest={urllib.parse.quote(expected_runtime_digest)}"
        )
        if dashboard_url:
            url += f"&dashboard_url={urllib.parse.quote(dashboard_url, safe='')}"
        if validation_stage_id:
            url += f"&validation_stage_id={urllib.parse.quote(validation_stage_id, safe='')}"
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
            elapsed = time.monotonic() - started_monotonic
            if startup_timeout_seconds is not None and elapsed >= max(1, startup_timeout_seconds) and status == "queued":
                browser_tasks.emergency_stop(bundle.root, run_id)
                return RunOutcome(run_id, "extension_unresponsive", restart_count)
            if stop_after_seconds is not None and not stop_sent and elapsed >= max(0, stop_after_seconds):
                browser_tasks.request_stop(bundle.root, run_id)
                stop_sent = True
            if test_bridge_restart_after is not None and not bridge_restart_sent and elapsed >= max(0, test_bridge_restart_after):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                bridge_restart_sent = True
            if timeout_seconds is not None and elapsed >= max(1, timeout_seconds):
                browser_tasks.emergency_stop(bundle.root, run_id)
                return RunOutcome(run_id, "timed_out", restart_count)
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


def enqueue(bundle: ConfigBundle, mode: str, platforms: list[str] | None = None, *, due_only: bool = False) -> int:
    return browser_tasks.enqueue_production(bundle.root, mode, platforms, due_only=due_only)


def resume(bundle: ConfigBundle, run_id: int | None = None) -> int:
    return browser_tasks.resume_run(bundle.root, run_id)
