from __future__ import annotations

import json
import csv
import copy
import contextlib
import http.client
import http.server
import io
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from typing import Any
import urllib.request
from unittest import mock

from jobbot.acquisition import brightdata as brightdata_module
from jobbot.acquisition.brightdata import (
    BrightDataHTTPResponse, BrightDataHTTPTransport, BrightDataJobsProvider,
    BrightDataRuntimeConfig, brightdata_preflight, brightdata_runtime_config,
    parse_platform_config,
)
from jobbot.acquisition.models import ProviderCompletionState, ProviderFailure, ProviderFailureClass
from jobbot import cli
from jobbot.acquisition.coordinator import acquire
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.dashboard import live_discoveries
from jobbot.db import Database
from jobbot.exports import export_all
from jobbot.search_plan import compile_plan
from jobbot.search_quality import search_quality_metrics


SECRET = "build-only-brightdata-secret-value"


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeWireSocket:
    """Scripted raw socket for exercising http.client's real response parser."""

    def __init__(self, chunks, *, clock):
        self.chunks = deque({"data": bytes(data), "delay": float(delay)} for data, delay in chunks)
        self.clock = clock
        self.timeout = None
        self.timeout_history = []
        self.attempted_chunks = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeout_history.append(timeout)

    def recv_into(self, buffer):
        if not self.chunks:
            return 0
        segment = self.chunks[0]
        self.attempted_chunks.append(bytes(segment["data"]))
        if segment["delay"] > self.timeout:
            self.clock.advance(self.timeout)
            raise socket.timeout()
        self.clock.advance(segment["delay"])
        segment["delay"] = 0
        count = min(len(buffer), len(segment["data"]))
        buffer[:count] = segment["data"][:count]
        segment["data"] = segment["data"][count:]
        if not segment["data"]:
            self.chunks.popleft()
        return count

    def close(self):
        self.closed = True


class FakeHTTPConnection:
    def __init__(self, sock, *, on_connect=None, on_close=None):
        self.sock = sock
        self.on_connect = on_connect
        self.on_close = on_close
        self.request_info = None
        self.closed = False

    def connect(self):
        if self.on_connect is not None:
            self.on_connect()

    def request(self, method, target, body=None, headers=None):
        self.request_info = (method, target, body, dict(headers or {}))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.sock.close()
        if self.on_close is not None:
            self.on_close()


class FakeHTTPConnectionFactory:
    def __init__(self, scripts, *, clock, on_connect=None, on_close=None):
        self.scripts = deque(scripts)
        self.clock = clock
        self.on_connect = on_connect
        self.on_close = on_close
        self.connections = []

    def __call__(self, _host, _port=None, timeout=None):
        if not self.scripts:
            raise AssertionError("unexpected HTTP connection")
        sock = FakeWireSocket(self.scripts.popleft(), clock=self.clock)
        connection = FakeHTTPConnection(sock, on_connect=self.on_connect, on_close=self.on_close)
        connection.timeout = timeout
        self.connections.append(connection)
        return connection


class FakeSetupSocket(FakeWireSocket):
    """Offline socket with separately timed TCP and TLS setup phases."""

    def __init__(self, chunks, *, clock, tcp_delay):
        super().__init__(chunks, clock=clock)
        self.tcp_delay = tcp_delay
        self.sent = []

    def connect(self, _address):
        if self.tcp_delay > self.timeout:
            self.clock.advance(self.timeout)
            raise socket.timeout()
        self.clock.advance(self.tcp_delay)

    def sendall(self, data):
        self.sent.append(bytes(data))

    def setsockopt(self, *_args):
        return None


class FakeTLSContext:
    def __init__(self, *, clock, handshake_delay):
        self.clock = clock
        self.handshake_delay = handshake_delay
        self.timeout = None
        self.post_handshake_auth = None
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True

    def set_alpn_protocols(self, _protocols):
        return None

    def wrap_socket(self, network_socket, **_kwargs):
        self.timeout = network_socket.timeout
        if self.handshake_delay > self.timeout:
            self.clock.advance(self.timeout)
            raise socket.timeout()
        self.clock.advance(self.handshake_delay)
        return network_socket


def http_wire_response(status, headers=(), body_chunks=()):
    reason = {200: "OK", 302: "Found", 500: "Internal Server Error"}.get(status, "Response")
    headers = list(headers)
    if not any(key.casefold() in {"content-length", "transfer-encoding"} for key, _value in headers):
        headers.append(("Content-Length", str(sum(len(data) for data, _delay in body_chunks))))
    head = f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
    head += b"".join(f"{key}: {value}\r\n".encode("ascii") for key, value in headers)
    head += b"\r\n"
    return [(head, 0), *[(data, delay) for data, delay in body_chunks]]


def json_http_wire_response(status, payload, *, body_chunks=None):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if body_chunks is None:
        body_chunks = [(body, 0)]
    return http_wire_response(
        status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))], body_chunks,
    )


class MockTransport:
    def __init__(self, responses, *, on_request=None):
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []
        self.on_request = on_request

    def request(self, method, path, *, params, json_body, token, timeout_seconds, task_deadline, on_attempt):
        on_attempt()
        call = {"method": method, "path": path, "params": dict(params),
                "json_body": json_body, "token": token,
                "timeout_seconds": timeout_seconds, "task_deadline": task_deadline}
        self.calls.append(call)
        if self.on_request is not None:
            self.on_request(call)
        if not self.responses:
            raise AssertionError(f"unexpected transport request: {method} {path}")
        value = self.responses.popleft()
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(method, path, params, json_body, token)
        return value


class SnapshotSequenceTransport:
    """A fully mocked snapshot flow; only the first task returns fixture rows."""

    def __init__(self, first_rows):
        self.first_rows = first_rows
        self.snapshot_number = 0
        self.calls = []

    def request(self, method, path, *, params, json_body, token, timeout_seconds, task_deadline, on_attempt):
        on_attempt()
        self.calls.append({"method": method, "path": path, "params": dict(params),
                           "json_body": json_body, "token": token,
                           "timeout_seconds": timeout_seconds, "task_deadline": task_deadline})
        if method == "POST" and path.endswith("/trigger"):
            self.snapshot_number += 1
            return response(200, {"snapshot_id": f"snapshot-{self.snapshot_number}"})
        if path.endswith("/parts"):
            return response(200, {"parts": 1})
        if "/progress/" in path:
            return response(200, {"status": "ready"})
        if "/snapshot/" in path:
            rows = self.first_rows if self.snapshot_number == 1 else []
            return response(200, rows)
        raise AssertionError(f"unexpected transport request: {method} {path}")


def platform_config(platform: str, *, rows_key: str = ""):
    input_fields = {
        "linkedin": {
            "keyword": "keywords", "location": {"field": "area", "source": "home_state"},
            "remote": {"field": "remote_only", "value": "yes"},
            "window_days": {"field": "days_back", "type": "integer"},
        },
        "indeed": {
            "keyword": "search_term", "location": {"field": "where", "source": "home_metro"},
            "window_days": {"field": "freshness", "template": "past_{days}_days"},
        },
        "glassdoor": {"keyword": "search_phrase"},
    }[platform]
    output_names = {
        "linkedin": {"source_job_id": "job_posting_id", "source_url": "job_url", "title": "job_title",
                     "company": "company_name", "location": "job_location", "posted_date": "date_posted",
                     "posted_text": "posted_text", "employment_type": "employment_type",
                     "description": "job_description", "summary": "job_summary", "salary": "salary_range",
                     "application_url": "apply_link", "ats_requisition_url": "ats_url",
                     "provider_record_id": "provider_row_id"},
        "indeed": {"source_job_id": "jobid", "source_url": "url", "title": "title",
                   "company": "company", "location": "location", "posted_date": "date_posted_parsed",
                   "posted_text": "date_posted", "employment_type": "job_type", "description": "description_text",
                   "salary": "salary", "application_url": "application_url", "provider_record_id": "record_id"},
        "glassdoor": {"source_job_id": "posting_id", "source_url": "job_link", "title": "job_title",
                      "company": "employer_name", "location": "job_location", "posted_text": "date_posted",
                      "employment_type": "employment_type", "summary": "job_overview", "salary": "salary_text",
                      "application_url": "apply_url", "provider_record_id": "row_id"},
    }[platform]
    value = {
        "dataset_id": f"runtime-{platform}-dataset-id",
        "input_schema": input_fields,
        "output_schema": output_names,
    }
    if rows_key:
        value["result_rows_key"] = rows_key
    return parse_platform_config(platform, json.dumps(value))


def response(status: int, payload: Any):
    return BrightDataHTTPResponse(status, payload)


def ready_flow(rows: list[dict[str, Any]], *, parts: int = 1):
    return [
        response(200, {"snapshot_id": "snapshot-build-1"}),
        response(200, {"snapshot_id": "snapshot-build-1", "status": "ready"}),
        response(200, {"parts": parts}),
        *[response(200, rows[index:index + 1]) for index in range(parts)],
    ]


class BrightDataJobsProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()

    def task(self, platform):
        return compile_plan(self.bundle, "fast", [platform])[0]

    def provider(self, platform, transport, *, max_records=100, max_polls=5,
                 retries=2, rows_key="", task_timeout_seconds=300, monotonic=time.monotonic,
                 poll_interval_seconds=0, sleep=None):
        runtime = BrightDataRuntimeConfig(
            token=SECRET, platforms={platform: platform_config(platform, rows_key=rows_key)},
            task_timeout_seconds=task_timeout_seconds,
        )
        return BrightDataJobsProvider(
            runtime, candidate=self.bundle.candidate["candidate"], max_records=max_records,
            transport=transport, max_polls=max_polls, max_transient_retries=retries,
            poll_interval_seconds=poll_interval_seconds,
            sleep=sleep or (lambda _seconds: None), monotonic=monotonic,
        )

    def row(self, platform, *, job_id="job-101", suffix=""):
        config = platform_config(platform)
        key = config.output_schema
        values = {
            "source_job_id": job_id, "source_url": f"https://jobs.example.test/{job_id}",
            "title": "Patient Enrollment Specialist", "company": "Example Health",
            "location": "Texas", "posted_date": "2026-09-22", "posted_text": "1 day ago",
            "employment_type": "Full-time", "description": "Observed role description", 
            "summary": "Observed card summary", "salary": "$50,000 - $60,000",
            "application_url": "https://jobs.example.test/apply", "provider_record_id": suffix or "row-101",
        }
        return {field: values[semantic] for semantic, field in key.items() if semantic in values}

    def test_offline_preflight_reports_presence_and_schema_only(self):
        valid_linkedin = {
            "dataset_id": "not-reported", "input_schema": {"keyword": "kw"},
            "output_schema": {"source_job_id": "id"},
        }
        env = {
            "BRIGHTDATA_API_TOKEN": SECRET,
            "JOBBOT_BRIGHTDATA_LINKEDIN_CONFIG": json.dumps(valid_linkedin),
            "JOBBOT_BRIGHTDATA_INDEED_CONFIG": "{broken-json",
        }
        status = brightdata_preflight(["linkedin", "indeed", "glassdoor"], env)
        encoded = json.dumps(status)
        self.assertTrue(status["token_present"])
        self.assertEqual(status["token_validity"], "not_checked_offline")
        self.assertTrue(status["platform_configs"]["linkedin"]["valid"])
        self.assertFalse(status["platform_configs"]["indeed"]["valid"])
        self.assertFalse(status["platform_configs"]["glassdoor"]["present"])
        self.assertFalse(status["valid"])
        self.assertNotIn(SECRET, encoded)
        runtime = BrightDataRuntimeConfig(SECRET, {"linkedin": platform_config("linkedin")})
        self.assertEqual(runtime.task_timeout_seconds, 300)
        self.assertNotIn(SECRET, repr(runtime))

    def test_task_timeout_configuration_must_be_positive_finite_and_secret_safe(self):
        env = {
            "BRIGHTDATA_API_TOKEN": "offline-placeholder",
            "JOBBOT_BRIGHTDATA_LINKEDIN_CONFIG": json.dumps({
                "dataset_id": "offline-dataset", "input_schema": {"keyword": "kw"},
                "output_schema": {"source_job_id": "id"},
            }),
        }
        configured = brightdata_runtime_config(["linkedin"], environ={
            **env, "JOBBOT_BRIGHTDATA_TASK_TIMEOUT_SECONDS": "12.5",
        })
        self.assertEqual(configured.task_timeout_seconds, 12.5)
        for value in ("0", "-1", "NaN", "Infinity", "secret-setting"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    brightdata_runtime_config(["linkedin"], environ={
                        **env, "JOBBOT_BRIGHTDATA_TASK_TIMEOUT_SECONDS": value,
                    })
                self.assertNotIn(value, str(raised.exception))
                status = brightdata_preflight(["linkedin"], {
                    **env, "JOBBOT_BRIGHTDATA_TASK_TIMEOUT_SECONDS": value,
                })
                self.assertFalse(status["task_timeout_valid"])
                self.assertFalse(status["valid"])

        with self.assertRaises(ValueError):
            BrightDataRuntimeConfig(SECRET, {}, task_timeout_seconds=float("inf"))

    def test_all_platform_keyword_mappings_and_observed_field_mappers(self):
        expected_inputs = {
            "linkedin": lambda task: {"keywords": task.query, "area": "TX", "remote_only": "yes", "days_back": task.age_days},
            "indeed": lambda task: {"search_term": task.query, "where": "Austin Metropolitan Area", "freshness": f"past_{task.age_days}_days"},
            "glassdoor": lambda task: {"search_phrase": task.query},
        }
        for platform in ("linkedin", "indeed", "glassdoor"):
            with self.subTest(platform=platform):
                task = self.task(platform)
                row = self.row(platform)
                transport = MockTransport(ready_flow([row]))
                provider = self.provider(platform, transport)
                batch = provider.fetch(task)
                self.assertTrue(batch.proven_complete)
                self.assertEqual(batch.completion_state, ProviderCompletionState.COMPLETE)
                self.assertEqual(batch.requests_submitted, len(transport.calls))
                self.assertEqual(batch.requests_submitted, 4)
                self.assertEqual(batch.provider_metadata["requests_submitted"], 4)
                self.assertEqual(batch.provider_metadata["task_timeout_seconds"], 300)
                self.assertTrue(all(call["timeout_seconds"] == 45 for call in transport.calls))
                self.assertEqual(transport.calls[0]["json_body"], [expected_inputs[platform](task)])
                self.assertEqual(transport.calls[0]["params"]["type"], "discover_new")
                self.assertEqual(transport.calls[0]["params"]["discover_by"], "keyword")
                record = batch.records[0]
                self.assertEqual(record.source_surface, platform)
                self.assertEqual(record.query_task_key, task.task_key)
                self.assertEqual(record.source_job_id, "job-101")
                self.assertEqual(record.card["title"], "Patient Enrollment Specialist")
                self.assertEqual(record.card.get("posted_date"), "2026-09-22" if platform != "glassdoor" else None)
                self.assertEqual(record.detail.get("salary_text"), "$50,000 - $60,000" if platform != "glassdoor" else "$50,000 - $60,000")
                self.assertEqual(record.source_urls.get("observed_board_apply_url"), "https://jobs.example.test/apply")
                self.assertEqual(batch.provider_task_id, "snapshot-build-1")
                self.assertEqual(batch.completion_evidence["batch"]["part_numbers"], [1])
                self.assertEqual(batch.completion_evidence["platform"], platform)
                self.assertEqual(batch.completion_evidence["task_key"], task.task_key)

    def test_trigger_pending_ready_and_multiple_result_parts(self):
        platform = "linkedin"
        rows = [self.row(platform, job_id="part-1"), self.row(platform, job_id="part-2")]
        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "starting"}), response(200, {"status": "running"}),
            response(200, {"status": "ready"}), response(200, {"parts": 2}),
            response(200, [rows[0]]), response(200, [rows[1]]),
        ])
        provider = self.provider(platform, transport)
        batch = provider.fetch(self.task(platform))
        self.assertTrue(batch.proven_complete)
        self.assertEqual([record.source_job_id for record in batch.records], ["part-1", "part-2"])
        self.assertEqual(batch.completion_evidence["batch"]["parts_retrieved"], 2)
        self.assertEqual(batch.completion_evidence["batch"]["part_numbers"], [1, 2])
        self.assertEqual(sum(call["path"].endswith("/progress/snapshot-build-1") for call in transport.calls), 3)
        self.assertEqual(batch.requests_submitted, len(transport.calls))
        self.assertEqual(batch.requests_submitted, 7)
        self.assertEqual(batch.records_delivered, 2)
        self.assertEqual(transport.calls[0]["token"], SECRET)
        self.assertNotIn(SECRET, json.dumps(batch.provider_metadata))
        self.assertEqual(batch.provider_metadata["request_shape"]["trigger"]["body"],
                         [{"keywords": self.task(platform).query, "area": "TX", "remote_only": "yes",
                           "days_back": self.task(platform).age_days}])

    def test_transient_429_and_5xx_are_retried_with_a_local_bound(self):
        platform = "indeed"
        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(429, {"error": "redacted body"}), response(503, {"error": "redacted body"}),
            response(200, {"status": "ready"}), response(200, {"parts": 1}), response(200, []),
        ])
        batch = self.provider(platform, transport, retries=2).fetch(self.task(platform))
        self.assertTrue(batch.proven_complete)
        self.assertEqual(sum(call["path"].endswith("/progress/snapshot-build-1") for call in transport.calls), 3)
        self.assertEqual(batch.requests_submitted, len(transport.calls))
        self.assertEqual(batch.requests_submitted, 6)

        exhausted = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-2"}),
            response(429, {}), response(503, {}), response(500, {}),
        ])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, exhausted, retries=2).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.TRANSPORT)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(exhausted.calls), 4)
        self.assertEqual(raised.exception.requests_submitted, len(exhausted.calls))
        self.assertEqual(raised.exception.provider_metadata["requests_submitted"], 4)

    def test_returned_cost_credits_are_preserved_without_estimation(self):
        platform = "glassdoor"
        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "ready", "credits": 0.25}),
            response(200, {"parts": 1}), response(200, []),
        ])
        batch = self.provider(platform, transport).fetch(self.task(platform))
        self.assertTrue(batch.proven_complete)
        self.assertEqual(batch.reported_cost, {"credits": 0.25})
        self.assertEqual(batch.provider_metadata["provider_reported_cost"], {"credits": 0.25})

    def test_token_echoes_in_provider_rows_and_cost_are_never_persisted(self):
        platform = "glassdoor"
        row = self.row(platform)
        row[platform_config(platform).output_schema["summary"]] = f"Unexpected echo: {SECRET}"
        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "ready", "credits": SECRET}),
            response(200, {"parts": 1}), response(200, [row]),
        ])
        batch = self.provider(platform, transport).fetch(self.task(platform))
        self.assertEqual(batch.completion_state, ProviderCompletionState.INCOMPLETE)
        self.assertEqual(batch.records, ())
        self.assertEqual(batch.reported_cost, {"credits": "[REDACTED]"})
        self.assertNotIn(SECRET, json.dumps(batch.provider_metadata))
        self.assertNotIn(SECRET, json.dumps(batch.reported_cost))

    def test_timeout_auth_and_schema_failures_have_precise_classes(self):
        platform = "linkedin"
        timeout_transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "running"}), response(200, {"status": "running"}),
        ])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, timeout_transport, max_polls=2).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.TIMEOUT)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, json.dumps(raised.exception.provider_metadata))
        self.assertEqual(raised.exception.requests_submitted, 3)
        self.assertEqual(raised.exception.provider_metadata["requests_submitted"], 3)
        self.assertEqual(raised.exception.provider_metadata["task_timeout_seconds"], 300)

        auth_transport = MockTransport([response(401, {"error": SECRET})])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, auth_transport).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.AUTHORIZATION)
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn(SECRET, str(raised.exception))

        schema_transport = MockTransport([response(400, {"error": SECRET})])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, schema_transport).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.SCHEMA)
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn(SECRET, json.dumps(raised.exception.provider_metadata))

    def test_task_deadline_caps_readiness_sleep_and_prevents_later_poll(self):
        clock = FakeClock()
        sleeps = []

        def advance_request(call):
            clock.advance(0.25 if call["method"] == "POST" else 0.4)

        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "running"}),
        ], on_request=advance_request)

        def sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        with self.assertRaises(ProviderFailure) as raised:
            self.provider("linkedin", transport, task_timeout_seconds=1, monotonic=clock,
                          poll_interval_seconds=5, sleep=sleep).fetch(self.task("linkedin"))

        self.assertEqual(raised.exception.classification, ProviderFailureClass.TIMEOUT)
        self.assertEqual(raised.exception.requests_submitted, 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.35)
        self.assertEqual([call["timeout_seconds"] for call in transport.calls], [1, 0.75])
        self.assertEqual(clock(), 1)
        metadata = raised.exception.provider_metadata
        self.assertEqual(metadata["requests_submitted"], 2)
        self.assertEqual(metadata["task_timeout_seconds"], 1)
        self.assertEqual(metadata["task_elapsed_seconds"], 1)
        self.assertNotIn(SECRET, json.dumps(metadata))

    def test_urllib_success_response_shape_is_supported_by_deadline_transport(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"snapshot_id":"local-only"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return None

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        api_root = f"http://127.0.0.1:{server.server_port}"
        attempts = []
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"{api_root}/shape", timeout=2) as urllib_response:
                self.assertIsNotNone(urllib_response.fp)
                self.assertTrue(callable(urllib_response.fp.read))
                self.assertFalse(hasattr(urllib_response.fp, "fp"))

            with mock.patch("jobbot.acquisition.brightdata.API_ROOT", api_root):
                result = BrightDataHTTPTransport().request(
                    "GET", "/shape", params={}, json_body=None, token=SECRET,
                    timeout_seconds=2, task_deadline=time.monotonic() + 3,
                    on_attempt=lambda: attempts.append(1),
                )
            self.assertEqual(result.status, 200)
            self.assertEqual(result.payload, {"snapshot_id": "local-only"})
            self.assertEqual(attempts, [1])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_deadline_covers_delayed_response_headers(self):
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory([[
            (b"HTTP/1.1 200 OK\r\n", 0),
            (b"Content-Length: 2\r\n\r\n", 1.1),
        ]], clock=clock)
        attempts = []
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/headers", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1, on_attempt=lambda: attempts.append(1),
                )
        self.assertEqual(clock(), 1)
        self.assertEqual(attempts, [1])
        self.assertEqual(len(factory.connections), 1)
        self.assertEqual(factory.connections[0].sock.attempted_chunks[-1], b"Content-Length: 2\r\n\r\n")

    def test_deadline_is_checked_after_connection_before_http_request(self):
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory(
            [json_http_wire_response(200, {"unexpected": True})],
            clock=clock, on_connect=lambda: clock.advance(1),
        )
        attempts = []
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/connect", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1, on_attempt=lambda: attempts.append(1),
                )
        self.assertEqual(attempts, [1])
        self.assertEqual(clock(), 1)
        self.assertEqual(len(factory.connections), 1)
        self.assertIsNone(factory.connections[0].request_info)

    def test_connection_error_after_expiry_is_classified_as_timeout(self):
        clock = FakeClock()

        def fail_after_expiry():
            clock.advance(1.1)
            raise OSError("late connection failure")

        factory = FakeHTTPConnectionFactory(
            [json_http_wire_response(200, {"unexpected": True})],
            clock=clock, on_connect=fail_after_expiry,
        )
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/late-connect-error", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1, on_attempt=lambda: None,
                )
        self.assertGreater(clock(), 1)
        self.assertIsNone(factory.connections[0].request_info)

    def test_https_tcp_and_tls_setup_share_the_remaining_task_deadline(self):
        clock = FakeClock()
        network_socket = FakeSetupSocket(
            json_http_wire_response(200, {"ok": True}), clock=clock, tcp_delay=0.6,
        )
        tls_context = FakeTLSContext(clock=clock, handshake_delay=0.3)
        transport = BrightDataHTTPTransport(monotonic=clock)
        attempts = []
        with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
             mock.patch("jobbot.acquisition.brightdata._deadline_getaddrinfo", return_value=[
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
             ]), \
             mock.patch("jobbot.acquisition.brightdata.socket.socket", return_value=network_socket), \
             mock.patch("jobbot.acquisition.brightdata.http.client.ssl._create_default_https_context",
                        return_value=tls_context):
            result = transport.request(
                "GET", "/setup", params={}, json_body=None, token=SECRET,
                timeout_seconds=10, task_deadline=1, on_attempt=lambda: attempts.append(1),
            )

        self.assertEqual(result.payload, {"ok": True})
        self.assertAlmostEqual(clock(), 0.9)
        self.assertEqual(tls_context.timeout, 0.4)
        self.assertEqual(network_socket.timeout_history[:2], [1, 0.4])
        self.assertTrue(network_socket.sent)
        self.assertEqual(attempts, [1])

    def test_https_tls_handshake_cannot_reuse_the_full_tcp_timeout(self):
        clock = FakeClock()
        network_socket = FakeSetupSocket(
            json_http_wire_response(200, {"unexpected": True}), clock=clock, tcp_delay=0.6,
        )
        tls_context = FakeTLSContext(clock=clock, handshake_delay=0.6)
        transport = BrightDataHTTPTransport(monotonic=clock)
        attempts = []
        with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
             mock.patch("jobbot.acquisition.brightdata._deadline_getaddrinfo", return_value=[
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
             ]), \
             mock.patch("jobbot.acquisition.brightdata.socket.socket", return_value=network_socket), \
             mock.patch("jobbot.acquisition.brightdata.http.client.ssl._create_default_https_context",
                        return_value=tls_context):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/staged-setup", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1, on_attempt=lambda: attempts.append(1),
                )

        self.assertEqual(clock(), 1)
        self.assertEqual(tls_context.timeout, 0.4)
        self.assertEqual(network_socket.timeout_history[:2], [1, 0.4])
        self.assertFalse(network_socket.sent)
        self.assertEqual(attempts, [1])

    def test_slow_dns_is_killed_at_deadline_for_http_and_https(self):
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                spawned = []
                attempts = []
                request_calls = []
                created_sockets = []
                start = time.monotonic()
                budget = 0.3
                task_deadline = start + budget
                transport = BrightDataHTTPTransport(monotonic=time.monotonic)
                real_socket = socket.socket

                def start_slow_worker(_host, _port):
                    process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, close_fds=True,
                    )
                    spawned.append(process)
                    return process

                def track_socket(*args, **kwargs):
                    created_sockets.append(time.monotonic())
                    return real_socket(*args, **kwargs)

                original_request = http.client.HTTPConnection.request

                def track_request(connection, method, *args, **kwargs):
                    request_calls.append((connection.host, method, time.monotonic()))
                    return original_request(connection, method, *args, **kwargs)

                try:
                    with mock.patch("jobbot.acquisition.brightdata.API_ROOT",
                                    f"{scheme}://dns-slow.example"), \
                         mock.patch("jobbot.acquisition.brightdata._start_dns_worker",
                                    side_effect=start_slow_worker), \
                         mock.patch("jobbot.acquisition.brightdata.socket.socket",
                                    side_effect=track_socket), \
                         mock.patch.object(http.client.HTTPConnection, "request",
                                           new=track_request):
                        with self.assertRaises(TimeoutError):
                            transport.request(
                                "GET", "/slow-dns", params={}, json_body=None, token=SECRET,
                                timeout_seconds=5, task_deadline=task_deadline,
                                on_attempt=lambda: attempts.append(1),
                            )

                    elapsed = time.monotonic() - start
                    self.assertGreaterEqual(elapsed, 0.2)
                    self.assertLessEqual(elapsed, budget + 0.08)
                    self.assertEqual(attempts, [1])
                    self.assertEqual(created_sockets, [])
                    self.assertEqual(request_calls, [])
                    self.assertEqual(len(spawned), 1)
                    self.assertIsNotNone(spawned[0].poll(), "DNS worker was not reaped")
                finally:
                    for process in spawned:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=1)

    def test_slow_dns_redirect_targets_are_suppressed_for_http_and_https(self):
        for target_scheme in ("http", "https"):
            with self.subTest(target_scheme=target_scheme):
                requests_seen = []

                class RedirectHandler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        requests_seen.append(time.monotonic())
                        self.send_response(302)
                        self.send_header(
                            "Location", f"{target_scheme}://dns-slow.example/target",
                        )
                        self.send_header("Content-Length", "0")
                        self.end_headers()

                    def log_message(self, *_args):
                        return None

                server = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                spawned = []
                attempts = []
                request_calls = []
                created_sockets = []
                start = time.monotonic()
                budget = 0.4
                task_deadline = start + budget
                transport = BrightDataHTTPTransport(monotonic=time.monotonic)
                original_start_worker = brightdata_module._start_dns_worker
                real_socket = socket.socket
                original_request = http.client.HTTPConnection.request

                def start_worker(host, port):
                    if host != "dns-slow.example":
                        return original_start_worker(host, port)
                    process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, close_fds=True,
                    )
                    spawned.append(process)
                    return process

                def track_socket(*args, **kwargs):
                    created_sockets.append(time.monotonic())
                    return real_socket(*args, **kwargs)

                def track_request(connection, method, *args, **kwargs):
                    request_calls.append((connection.host, method, time.monotonic()))
                    return original_request(connection, method, *args, **kwargs)

                try:
                    api_root = f"http://127.0.0.1:{server.server_port}"
                    with mock.patch("jobbot.acquisition.brightdata.API_ROOT", api_root), \
                         mock.patch("jobbot.acquisition.brightdata._start_dns_worker",
                                    side_effect=start_worker), \
                         mock.patch("jobbot.acquisition.brightdata.socket.socket",
                                    side_effect=track_socket), \
                         mock.patch.object(http.client.HTTPConnection, "request",
                                           new=track_request):
                        with self.assertRaises(TimeoutError):
                            transport.request(
                                "GET", "/redirect", params={}, json_body=None, token=SECRET,
                                timeout_seconds=5, task_deadline=task_deadline,
                                on_attempt=lambda: attempts.append(1),
                            )

                    elapsed = time.monotonic() - start
                    self.assertGreaterEqual(elapsed, 0.25)
                    self.assertLessEqual(elapsed, budget + 0.08)
                    self.assertEqual(len(requests_seen), 1)
                    self.assertEqual(attempts, [1, 1])
                    self.assertEqual(len(request_calls), 1)
                    self.assertLess(request_calls[0][2], task_deadline)
                    self.assertGreaterEqual(len(created_sockets), 1)
                    self.assertTrue(all(created_at < task_deadline
                                        for created_at in created_sockets))
                    self.assertEqual(len(spawned), 1)
                    self.assertIsNotNone(spawned[0].poll(), "DNS worker was not reaped")
                finally:
                    for process in spawned:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=1)
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)
                    self.assertFalse(server_thread.is_alive())

    def test_deadline_covers_slow_content_length_body_reads(self):
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory([http_wire_response(
            200, [("Content-Length", "4")], [(b"ab", 0.6), (b"cd", 0.6)],
        )], clock=clock)
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/slow-body", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1, on_attempt=lambda: None,
                )
        self.assertEqual(clock(), 1)
        self.assertEqual(factory.connections[0].sock.attempted_chunks[-1], b"cd")
        self.assertLessEqual(factory.connections[0].sock.timeout_history[-1], 0.4)

    def test_chunked_data_crlf_and_trailers_are_read_under_the_task_deadline(self):
        frame = [
            (b"4\r\n", 0), (b"Wiki", 0), (b"\r\n", 0),
            (b"5\r\n", 0), (b"pedia", 0), (b"\r\n", 0), (b"0\r\n", 0),
            (b"X-Trailer: verified\r\n", 0), (b"\r\n", 0),
        ]
        successful_clock = FakeClock()
        successful_factory = FakeHTTPConnectionFactory([http_wire_response(
            200, [("Transfer-Encoding", "chunked")], frame,
        )], clock=successful_clock)
        transport = BrightDataHTTPTransport(monotonic=successful_clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=successful_factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=successful_factory):
            result = transport.request(
                "GET", "/chunked", params={}, json_body=None, token=SECRET,
                timeout_seconds=10, task_deadline=1, on_attempt=lambda: None,
            )
        self.assertEqual(result.payload, "Wikipedia")
        self.assertEqual(dict(result.headers).get("Transfer-Encoding"), "chunked")

        frame_phases = {
            "chunk data": 1,
            "chunk CRLF": 2,
            "trailer": 7,
        }
        for phase, target_index in frame_phases.items():
            with self.subTest(phase=phase):
                clock = FakeClock()
                delayed_frame = list(frame)
                delayed_frame[target_index] = (delayed_frame[target_index][0], 1.1)
                factory = FakeHTTPConnectionFactory([http_wire_response(
                    200, [("Transfer-Encoding", "chunked")], delayed_frame,
                )], clock=clock)
                transport = BrightDataHTTPTransport(monotonic=clock)
                with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
                     mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
                    with self.assertRaises(TimeoutError):
                        transport.request(
                            "GET", "/chunked", params={}, json_body=None, token=SECRET,
                            timeout_seconds=10, task_deadline=1, on_attempt=lambda: None,
                        )
                self.assertEqual(clock(), 1)
                self.assertEqual(factory.connections[0].sock.attempted_chunks[-1], delayed_frame[target_index][0])

    def test_deadline_covers_http_error_body_reads(self):
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory([http_wire_response(
            500, [("Content-Length", "4")], [(b"fail", 1.1)],
        )], clock=clock)
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/error", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1, on_attempt=lambda: None,
                )
        self.assertEqual(clock(), 1)
        self.assertEqual(factory.connections[0].sock.attempted_chunks[-1], b"fail")

    def test_redirect_hops_are_counted_and_expired_targets_are_suppressed(self):
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory([
            http_wire_response(302, [("Location", "/target")]),
            json_http_wire_response(200, {"ok": True}),
        ], clock=clock)
        attempts = []
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            result = transport.request(
                "GET", "/origin", params={}, json_body=None, token=SECRET,
                timeout_seconds=10, task_deadline=1, on_attempt=lambda: attempts.append(1),
            )
        self.assertEqual(result.payload, {"ok": True})
        self.assertEqual(attempts, [1, 1])
        self.assertEqual([connection.request_info[1] for connection in factory.connections], ["/origin", "/target"])
        self.assertTrue(all(connection.request_info[3].get("Authorization") == f"Bearer {SECRET}"
                            for connection in factory.connections))

        redirect_clock = FakeClock()
        redirect_factory = FakeHTTPConnectionFactory([
            http_wire_response(302, [("Location", "https://redirect.example/target")]),
            json_http_wire_response(200, {"redirected": True}),
        ], clock=redirect_clock)
        redirect_attempts = []
        transport = BrightDataHTTPTransport(monotonic=redirect_clock)
        with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=redirect_factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=redirect_factory):
            redirected = transport.request(
                "POST", "/trigger", params={}, json_body={"keyword": "test"}, token=SECRET,
                timeout_seconds=10, task_deadline=1, on_attempt=lambda: redirect_attempts.append(1),
            )
        self.assertEqual(redirected.payload, {"redirected": True})
        self.assertEqual(redirect_attempts, [1, 1])
        self.assertEqual(redirect_factory.connections[1].request_info[0], "GET")
        self.assertIsNone(redirect_factory.connections[1].request_info[2])
        self.assertNotIn("Authorization", redirect_factory.connections[1].request_info[3])
        self.assertNotIn("Content-Type", redirect_factory.connections[1].request_info[3])

        expired_clock = FakeClock()
        expired_factory = FakeHTTPConnectionFactory([
            http_wire_response(302, [("Location", "/must-not-run")]),
            json_http_wire_response(200, {"unexpected": True}),
        ], clock=expired_clock, on_close=lambda: expired_clock.advance(1))
        expired_attempts = []
        transport = BrightDataHTTPTransport(monotonic=expired_clock)
        with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=expired_factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=expired_factory):
            with self.assertRaises(TimeoutError):
                transport.request(
                    "GET", "/origin", params={}, json_body=None, token=SECRET,
                    timeout_seconds=10, task_deadline=1,
                    on_attempt=lambda: expired_attempts.append(1),
                )
        self.assertEqual(expired_attempts, [1])
        self.assertEqual(len(expired_factory.connections), 1)
        self.assertEqual(expired_factory.connections[0].request_info[1], "/origin")

    def test_redirect_method_and_body_matrix(self):
        body = {"keyword": "offline-test"}
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        methods = ("GET", "HEAD", "POST", "DELETE", "PUT", "OPTIONS")
        statuses = (301, 302, 303, 307, 308)

        for method in methods:
            for status in statuses:
                with self.subTest(method=method, status=status):
                    clock = FakeClock()
                    factory = FakeHTTPConnectionFactory([
                        http_wire_response(status, [("Location", "https://redirect.example/target")]),
                        json_http_wire_response(200, {"ok": True}),
                    ], clock=clock)
                    transport = BrightDataHTTPTransport(monotonic=clock)
                    attempts = []
                    with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
                         mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection",
                                    side_effect=factory), \
                         mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection",
                                    side_effect=factory):
                        transport.request(
                            method, "/origin", params={},
                            json_body=None if method == "HEAD" else body,
                            token=SECRET, timeout_seconds=10, task_deadline=1,
                            on_attempt=lambda: attempts.append(1),
                        )

                    redirected_method, _target, redirected_body, redirected_headers = \
                        factory.connections[1].request_info
                    rewrite_to_get = (method == "POST" and status in {301, 302}) \
                        or (status == 303 and method != "HEAD")
                    self.assertEqual(redirected_method, "GET" if rewrite_to_get else method)
                    self.assertEqual(attempts, [1, 1])
                    self.assertEqual(len(factory.connections), 2)
                    if rewrite_to_get or method == "HEAD":
                        self.assertIsNone(redirected_body)
                    else:
                        self.assertEqual(redirected_body, body_bytes)
                    self.assertNotIn("Authorization", redirected_headers)
                    if rewrite_to_get:
                        for header in ("Content-Type", "Content-Length", "Transfer-Encoding"):
                            self.assertNotIn(header, redirected_headers)
                    else:
                        self.assertEqual(redirected_headers.get("Content-Type"), "application/json")

    def test_cross_origin_preserved_method_redirect_strips_authorization(self):
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory([
            http_wire_response(307, [("Location", "https://redirect.example/target")]),
            json_http_wire_response(200, {"ok": True}),
        ], clock=clock)
        body = {"keyword": "offline-test"}
        transport = BrightDataHTTPTransport(monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.API_ROOT", "https://api.example"), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            transport.request(
                "DELETE", "/delete", params={}, json_body=body, token=SECRET,
                timeout_seconds=10, task_deadline=1, on_attempt=lambda: None,
            )

        method, _target, redirected_body, redirected_headers = factory.connections[1].request_info
        self.assertEqual(method, "DELETE")
        self.assertEqual(redirected_body, json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(redirected_headers.get("Content-Type"), "application/json")
        self.assertNotIn("Authorization", redirected_headers)

    def test_transient_get_retries_are_counted_as_separate_attempts(self):
        platform = "linkedin"
        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-retry-1"}),
            response(503, {"message": "temporary"}),
            response(200, {"status": "ready"}),
            response(200, {"parts": 1}),
            response(200, [self.row(platform, job_id="retry-601")]),
        ])
        batch = self.provider(platform, transport, retries=1).fetch(self.task(platform))
        self.assertEqual(batch.completion_state, ProviderCompletionState.COMPLETE)
        self.assertEqual(batch.requests_submitted, 5)
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual(sum(call["path"].find("/progress/") >= 0 for call in transport.calls), 2)

    def test_provider_request_count_includes_redirect_hops(self):
        platform = "linkedin"
        clock = FakeClock()
        factory = FakeHTTPConnectionFactory([
            json_http_wire_response(200, {"snapshot_id": "snapshot-redirect-1"}),
            http_wire_response(302, [("Location", "/progress-redirected")]),
            json_http_wire_response(200, {"status": "ready"}),
            json_http_wire_response(200, {"parts": 1}),
            json_http_wire_response(200, [self.row(platform, job_id="redirect-701")]),
        ], clock=clock)
        transport = BrightDataHTTPTransport(monotonic=clock)
        provider = self.provider(platform, transport, monotonic=clock)
        with mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            batch = provider.fetch(self.task(platform))
        self.assertEqual(batch.completion_state, ProviderCompletionState.COMPLETE)
        self.assertEqual(batch.requests_submitted, 5)
        self.assertEqual(len(factory.connections), 5)
        self.assertEqual(factory.connections[1].request_info[1].split("?", 1)[0],
                         "/datasets/v3/progress/snapshot-redirect-1")
        self.assertEqual(factory.connections[2].request_info[1], "/progress-redirected")

    def test_snapshot_ack_is_not_completion_and_missing_parts_are_ambiguous(self):
        platform = "glassdoor"
        pending = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "running"}), response(200, {"status": "running"}),
        ])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, pending, max_polls=2).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.TIMEOUT)
        self.assertEqual(len(pending.calls), 3)

        missing_parts = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "ready"}), response(200, {"unexpected": 1}),
        ])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, missing_parts).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.AMBIGUOUS_CURSOR)
        self.assertTrue(raised.exception.retryable)

    def test_successful_part_records_survive_a_later_result_failure(self):
        platform = "linkedin"
        transport = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}),
            response(200, {"status": "ready"}), response(200, {"parts": 2}),
            response(200, [self.row(platform, job_id="persist-part-1")]),
            response(429, {}), response(503, {}), response(500, {}),
        ])
        batch = self.provider(platform, transport, retries=2).fetch(self.task(platform))
        self.assertEqual(batch.completion_state, ProviderCompletionState.RETRYABLE)
        self.assertEqual(batch.failure_class, ProviderFailureClass.TRANSPORT)
        self.assertEqual([record.source_job_id for record in batch.records], ["persist-part-1"])
        self.assertEqual(batch.records_delivered, 1)
        self.assertEqual(batch.provider_metadata["parts_retrieved"], 1)
        self.assertEqual(batch.requests_submitted, len(transport.calls))
        self.assertEqual(batch.requests_submitted, 7)
        self.assertEqual(batch.provider_metadata["requests_submitted"], 7)
        self.assertFalse(batch.proven_complete)

    def test_cursor_truncation_and_malformed_rows_never_complete(self):
        platform = "indeed"
        config = platform_config(platform, rows_key="rows")
        runtime = BrightDataRuntimeConfig(SECRET, {platform: config})
        provider = BrightDataJobsProvider(
            runtime, candidate=self.bundle.candidate["candidate"], max_records=50,
            transport=MockTransport([
                response(200, {"snapshot_id": "snapshot-build-1"}), response(200, {"status": "ready"}),
                response(200, {"parts": 1}),
                response(200, {"rows": [self.row(platform)], "has_more": True, "next_cursor": "cursor-2"}),
            ]), poll_interval_seconds=0, sleep=lambda _seconds: None,
        )
        batch = provider.fetch(self.task(platform))
        self.assertEqual(batch.completion_state, ProviderCompletionState.INCOMPLETE)
        self.assertEqual(batch.failure_class, ProviderFailureClass.AMBIGUOUS_CURSOR)
        self.assertFalse(batch.proven_complete)
        self.assertEqual(batch.provider_metadata["continuation_markers"]["next_cursor"], "cursor-2")

        malformed = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-1"}), response(200, {"status": "ready"}),
            response(200, {"parts": 1}), response(200, {"wrong_container": []}),
        ])
        malformed_batch = self.provider(platform, malformed, rows_key="rows").fetch(self.task(platform))
        self.assertEqual(malformed_batch.completion_state, ProviderCompletionState.INCOMPLETE)
        self.assertEqual(malformed_batch.failure_class, ProviderFailureClass.INVALID_RESPONSE)
        self.assertFalse(malformed_batch.proven_complete)

    def test_record_budget_is_a_hard_stop_and_exact_cap_is_incomplete(self):
        platform = "linkedin"
        transport = MockTransport(ready_flow([self.row(platform, job_id="budget-1")]))
        provider = self.provider(platform, transport, max_records=1)
        first = provider.fetch(self.task(platform))
        self.assertEqual(first.completion_state, ProviderCompletionState.INCOMPLETE)
        self.assertEqual(first.failure_class, ProviderFailureClass.PARTIAL_BATCH)
        self.assertEqual(first.records_delivered, 1)
        self.assertFalse(first.proven_complete)
        prior_calls = len(transport.calls)
        second = provider.fetch(self.task(platform))
        self.assertEqual(second.completion_state, ProviderCompletionState.INCOMPLETE)
        self.assertTrue(second.provider_metadata["budget_stop"])
        self.assertEqual(second.requests_submitted, 0)
        self.assertEqual(len(transport.calls), prior_calls)

    def test_missing_identity_and_provider_trust_claims_are_not_normalized(self):
        platform = "linkedin"
        source_row = self.row(platform)
        source_row.pop("job_posting_id")
        source_row.pop("job_url")
        source_row["verified_application_url"] = "https://fake.example/verified"
        source_row["source_verification_state"] = "verified_direct_ats"
        source_row["application_destination_verification_state"] = "VERIFIED_ATS"
        transport = MockTransport(ready_flow([source_row]))
        batch = self.provider(platform, transport).fetch(self.task(platform))
        self.assertEqual(batch.completion_state, ProviderCompletionState.INCOMPLETE)
        self.assertEqual(batch.failure_class, ProviderFailureClass.INVALID_RESPONSE)
        self.assertEqual(batch.records, ())
        self.assertEqual(batch.provider_metadata["invalid_rows"], 1)

    def test_cli_missing_secret_or_platform_config_fails_before_run_creation(self):
        args = cli.parser().parse_args([
            "acquire", "--provider", "brightdata-jobs", "--live-transport", "--max-records", "1",
            "--platform", "linkedin",
        ])
        with mock.patch("jobbot.cli._bundle", return_value=self.bundle), \
             mock.patch("jobbot.cli.run_acquisition") as run_acquisition, \
             mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "", "JOBBOT_BRIGHTDATA_LINKEDIN_CONFIG": "{}"}), \
             contextlib.redirect_stderr(io.StringIO()):
            status = cli.command_acquire(args)
        self.assertEqual(status, 2)
        run_acquisition.assert_not_called()


class BrightDataSharedIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jobbot-brightdata-build-")
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT_ROOT / "config", self.root / "config")
        (self.root / "data").mkdir()
        (self.root / "out").mkdir()
        original = load_bundle(self.root)
        runtime = copy.deepcopy(original.runtime)
        runtime["runtime"]["database_path"] = str(self.root / "data" / "jobs.sqlite3")
        runtime["runtime"]["output_dir"] = str(self.root / "out")
        runtime["runtime"]["crawl_observations_path"] = str(self.root / "data" / "crawl_observations.sqlite3")
        runtime["ledger"]["backup_dir"] = str(self.root / "backups")
        self.bundle = ConfigBundle(self.root, original.strategy, original.candidate, runtime, original.live_search)
        self.database = Database(self.bundle)
        self.database.migrate()

    def tearDown(self):
        self.temp.cleanup()

    def provider(self, transport, *, run_id="brightdata-test-run", max_records=100,
                 task_timeout_seconds=300, monotonic=time.monotonic):
        return BrightDataJobsProvider(
            BrightDataRuntimeConfig(
                SECRET, {"linkedin": platform_config("linkedin")},
                task_timeout_seconds=task_timeout_seconds,
            ),
            candidate=self.bundle.candidate["candidate"], max_records=max_records,
            transport=transport, run_id=run_id, poll_interval_seconds=0,
            sleep=lambda _seconds: None, monotonic=monotonic,
        )

    def provider_row(self, job_id, provider_record_id, *, description="", location="", salary="",
                     trust_claims=False, apply_url=""):
        config = platform_config("linkedin")
        values = {
            "job_posting_id": job_id, "job_url": f"https://jobs.example.test/{job_id}",
            "job_title": "Patient Enrollment Specialist", "company_name": "Example Health",
            "provider_row_id": provider_record_id,
            "ats_url": f"https://boards.greenhouse.io/example/jobs/{job_id}",
        }
        optional = {"job_location": location, "salary_range": salary,
                    "job_description": description, "apply_link": apply_url}
        values.update({key: value for key, value in optional.items() if value})
        if trust_claims:
            values.update({
                "verified_application_url": "https://fake.example/verified",
                "source_verification_state": "verified_direct_ats",
                "application_destination_verification_state": "VERIFIED_ATS",
            })
        return values

    def test_shared_ingestion_is_identity_first_and_provider_claims_stay_untrusted(self):
        rows = [
            self.provider_row("card-only-201", "row-card"),
            self.provider_row("detail-202", "row-detail", description="Observed patient enrollment role. " * 20,
                              trust_claims=True),
        ]
        transport = SnapshotSequenceTransport(rows)
        result = acquire(self.bundle, self.provider(transport), mode="fast", platforms=["linkedin"])
        self.assertEqual(result["status"], "completed")
        conn = self.database.connect()
        result_rows = conn.execute(
            "SELECT * FROM search_task_results WHERE source_job_id IN ('card-only-201','detail-202') ORDER BY result_id"
        ).fetchall()
        self.assertEqual(len(result_rows), 2)
        self.assertTrue(all(row["acquisition_provider"] == "brightdata-jobs" for row in result_rows))
        task_id = int(result_rows[0]["task_id"])
        event_rows = conn.execute(
            "SELECT event_type,event_id FROM browser_events WHERE task_id=? AND event_type IN ('result_discovered','job_recorded') ORDER BY event_id",
            (task_id,),
        ).fetchall()
        discovered = [row["event_id"] for row in event_rows if row["event_type"] == "result_discovered"]
        recorded = [row["event_id"] for row in event_rows if row["event_type"] == "job_recorded"]
        self.assertEqual(len(discovered), 2)
        self.assertTrue(recorded)
        self.assertLess(max(discovered), min(recorded))

        detail_row = next(row for row in result_rows if row["source_job_id"] == "detail-202")
        metadata = json.loads(detail_row["provider_metadata_json"])
        serialized = json.dumps(metadata)
        for claim in ("verified_application_url", "source_verification_state", "application_destination_verification_state"):
            self.assertNotIn(claim, serialized)
        self.assertNotIn(SECRET, serialized)
        self.assertEqual(metadata["provider_job_id"], "snapshot-1")
        self.assertIn("request_shape", metadata)
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (detail_row["canonical_job_id"],)).fetchone()
        self.assertEqual(job["location_raw"], "")
        self.assertEqual(job["salary_text"], "")
        self.assertEqual(job["apply_url"], "")
        self.assertEqual(job["location_evidence_state"], "UNKNOWN")
        self.assertEqual(job["remote_evidence_state"], "UNKNOWN")
        self.assertNotEqual(job["evidence_readiness_state"], "READY")
        self.assertNotIn(job["recommendation"], {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})

        first_task = conn.execute(
            "SELECT * FROM browser_search_tasks WHERE browser_run_id=? AND task_id=?",
            (result["browser_run_id"], task_id),
        ).fetchone()
        self.assertEqual(first_task["provider_requests_submitted"], 4)
        self.assertEqual(first_task["provider_records_delivered"], 2)
        self.assertEqual(first_task["provider_reported_cost_json"], "{}")
        self.assertIn("request_shape", json.loads(first_task["provider_metadata_json"]))
        completion = json.loads(first_task["completion_evidence_json"])
        self.assertEqual(completion["platform"], "linkedin")
        self.assertEqual(completion["task_key"], first_task["task_key"])
        self.assertEqual(completion["provider_job_id"], "snapshot-1")
        self.assertTrue(completion["request_fingerprint"])

        dashboard = live_discoveries(conn)
        dashboard_detail = next(row for row in dashboard["discoveries"] if row["source_job_id"] == "detail-202")
        self.assertEqual(dashboard_detail["acquisition_provider"], "brightdata-jobs")
        metrics = search_quality_metrics(conn)
        diagnostic = next(item for item in metrics["provider_metrics"] if item["acquisition_provider"] == "brightdata-jobs")
        self.assertEqual(diagnostic["requests_submitted"], 4 * len(compile_plan(self.bundle, "fast", ["linkedin"])))
        self.assertEqual(diagnostic["records_delivered"], 2)
        self.assertEqual(metrics["metric_definition_version"], "chg114-search-quality-v2")
        order_row = next(item for item in metrics["current_order"] if item["task_id"] == task_id)
        self.assertEqual(order_row["effective_execution_rank"], first_task["effective_execution_rank"])
        self.assertEqual(order_row["baseline_execution_rank"], first_task["baseline_execution_rank"])
        self.assertNotIn("acquisition_provider", order_row)

        paths = export_all(conn, self.bundle.output_dir)
        with paths["all_jobs.csv"].open(encoding="utf-8-sig", newline="") as handle:
            exports = list(csv.DictReader(handle))
        self.assertIn("brightdata-jobs", {row["acquisition_providers"] for row in exports})
        export_metadata = json.loads(paths["provider_diagnostics.json"].read_text(encoding="utf-8"))
        self.assertIn("brightdata-jobs", {item["acquisition_provider"] for item in export_metadata["provider_metrics"]})
        conn.close()

    def test_deadline_after_result_part_persists_partial_records_without_completion_evidence(self):
        clock = FakeClock()
        task = compile_plan(self.bundle, "fast", ["linkedin"])[0]
        factory = FakeHTTPConnectionFactory([
            json_http_wire_response(200, {"snapshot_id": "snapshot-partial-1"}),
            json_http_wire_response(200, {"status": "ready"}),
            json_http_wire_response(200, {"parts": 2}),
            json_http_wire_response(200, [self.provider_row("partial-501", "partial-row")]),
            http_wire_response(200, [("Content-Length", "2")], [(b"[", 0.6), (b"]", 0.6)]),
        ], clock=clock)
        transport = BrightDataHTTPTransport(monotonic=clock)
        provider = self.provider(
            transport, run_id="brightdata-deadline-run", task_timeout_seconds=1, monotonic=clock,
        )
        with mock.patch("jobbot.acquisition.coordinator.compile_plan", return_value=[task]), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPConnection", side_effect=factory), \
             mock.patch("jobbot.acquisition.brightdata.http.client.HTTPSConnection", side_effect=factory):
            result = acquire(self.bundle, provider, mode="fast", platforms=["linkedin"])

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["task_complete_count"], 0)
        self.assertEqual(result["task_incomplete_count"], 1)
        self.assertEqual(len(factory.connections), 5)
        conn = self.database.connect()
        stored = conn.execute(
            "SELECT status,exhausted,provider_completion_state,provider_failure_class,"
            "provider_requests_submitted,provider_metadata_json,completion_evidence_json "
            "FROM browser_search_tasks WHERE browser_run_id=?",
            (result["browser_run_id"],),
        ).fetchone()
        self.assertEqual(stored["status"], "incomplete")
        self.assertEqual(stored["exhausted"], 0)
        self.assertEqual(stored["provider_completion_state"], "RETRYABLE")
        self.assertEqual(stored["provider_failure_class"], "TIMEOUT")
        self.assertEqual(stored["provider_requests_submitted"], 5)
        self.assertEqual(json.loads(stored["completion_evidence_json"]), {})
        metadata = json.loads(stored["provider_metadata_json"])
        self.assertEqual(metadata["requests_submitted"], 5)
        self.assertEqual(metadata["task_timeout_seconds"], 1)
        self.assertGreaterEqual(metadata["task_elapsed_seconds"], 1)
        self.assertNotIn("completion_observation", metadata)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM search_task_results WHERE source_job_id='partial-501'").fetchone()[0],
            1,
        )
        conn.close()

    def test_repeated_snapshots_dedupe_requisition_and_preserve_provider_rows(self):
        snapshots = [
            ("shared-301", "provider-row-a"),
            ("shared-301", "provider-row-b"),
            ("distinct-302", "provider-row-c"),
        ]
        for index, (job_id, record_id) in enumerate(snapshots):
            transport = SnapshotSequenceTransport([
                self.provider_row(job_id, record_id, description="Observed enrollment work. " * 20),
            ])
            result = acquire(
                self.bundle, self.provider(transport, run_id=f"brightdata-snapshot-{index}", max_records=1000),
                mode="fast", platforms=["linkedin"],
            )
            self.assertEqual(result["status"], "completed")

        conn = self.database.connect()
        shared = conn.execute(
            "SELECT DISTINCT canonical_job_id FROM search_task_results WHERE source_job_id='shared-301'"
        ).fetchall()
        distinct = conn.execute(
            "SELECT DISTINCT canonical_job_id FROM search_task_results WHERE source_job_id='distinct-302'"
        ).fetchall()
        self.assertEqual(len(shared), 1)
        self.assertEqual(len(distinct), 1)
        self.assertNotEqual(shared[0][0], distinct[0][0])
        provider_rows = conn.execute(
            "SELECT acquisition_provider,provider_record_id FROM search_task_results WHERE source_job_id='shared-301' ORDER BY result_id"
        ).fetchall()
        self.assertEqual({row[0] for row in provider_rows}, {"brightdata-jobs"})
        self.assertEqual({row[1] for row in provider_rows}, {"provider-row-a", "provider-row-b"})
        occurrence = conn.execute(
            "SELECT seen_count,acquisition_provider,provider_record_id FROM source_occurrences WHERE source_job_id='shared-301'"
        ).fetchone()
        self.assertEqual(occurrence["seen_count"], 2)
        self.assertEqual(occurrence["acquisition_provider"], "brightdata-jobs")
        self.assertEqual(occurrence["provider_record_id"], "provider-row-b")
        conn.close()

    def test_validation_budget_checkpoints_every_remaining_task_as_incomplete(self):
        transport = SnapshotSequenceTransport([self.provider_row("budget-401", "budget-row")])
        tasks = compile_plan(self.bundle, "fast", ["linkedin"])
        result = acquire(self.bundle, self.provider(transport, run_id="brightdata-budget-run", max_records=1),
                         mode="fast", platforms=["linkedin"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["task_complete_count"], 0)
        self.assertEqual(result["task_incomplete_count"], len(tasks))
        self.assertEqual(transport.snapshot_number, 1)
        conn = self.database.connect()
        rows = conn.execute(
            "SELECT status,provider_completion_state,provider_failure_class,provider_requests_submitted,provider_metadata_json FROM browser_search_tasks WHERE browser_run_id=?",
            (result["browser_run_id"],),
        ).fetchall()
        self.assertEqual(len(rows), len(tasks))
        self.assertTrue(all(row["status"] == "incomplete" for row in rows))
        self.assertTrue(all(row["provider_completion_state"] == "INCOMPLETE" for row in rows))
        self.assertEqual(sum(row["provider_requests_submitted"] for row in rows), 4)
        self.assertTrue(any(json.loads(row["provider_metadata_json"]).get("budget_stop") for row in rows))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE source_job_id='budget-401'").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
