#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import rpc as native
from .. import browser_tasks as v3

MAX_BODY = 4 * 1024 * 1024
RPC_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(f"[jobbot-bridge] {msg}", file=sys.stderr, flush=True)


class BridgeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    def __init__(self, addr, handler, token: str):
        super().__init__(addr, handler)
        self.token = token
        self.expected_origin = f"chrome-extension://{v3.EXTENSION_ID}"


class Handler(BaseHTTPRequestHandler):
    server_version = "JobBotLoopback/3.2.1"
    def log_message(self, fmt: str, *args: Any) -> None:
        log(fmt % args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin == self.server.expected_origin:  # type: ignore[attr-defined]
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-JobBot-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = self.headers.get("X-JobBot-Token", "")
        return bool(token) and secrets.compare_digest(token, self.server.token)  # type: ignore[attr-defined]

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if not self._authorized():
            self._json(403, {"ok": False, "error": "unauthorized"}); return
        if parsed.path == "/health":
            self._json(200, {"ok": True, "version": v3.V3_VERSION, "bridge": "loopback"}); return
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/run-status":
            self._json(200, native.handle({"action": "run_status", "run_id": int((query.get("run_id") or ["0"])[0])})); return
        if parsed.path == "/task-status":
            self._json(200, native.handle({"action": "task_status", "task_id": int((query.get("task_id") or ["0"])[0])})); return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/rpc":
            self._json(404, {"ok": False, "error": "not_found"}); return
        if not self._authorized():
            self._json(403, {"ok": False, "error": "unauthorized"}); return
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._json(413, {"ok": False, "error": "invalid_body_size"}); return
        try:
            msg = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(msg, dict):
                raise ValueError("request must be a JSON object")
            with RPC_LOCK:
                resp = native.handle(msg)
            if msg.get("request_id") and isinstance(resp, dict):
                resp["request_id"] = msg.get("request_id")
            self._json(200, resp if isinstance(resp, dict) else {"ok": False, "error": "invalid_response"})
        except Exception as e:
            log(f"RPC error: {type(e).__name__}: {e}")
            self._json(500, {"ok": False, "error": type(e).__name__, "message": str(e)[:700]})


def write_ready(path: Path, port: int, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": port, "token": token, "extension_id": v3.EXTENSION_ID}), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


def self_test() -> int:
    token = secrets.token_urlsafe(32)
    srv = BridgeServer(("127.0.0.1", 0), Handler, token)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_port}/rpc",
            data=json.dumps({"action": "ping", "request_id": "selftest"}).encode(),
            headers={"Content-Type": "application/json", "X-JobBot-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            obj = json.loads(r.read().decode())
        assert obj.get("ok") and obj.get("request_id") == "selftest", obj
        unauth = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/health", method="GET")
        try:
            urllib.request.urlopen(unauth, timeout=10)
            raise AssertionError("unauthorized health request unexpectedly succeeded")
        except urllib.error.HTTPError as e:
            try:
                assert e.code == 403, e.code
            finally:
                e.close()
        print(f"LOOPBACK BRIDGE SELF-TEST PASSED — 127.0.0.1:{srv.server_port}")
        return 0
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="JobBot v3 loopback bridge")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ready-file")
    ap.add_argument("--token")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    token = args.token or secrets.token_urlsafe(48)
    srv = BridgeServer(("127.0.0.1", max(0, args.port)), Handler, token)
    stop = threading.Event()
    def shutdown(_sig=None, _frame=None):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)
    if args.ready_file:
        write_ready(Path(args.ready_file).expanduser().resolve(), srv.server_port, token)
    log(f"started on 127.0.0.1:{srv.server_port}; extension={v3.EXTENSION_ID}")
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close(); log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
