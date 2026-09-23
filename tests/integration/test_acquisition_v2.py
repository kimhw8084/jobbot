from __future__ import annotations

import copy
import csv
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobbot import browser_tasks
from jobbot.acquisition.coordinator import acquire
from jobbot.acquisition.models import (
    AcquisitionRecord, ProviderBatch, ProviderCompletionState, ProviderFailure,
    ProviderFailureClass,
)
from jobbot.acquisition.providers import JSONFileProvider, JSONLProvider, ManagedHTTPProvider
from jobbot.bridge import rpc
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.dashboard import live_discoveries, query_jobs, job_detail
from jobbot.db import Database
from jobbot.exports import export_all
from jobbot.search_plan import compile_plan, compile_staged_plan
from jobbot.search_quality import search_quality_metrics
from jobbot.migrations import all_migrations
from jobbot.migrations import m0021_acquisition_v2


DETAIL_TEXT = (
    "This patient enrollment coordinator supports healthcare access operations, documents patient "
    "onboarding, resolves enrollment issues, maintains privacy, and coordinates daily workflows. "
    "Required Qualifications: two years of experience supporting patient access or healthcare "
    "operations. The role works with clinical and operations teams to improve enrollment quality. "
) * 2


class FixtureProvider:
    name = "managed-fixture"
    run_id = "fixture-run-200"

    def __init__(self, records: dict[str, tuple[AcquisitionRecord, ...]] | None = None,
                 states: dict[str, ProviderCompletionState] | None = None,
                 failures: dict[str, ProviderFailureClass] | None = None,
                 proof: bool = True):
        self.records = records or {}
        self.states = states or {}
        self.failures = failures or {}
        self.proof = proof

    def fetch(self, task):
        state = self.states.get(task.task_key, ProviderCompletionState.COMPLETE if self.proof else ProviderCompletionState.INCOMPLETE)
        evidence = {"end_of_results": True, "task_key": task.task_key} if self.proof and state == ProviderCompletionState.COMPLETE else {}
        return ProviderBatch(
            records=self.records.get(task.task_key, ()), completion_state=state,
            completion_evidence=evidence, failure_class=self.failures.get(task.task_key),
            provider_task_id=f"provider-task:{task.task_key}",
        )


class AcquisitionV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
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
        self.previous_rpc_base = rpc.BASE
        rpc.BASE = self.root

    def tearDown(self) -> None:
        rpc.BASE = self.previous_rpc_base
        self.temp.cleanup()

    def selected_task(self):
        return compile_plan(self.bundle, "fast", ["linkedin"])[0]

    def record(self, task_key: str, *, source_job_id: str = "shared-req-41",
               provider_record_id: str = "record-a", description: str = DETAIL_TEXT,
               include_detail: bool = True, location: str = "", salary: str = "",
               apply_url: str = "") -> AcquisitionRecord:
        card = {
            "title": "Patient Enrollment Specialist", "company": "Example Health",
            "location": location, "posted_text": "2 days ago",
        }
        urls = {
            "discovery_url": f"https://www.linkedin.com/jobs/view/{source_job_id}",
            "board_detail_url": f"https://www.linkedin.com/jobs/view/{source_job_id}",
            "employer_job_url": f"https://careers.example.org/jobs/{source_job_id}",
            "ats_requisition_url": f"https://boards.greenhouse.io/example/jobs/{source_job_id}",
        }
        if apply_url:
            urls["observed_board_apply_url"] = apply_url
        detail = None
        if include_detail:
            detail = {
                "source_job_id": source_job_id,
                "canonical_url": urls["board_detail_url"],
                "title": "Patient Enrollment Specialist",
                "company": "Example Health",
                "location": location,
                "salary_text": salary,
                "employment_type": "",
                "remote_status": "unknown",
                "apply_url": apply_url,
                "description": description,
                "verified_application_url": "https://boards.greenhouse.io/example/jobs/fake-verified",
                "source_verification_state": "verified_direct_ats",
                "application_destination_verification_state": "VERIFIED_ATS",
            }
        return AcquisitionRecord(
            source_surface="linkedin", source_job_id=source_job_id, source_urls=urls,
            query_task_key=task_key, provider_record_id=provider_record_id,
            observed_at="2026-09-23T10:00:00+00:00", card=card, detail=detail,
            raw_metadata={"provider_note": "offline fixture", "claim": "observed"},
        )

    def run_fixture_provider(self, provider: FixtureProvider, *, mode: str = "fast"):
        return acquire(self.bundle, provider, mode=mode, platforms=["linkedin"])

    def test_provider_input_and_legacy_rpc_share_canonical_truth_and_versioning(self) -> None:
        task_definition = self.selected_task()
        record = self.record(task_definition.task_key)
        run_id = browser_tasks.enqueue_production(self.root, "fast", ["linkedin"])
        conn = self.database.connect()
        task = conn.execute(
            "SELECT * FROM browser_search_tasks WHERE browser_run_id=? AND task_key=?",
            (run_id, task_definition.task_key),
        ).fetchone()
        task_id = int(task["task_id"])
        card = dict(record.card)
        rpc_card = rpc.handle({
            "action": "record_result", "run_id": run_id, "task_id": task_id,
            "source_site": "linkedin", "source_job_id": record.source_job_id,
            "source_url": record.discovery_url(), "card": card,
            "title_hint": card["title"], "company_hint": card["company"],
            "location_hint": card["location"], "posted_text": card["posted_text"],
        })
        legacy_detail = dict(record.detail or {})
        legacy_detail.pop("verified_application_url", None)
        legacy_detail.pop("source_verification_state", None)
        legacy_detail.pop("application_destination_verification_state", None)
        legacy_detail["search_card"] = card
        legacy_detail["employer_job_url"] = record.source_urls["employer_job_url"]
        legacy_detail["ats_requisition_url"] = record.source_urls["ats_requisition_url"]
        legacy = rpc.handle({
            "action": "record_job", "run_id": run_id, "task_id": task_id,
            "result_id": rpc_card["result_id"], "job": legacy_detail,
        })
        self.assertTrue(legacy["ok"])
        conn.close()

        result = self.run_fixture_provider(FixtureProvider({
            task_definition.task_key: (self.record(task_definition.task_key, provider_record_id="record-b"),)
        }))
        self.assertEqual(result["status"], "completed")
        conn = self.database.connect()
        rows = conn.execute(
            """SELECT r.canonical_job_id,r.source_site,r.source_job_id,r.source_url,r.acquisition_provider,
                      r.provider_record_id,r.query_task_key,r.phase,r.detail_status
               FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id
               WHERE r.source_job_id=? ORDER BY r.result_id""",
            (record.source_job_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["canonical_job_id"], rows[1]["canonical_job_id"])
        self.assertEqual(rows[0]["canonical_job_id"], legacy["job_id"])
        self.assertEqual(rows[1]["source_site"], "linkedin")
        self.assertEqual((rows[1]["acquisition_provider"], rows[1]["provider_record_id"]), ("managed-fixture", "record-b"))
        self.assertEqual(rows[1]["query_task_key"], task_definition.task_key)
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (legacy["job_id"],)).fetchone()
        self.assertEqual(job["title"], "Patient Enrollment Specialist")
        self.assertEqual(job["company"], "Example Health")
        self.assertEqual(job["description"], legacy_detail["description"].strip())
        self.assertEqual(job["source_verification_state"], "assisted_board")
        self.assertNotEqual(job["application_destination_verification_state"], "VERIFIED_ATS")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (legacy["job_id"],)).fetchone()[0], 1)
        occurrence = conn.execute("SELECT * FROM source_occurrences WHERE job_id=?", (legacy["job_id"],)).fetchone()
        self.assertEqual(occurrence["seen_count"], 2)
        self.assertEqual(occurrence["acquisition_provider"], "managed-fixture")
        self.assertEqual(occurrence["provider_record_id"], "record-b")
        self.assertTrue(occurrence["employer_job_url"])
        self.assertTrue(occurrence["ats_requisition_url"])
        conn.close()

    def test_jsonl_card_detail_identity_order_completion_and_provider_outputs(self) -> None:
        task = self.selected_task()
        record = self.record(task.task_key, provider_record_id="jsonl-record-1")
        fixture_path = self.root / "fixture.jsonl"
        fixture_path.write_text(
            json.dumps({
                "type": "record", "query_task_key": task.task_key,
                "source_surface": record.source_surface, "source_job_id": record.source_job_id,
                "source_urls": dict(record.source_urls), "provider_record_id": record.provider_record_id,
                "observed_at": record.observed_at, "card": dict(record.card),
                "detail": dict(record.detail or {}), "raw_metadata": dict(record.raw_metadata),
            }) + "\n" + json.dumps({
                "type": "completion", "query_task_key": task.task_key,
                "completion_evidence": {"cursor": None, "end_of_results": True},
            }) + "\n",
            encoding="utf-8",
        )
        provider = JSONLProvider(fixture_path, run_id="jsonl-fixture-run")
        result = self.run_fixture_provider(provider)
        self.assertEqual(result["status"], "partial")
        conn = self.database.connect()
        row = conn.execute(
            """SELECT r.*,t.status task_status,t.provider_completion_state,t.completion_evidence_json
               FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id
               WHERE r.source_job_id=?""", (record.source_job_id,),
        ).fetchone()
        self.assertEqual(row["identity_status"], "PERSISTED")
        self.assertEqual(row["detail_status"], "COMPLETE")
        self.assertEqual(row["canonical_job_id"], conn.execute(
            "SELECT job_id FROM jobs WHERE job_id=?", (row["canonical_job_id"],)
        ).fetchone()[0])
        self.assertEqual(row["task_status"], "exhausted")
        self.assertEqual(row["provider_completion_state"], "COMPLETE")
        self.assertTrue(json.loads(row["completion_evidence_json"]))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (row["canonical_job_id"],)).fetchone()[0], 1)
        event_ids = {
            item["event_type"]: item["event_id"]
            for item in conn.execute(
                """SELECT event_id,event_type FROM browser_events
                   WHERE browser_run_id=? AND task_id=? AND event_type IN ('result_discovered','job_recorded')""",
                (result["browser_run_id"], row["task_id"]),
            )
        }
        self.assertLess(event_ids["result_discovered"], event_ids["job_recorded"])
        dashboard = live_discoveries(conn)
        discovery = next(item for item in dashboard["discoveries"] if item["provider_record_id"] == "jsonl-record-1")
        self.assertEqual(discovery["acquisition_provider"], "jsonl-file")
        self.assertEqual(discovery["query_task_key"], task.task_key)
        jobs = query_jobs(conn, {})
        self.assertIn("jsonl-file", jobs["jobs"][0]["acquisition_providers"])
        details = job_detail(conn, row["canonical_job_id"])
        self.assertEqual(details["occurrences"][0]["acquisition_provider"], "jsonl-file")
        self.assertTrue(details["occurrences"][0]["employer_job_url"])
        self.assertTrue(details["occurrences"][0]["ats_requisition_url"])
        self.assertEqual(details["occurrences"][0]["verified_application_url"], "")
        metrics = search_quality_metrics(conn)
        diagnostic = next(item for item in metrics["provider_metrics"] if item["acquisition_provider"] == "jsonl-file")
        self.assertGreaterEqual(diagnostic["card_count"], 1)
        self.assertIn("jsonl-file", {item["acquisition_provider"] for item in metrics["source_occurrence_metrics"]})
        paths = export_all(conn, self.bundle.output_dir)
        with paths["all_jobs.csv"].open(encoding="utf-8-sig", newline="") as handle:
            exported = next(csv.DictReader(handle))
        self.assertIn("jsonl-file", exported["acquisition_providers"])
        conn.close()

    def test_absent_completion_proof_partial_detail_and_unknown_fields_fail_closed(self) -> None:
        task = self.selected_task()
        partial = self.record(
            task.task_key, source_job_id="partial-req-21", provider_record_id="partial-record",
            description="", include_detail=True,
        )
        no_proof = FixtureProvider(
            {task.task_key: (partial,)},
            states={task.task_key: ProviderCompletionState.INCOMPLETE},
            proof=False,
        )
        result = self.run_fixture_provider(no_proof)
        self.assertEqual(result["status"], "partial")
        conn = self.database.connect()
        task_row = conn.execute(
            "SELECT status,provider_completion_state,provider_failure_class FROM browser_search_tasks WHERE task_key=? ORDER BY task_id DESC LIMIT 1",
            (task.task_key,),
        ).fetchone()
        self.assertEqual(task_row["status"], "incomplete")
        self.assertEqual(task_row["provider_completion_state"], "INCOMPLETE")
        self.assertEqual(task_row["provider_failure_class"], "PARTIAL_BATCH")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE source_job_id='partial-req-21'").fetchone()[0], 1)
        result_row = conn.execute("SELECT * FROM search_task_results WHERE source_job_id='partial-req-21'").fetchone()
        self.assertEqual(result_row["detail_status"], "RETRYABLE")
        self.assertEqual(result_row["evidence_readiness_state"], "REVIEW")
        self.assertEqual(result_row["verified_application_url"], "")
        self.assertNotEqual(result_row["application_destination_verification_state"], "VERIFIED_ATS")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        conn.close()

        observed = self.record(
            task.task_key, source_job_id="observed-req-22", provider_record_id="observed-record",
            location="", salary="", apply_url="",
        )
        self.run_fixture_provider(FixtureProvider({task.task_key: (observed,)}))
        conn = self.database.connect()
        job = conn.execute("SELECT * FROM jobs WHERE canonical_source_site='linkedin' AND title='Patient Enrollment Specialist'").fetchone()
        self.assertEqual(job["location_raw"], "")
        self.assertEqual(job["salary_text"], "")
        self.assertEqual(job["apply_url"], "")
        self.assertEqual(job["location_evidence_state"], "UNKNOWN")
        self.assertEqual(job["remote_evidence_state"], "UNKNOWN")
        self.assertNotEqual(job["evidence_readiness_state"], "READY")
        self.assertNotIn(job["recommendation"], {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})
        conn.close()

    def test_same_requisition_from_two_providers_reconciles_and_distinct_requisitions_stay_distinct(self) -> None:
        task = self.selected_task()
        shared = self.record(task.task_key, provider_record_id="shared-provider-a")
        self.run_fixture_provider(FixtureProvider({task.task_key: (shared,)}))
        provider_b = FixtureProvider({
            task.task_key: (self.record(task.task_key, provider_record_id="shared-provider-b"),)
        })
        provider_b.name = "managed-fixture-b"
        provider_b.run_id = "fixture-run-b"
        self.run_fixture_provider(provider_b)
        distinct = self.record(task.task_key, source_job_id="different-req-42", provider_record_id="distinct-provider-c")
        provider_c = FixtureProvider({task.task_key: (distinct,)})
        provider_c.name = "managed-fixture-c"
        provider_c.run_id = "fixture-run-c"
        self.run_fixture_provider(provider_c)
        conn = self.database.connect()
        shared_ids = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT canonical_job_id FROM search_task_results WHERE source_job_id='shared-req-41'"
            )
        }
        distinct_ids = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT canonical_job_id FROM search_task_results WHERE source_job_id='different-req-42'"
            )
        }
        self.assertEqual(len(shared_ids), 1)
        self.assertEqual(len(distinct_ids), 1)
        self.assertNotEqual(next(iter(shared_ids)), next(iter(distinct_ids)))
        providers = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT acquisition_provider FROM search_task_results WHERE source_job_id='shared-req-41'"
            )
        }
        self.assertEqual(providers, {"managed-fixture", "managed-fixture-b"})
        self.assertEqual(conn.execute("SELECT seen_count FROM source_occurrences WHERE source_job_id='shared-req-41'").fetchone()[0], 2)
        conn.close()

    def test_timeout_preserves_committed_sighting_and_managed_http_uses_injected_transport(self) -> None:
        task = self.selected_task()
        record = self.record(task.task_key, source_job_id="timeout-req-43", provider_record_id="timeout-record")
        timeout_provider = FixtureProvider(
            {task.task_key: (record,)},
            states={task.task_key: ProviderCompletionState.RETRYABLE},
            failures={task.task_key: ProviderFailureClass.TIMEOUT},
        )
        result = self.run_fixture_provider(timeout_provider)
        self.assertEqual(result["status"], "partial")
        conn = self.database.connect()
        task_row = conn.execute(
            "SELECT status,provider_completion_state,provider_failure_class FROM browser_search_tasks WHERE task_key=? ORDER BY task_id DESC LIMIT 1",
            (task.task_key,),
        ).fetchone()
        self.assertEqual(tuple(task_row), ("incomplete", "RETRYABLE", "TIMEOUT"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_task_results WHERE source_job_id='timeout-req-43'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE title='Patient Enrollment Specialist'").fetchone()[0], 1)
        conn.close()

        calls = []
        http = ManagedHTTPProvider(
            "https://provider.invalid/jobs",
            lambda endpoint, payload: calls.append((endpoint, payload["task_key"])) or {
                "complete": True, "provider_task_id": "http-task",
                "completion_evidence": {"next_cursor": None, "end_of_results": True},
                "records": [],
            },
            run_id="mock-http-run",
        )
        batch = http.fetch(task)
        self.assertTrue(batch.proven_complete)
        self.assertEqual(calls, [("https://provider.invalid/jobs", task.task_key)])
        json_path = self.root / "fixture.json"
        json_path.write_text(json.dumps({
            "records": [],
            "completions": [{"query_task_key": task.task_key, "completion_evidence": {"end_of_results": True}}],
        }), encoding="utf-8")
        json_provider = JSONFileProvider(json_path)
        self.assertTrue(json_provider.fetch(task).proven_complete)
        broken = ManagedHTTPProvider("https://unused.invalid", lambda *_: (_ for _ in ()).throw(TimeoutError("mock timeout")), run_id="mock-timeout")
        with self.assertRaises(ProviderFailure) as raised:
            broken.fetch(task)
        self.assertEqual(raised.exception.classification, ProviderFailureClass.TIMEOUT)

    def test_staged_acquisition_plan_retains_exact_universe_and_deep_pairs(self) -> None:
        tasks = compile_staged_plan(self.bundle, ["linkedin", "indeed", "glassdoor"])
        self.assertEqual(len(tasks), 140)
        required = {
            (str(family["id"]), platform)
            for family in self.bundle.live_search["families"]
            if family.get("enabled", True) and family.get("minimum_deep_recall", False)
            for platform in ("linkedin", "indeed", "glassdoor")
        }
        deep_pairs = {(task.query_family, task.platform) for task in tasks if task.phase == "C_DEEP_BACKFILL"}
        self.assertEqual(len(required), 21)
        self.assertTrue(required <= deep_pairs)
        self.assertEqual(tasks, compile_staged_plan(self.bundle, ["linkedin", "indeed", "glassdoor"]))

    def test_migration_backfills_history_as_legacy_browser_unknown(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for migration in all_migrations()[:-1]:
            migration.upgrade(conn)
        now = "2026-09-01T00:00:00+00:00"
        run_id = int(conn.execute(
            "INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES('3.2.1','deep','linkedin','completed',?)",
            (now,),
        ).lastrowid)
        task_id = int(conn.execute(
            """INSERT INTO browser_search_tasks(browser_run_id,task_key,platform,query_text,window_days,search_url,status,created_at,phase)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (run_id, "historical-task", "linkedin", "patient enrollment", 30, "https://example.test", "exhausted", now, "C_DEEP_BACKFILL"),
        ).lastrowid)
        conn.execute(
            """INSERT INTO search_task_results(task_id,browser_run_id,source_site,source_job_id,source_url,canonical_job_id,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (task_id, run_id, "linkedin", "historical-job", "https://example.test/job", "historical-job", now, now),
        )
        conn.execute(
            """INSERT INTO source_occurrences(occurrence_key,job_id,source_site,source_job_id,source_url,first_seen,last_seen)
               VALUES('historical-occurrence','historical-job','linkedin','historical-job','https://example.test/job',?,?)""",
            (now, now),
        )
        m0021_acquisition_v2.upgrade(conn)
        run = conn.execute("SELECT acquisition_provider,acquisition_mode,provider_run_id FROM browser_runs").fetchone()
        task = conn.execute("SELECT acquisition_provider,acquisition_mode,provider_run_id,query_task_key FROM browser_search_tasks").fetchone()
        result = conn.execute("SELECT acquisition_provider,acquisition_mode,provider_record_id,provider_metadata_json,query_task_key FROM search_task_results").fetchone()
        occurrence = conn.execute("SELECT acquisition_provider,acquisition_mode,provider_record_id,provider_metadata_json,query_task_key,phase FROM source_occurrences").fetchone()
        self.assertEqual(tuple(run), ("legacy-browser", "unknown", ""))
        self.assertEqual(tuple(task), ("legacy-browser", "unknown", "", "historical-task"))
        self.assertEqual(tuple(result), ("legacy-browser", "unknown", "", "{}", "historical-task"))
        self.assertEqual(tuple(occurrence), ("legacy-browser", "unknown", "", "{}", "historical-task", "C_DEEP_BACKFILL"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
