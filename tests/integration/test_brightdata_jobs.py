from __future__ import annotations

import json
import csv
import copy
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from collections import deque
from pathlib import Path
from typing import Any
from unittest import mock

from jobbot.acquisition.brightdata import (
    BrightDataHTTPResponse, BrightDataJobsProvider, BrightDataRuntimeConfig,
    brightdata_preflight, parse_platform_config,
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


class MockTransport:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, path, *, params, json_body, token):
        self.calls.append({"method": method, "path": path, "params": dict(params),
                           "json_body": json_body, "token": token})
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

    def request(self, method, path, *, params, json_body, token):
        self.calls.append({"method": method, "path": path, "params": dict(params),
                           "json_body": json_body, "token": token})
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
                 retries=2, rows_key=""):
        runtime = BrightDataRuntimeConfig(
            token=SECRET, platforms={platform: platform_config(platform, rows_key=rows_key)},
        )
        return BrightDataJobsProvider(
            runtime, candidate=self.bundle.candidate["candidate"], max_records=max_records,
            transport=transport, max_polls=max_polls, max_transient_retries=retries,
            poll_interval_seconds=0, sleep=lambda _seconds: None,
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
        self.assertNotIn(SECRET, repr(runtime))

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
        self.assertEqual(batch.requests_submitted, 1)
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

        exhausted = MockTransport([
            response(200, {"snapshot_id": "snapshot-build-2"}),
            response(429, {}), response(503, {}), response(500, {}),
        ])
        with self.assertRaises(ProviderFailure) as raised:
            self.provider(platform, exhausted, retries=2).fetch(self.task(platform))
        self.assertEqual(raised.exception.classification, ProviderFailureClass.TRANSPORT)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(exhausted.calls), 4)

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

    def provider(self, transport, *, run_id="brightdata-test-run", max_records=100):
        return BrightDataJobsProvider(
            BrightDataRuntimeConfig(SECRET, {"linkedin": platform_config("linkedin")}),
            candidate=self.bundle.candidate["candidate"], max_records=max_records,
            transport=transport, run_id=run_id, poll_interval_seconds=0,
            sleep=lambda _seconds: None,
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
        self.assertEqual(first_task["provider_requests_submitted"], 1)
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
        self.assertEqual(diagnostic["requests_submitted"], len(compile_plan(self.bundle, "fast", ["linkedin"])))
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
        self.assertEqual(sum(row["provider_requests_submitted"] for row in rows), 1)
        self.assertTrue(any(json.loads(row["provider_metadata_json"]).get("budget_stop") for row in rows))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE source_job_id='budget-401'").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
