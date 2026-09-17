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
from .extension_identity import extension_build
from .runtime_binding import chrome_open_command, chrome_target_diagnostics, sync_extension


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
    # macOS keeps the supported background argv ["open", "-g", ...] inside
    # chrome_open_command while adding the bound --profile-directory.
    executable = chrome_path()
    subprocess.Popen(chrome_open_command(url, executable), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def _bridge_rpc(port: int, token: str, payload: dict[str, object], *, timeout: float = 10) -> dict[str, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/rpc",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-JobBot-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("loopback bridge returned a non-object response")
    return value


def _request_extension_refresh(port: int, token: str, *, run_id: int = 0,
                               expected_build: str, refresh_id: str) -> dict[str, object]:
    return _bridge_rpc(port, token, {
        "action": "extension_refresh", "request_id": refresh_id, "refresh_id": refresh_id,
        "run_id": run_id, "expected_build": expected_build,
    })


def _run_status(bundle: ConfigBundle, run_id: int) -> str:
    conn = sqlite3.connect(bundle.database_path)
    try:
        row = conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
        return str(row[0]) if row else "missing"
    finally:
        conn.close()


def _active_run_id(bundle: ConfigBundle) -> int | None:
    """Read the durable run latch before replacing the loaded extension tree."""
    database = bundle.database_path
    if not database.is_file():
        return None
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(
            "SELECT browser_run_id FROM browser_runs WHERE status='running' ORDER BY browser_run_id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def launch_browser_run(bundle: ConfigBundle, run_id: int, *, wait: bool = True, open_browser: bool = True,
                       timeout_seconds: float | None = None, stop_after_seconds: float | None = None,
                       test_bridge_restart_after: float | None = None,
                       startup_timeout_seconds: float | None = None) -> RunOutcome:
    active_run_id = _active_run_id(bundle)
    if active_run_id is not None:
        raise RuntimeError(json.dumps({
            "classification": "active_run",
            "error": "active_run",
            "active_run_id": active_run_id,
        }, ensure_ascii=False, sort_keys=True))
    sync_extension(bundle.root)
    target = chrome_target_diagnostics()
    if not target.get("ok"):
        raise RuntimeError(json.dumps({
            "classification": "wrong_or_untargeted_chrome_profile_or_instance",
            "error": "chrome_profile_binding_required",
            "chrome_target": target,
        }, ensure_ascii=False, sort_keys=True))
    runtime = bundle.runtime["runtime"]
    restarts_allowed = int(runtime["bridge_restart_limit"])
    token = secrets.token_urlsafe(48)
    port = _free_high_port()
    extension_id = (bundle.root / "config" / "EXTENSION_ID.txt").read_text(encoding="utf-8").strip()
    expected_build = extension_build(bundle.root)
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
        refresh_id = f"run-{run_id}-{expected_build}"
        refresh = _request_extension_refresh(port, token, run_id=run_id,
                                             expected_build=expected_build, refresh_id=refresh_id)
        if not refresh.get("ok"):
            raise RuntimeError(f"extension refresh request rejected: {refresh.get('error', 'unknown error')}")
        url = (
            f"chrome-extension://{extension_id}/dashboard.html?autorun=1&run_id={run_id}"
            f"&bridge_port={port}&bridge_token={urllib.parse.quote(token)}"
            f"&expected_build={urllib.parse.quote(expected_build, safe='')}&refresh_id={urllib.parse.quote(refresh_id, safe='')}"
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


def refresh_extension(bundle: ConfigBundle, *, timeout_seconds: float = 45,
                      open_browser: bool = True) -> dict[str, object]:
    """Ask the installed unpacked extension to refresh and prove its build.

    The temporary bridge carries the same token-authenticated control path as a
    browser run. It is intentionally independent of the production database
    run queue, so this maintenance action cannot start or mutate a search run.
    """
    expected_build = extension_build(bundle.root)
    active_run_id = _active_run_id(bundle)
    if active_run_id is not None:
        return {
            "ok": False,
            "status": "failed",
            "error": "active_run",
            "active_run_id": active_run_id,
            "diagnostics": {
                "classification": "active_run",
                "active_run_id": active_run_id,
                "message": "maintenance refresh is blocked while a browser run is active",
            },
        }
    try:
        deployment = sync_extension(bundle.root)
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": "bootstrap_or_deployment_source_mismatch",
            "expected_build": expected_build,
            "diagnostics": {
                "classification": "bootstrap_or_deployment_source_mismatch",
                "error": str(exc),
            },
        }
    target = chrome_target_diagnostics()
    if not target.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "error": "chrome_profile_binding_required",
            "expected_build": expected_build,
            "deployment": deployment.as_dict(),
            "diagnostics": {
                "classification": "wrong_or_untargeted_chrome_profile_or_instance",
                "chrome_target": target,
            },
        }
    runtime_binding = {
        "ok": True,
        "classification": "runtime_binding_ready",
        "deployment": deployment.as_dict(),
        "chrome_target": target,
    }
    token = secrets.token_urlsafe(48)
    extension_id = deployment.extension_id
    refresh_id = f"maintenance-{secrets.token_urlsafe(18)}"
    log_path = bundle.output_dir / "logs" / "extension_refresh_bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ready_dir = tempfile.TemporaryDirectory(prefix="jobbot-extension-refresh-")
    ready_path = Path(ready_dir.name) / "ready.json"
    log_handle = log_path.open("ab")
    process: subprocess.Popen[bytes] | None = None

    def start() -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "jobbot.bridge.server", "--port", "0", "--token", token,
             "--ready-file", str(ready_path)],
            cwd=bundle.root, stdout=log_handle, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"loopback bridge exited during extension refresh; see {log_path}")
            if ready_path.is_file():
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
                port = int(ready["port"])
                if _health(port, token).get("ok"):
                    return proc
            time.sleep(0.1)
        proc.terminate()
        raise RuntimeError(f"loopback bridge health check timed out; see {log_path}")

    try:
        process = start()
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        port = int(ready["port"])
        request = _request_extension_refresh(port, token, expected_build=expected_build, refresh_id=refresh_id)
        if not request.get("ok"):
            return {"ok": False, "refresh_id": refresh_id, "runtime_binding": runtime_binding, **request}
        url = (
            f"chrome-extension://{extension_id}/dashboard.html?maintenance=1&autorun=1"
            f"&bridge_port={port}&bridge_token={urllib.parse.quote(token)}"
            f"&expected_build={urllib.parse.quote(expected_build, safe='')}"
            f"&refresh_id={urllib.parse.quote(refresh_id, safe='')}"
        )
        if open_browser:
            _open_chrome(url)
        else:
            print(url)
        deadline = time.monotonic() + max(1, timeout_seconds)
        while time.monotonic() < deadline:
            try:
                status = _bridge_rpc(port, token, {"action": "extension_refresh_status", "refresh_id": refresh_id}, timeout=5)
            except Exception as exc:
                return {
                    "ok": False,
                    "refresh_id": refresh_id,
                    "error": "bridge_auth_or_configuration_failure",
                    "runtime_binding": runtime_binding,
                    "diagnostics": {"classification": "bridge_auth_or_configuration_failure", "error": str(exc)},
                }
            if status.get("status") == "confirmed" and status.get("identity_confirmed") is True:
                return {**status, "runtime_binding": runtime_binding}
            if status.get("status") == "failed":
                return {**status, "runtime_binding": runtime_binding}
            time.sleep(0.5)
        try:
            failed = _bridge_rpc(
                port, token,
                {"action": "extension_refresh_failed", "refresh_id": refresh_id,
                 "error": "extension_unavailable_or_unreachable"},
                timeout=5,
            )
        except Exception:
            failed = {}
        return {**failed, "ok": False, "refresh_id": refresh_id,
                "status": failed.get("status", "failed"),
                "error": "extension_unavailable_or_unreachable", "expected_build": expected_build,
                "runtime_binding": runtime_binding,
                "diagnostics": {
                    "classification": "extension_absent_disabled_or_unavailable",
                    "expected_build": expected_build,
                    "chrome_target": target,
                    "deployment": deployment.as_dict(),
                }}
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
