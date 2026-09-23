from __future__ import annotations

import copy
import json
import shutil
import threading
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from jobbot import browser_tasks
from jobbot.bridge import rpc
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.dashboard import create_server, job_detail, query_jobs
from jobbot.db import Database
from jobbot.exports import export_selected
from jobbot.application import mark
from jobbot.legacy_engine import PrecisionStore, job_from_row
from jobbot.provenance import field_provenance_summary
from jobbot.qualified_yield import REQUIRED_SUBSTANTIVE_GATES
from jobbot.search_ordering import OrderingConfig, _history, order_tasks
from jobbot.search_plan import compile_staged_plan
from jobbot.search_quality import search_quality_metrics
from jobbot.scoring import Job, score_job
from jobbot.siblings import probable_sibling_clusters


NOW = "2026-09-21T12:00:00+00:00"


class Chg114SearchQualityTests(unittest.TestCase):
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
        self.bundle = ConfigBundle(self.root, original.strategy, original.candidate, runtime, original.live_search)
        self.database = Database(self.bundle)
        self.database.migrate()
        self.conn = self.database.connect()

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def run_row(self, mode: str = "fixture") -> int:
        return int(self.conn.execute(
            "INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES('3.2.1',?,?,?,?)",
            (mode, "linkedin", "completed", NOW),
        ).lastrowid)

    def task(self, run_id: int, query: str, *, platform: str = "linkedin", family: str = "access", kind: str = "intent", query_pass: str = "linkedin_intent", phase: str = "C_DEEP_BACKFILL", rank: int = 10, status: str = "exhausted", task_key: str | None = None, pages: int = 0, details: int = 0) -> int:
        query_task_key = task_key or f"key-{platform}-{query}-{phase}"
        return int(self.conn.execute("""INSERT INTO browser_search_tasks(
          browser_run_id,task_key,platform,query_text,window_days,search_url,status,created_at,started_at,completed_at,
          execution_rank,priority,baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,
          ordering_algorithm_version,pages_visited,detail_count_read,scroll_generation,results_seen,cards_extracted,
          cards_persistence_succeeded,duplicate_cards,strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,phase
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            run_id, query_task_key, platform, query, 30, f"https://{platform}.example/?q={query}", status,
            NOW, "2026-09-21T11:59:00+00:00", NOW, rank, 1, rank, rank,
            "baseline fixture", 0, "chg114-yield-v1", pages, details, 0, 0, 0, 0, 0,
            "jobbot-broad-qualified-yield", "1.0", family, kind, query_pass, 1, phase,
        )).lastrowid)

    def job(self, job_id: str, *, ready: bool = True, company: str = "Example Health", title: str = "Patient Access Specialist", source_state: str = "verified_direct_ats", destination_state: str = "VERIFIED_ATS", recommendation: str | None = None, app_status: str = "NEW", qualification_gates: dict | None = None, posted_at: str = "2026-09-20T12:00:00+00:00", ats_url: str | None = None, identity_state: str = "COMPLETE", description: str = "Patient access enrollment, healthcare operations, documentation quality, and patient onboarding workflows.", hard_rejects: list[str] | None = None) -> None:
        readiness = "READY" if ready else "REVIEW"
        rec = recommendation or ("APPLY_NOW" if ready else "REVIEW")
        gates = {name: {"status": "pass" if ready else "review", "evidence": "isolated fixture"} for name in REQUIRED_SUBSTANTIVE_GATES}
        gates.update(qualification_gates or {})
        gates.setdefault("no_repeat", {"status": "pass" if app_status == "NEW" else "fail", "evidence": "fixture handling status"})
        rejects = hard_rejects if hard_rejects is not None else (["fixture hard reject"] if rec == "SKIP_HARD_GATE" else [])
        self.conn.execute("""INSERT INTO jobs(
          job_id,title,company,location_raw,remote_status,employment_type,employment_class,salary_text,posted_at,description,first_seen,last_seen,
          remote_gate,recommendation,application_status,is_active,canonical_url,apply_url,canonical_source_site,
          evidence_readiness_state,qualification_readiness_state,identity_evidence_state,detail_evidence_state,
          requirements_evidence_state,source_verification,source_verification_state,application_destination_verification_state,
          verified_application_url,ats_requisition_url,discovery_url,board_detail_url,employer_job_url,location_evidence_state,
          remote_evidence_state,apply_destination_state,evidence_provenance_json,hard_reject_reasons_json,qualification_gates_json,posting_status,evidence_readiness_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            job_id, title, company, "Remote — United States", "remote", "Full-time permanent", "full_time_permanent" if ready else "unknown", "$60,000/year", posted_at,
            description, NOW, NOW, "pass" if ready else "review", rec, app_status, 1, f"https://boards.greenhouse.io/example/jobs/{job_id}",
            f"https://boards.greenhouse.io/example/jobs/{job_id}", "greenhouse", readiness, readiness,
            identity_state, "COMPLETE", "SUPPORTED" if ready else "UNRESOLVED", source_state, source_state,
            destination_state, f"https://boards.greenhouse.io/example/jobs/{job_id}" if destination_state == "VERIFIED_ATS" else "",
            ats_url or f"https://boards.greenhouse.io/example/jobs/{job_id}", f"https://www.linkedin.com/jobs/view/{job_id}",
            "https://www.linkedin.com/jobs/view/detail", "", "OBSERVED", "OBSERVED", "OBSERVED",
            json.dumps({"location": "observed", "remote": "observed_detail_text", "salary": "observed_detail_text", "employment_type": "observed_detail_text", "posted_at": "observed_search_card_or_detail", "source_type": "board_detail", "posted_at_source_type": "search_card"}),
            json.dumps(rejects), json.dumps(gates), "active" if ready else "unknown", json.dumps({"recommendation": rec, "intrinsic_recommendation": rec}),
        ))

    def result(self, task_id: int, source_id: str, job_id: str | None, *, source_site: str = "linkedin", url: str | None = None, sightings: int = 1, status: str = "COMPLETE", attempts: int = 1, selected: int = 1, qa: int = 0, content: str = "COMPLETE", first_seen: str = NOW) -> int:
        source_url = url or f"https://{source_site}.example/jobs/{source_id}"
        return int(self.conn.execute("""INSERT INTO search_task_results(
          task_id,source_site,source_job_id,source_url,canonical_job_id,first_seen_at,last_seen_at,sighting_count,
          detail_read,detail_status,detail_attempts,content_state,recall_selected,recall_qa_sample,
          strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,evidence_readiness_state
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            task_id, source_site, source_id, source_url, job_id, first_seen, NOW, sightings,
            int(status == "COMPLETE"), status, attempts, content, selected, qa,
            "jobbot-broad-qualified-yield", "1.0", "access", "intent", "linkedin_intent", 1, "READY" if job_id else "REVIEW",
        )).lastrowid)

    def history(self, query: str, *, n: int, qualified: int, duplicates: int = 0, platform: str = "linkedin", family: str = "access", phase: str = "C_DEEP_BACKFILL", rank: int = 10) -> None:
        run_id = self.run_row("history")
        task_id = self.task(run_id, query, platform=platform, family=family, phase=phase, rank=rank, pages=2, details=n)
        for index in range(n):
            job_id = f"hist-{run_id}-{query}-{index}"
            positive = index < qualified
            self.job(job_id, ready=positive, source_state="verified_direct_ats" if positive else "unverified_discovery", destination_state="VERIFIED_ATS" if positive else "MISSING")
            self.result(task_id, f"{query}-{index}", job_id, source_site=platform, selected=int(index % 2 == 0), qa=int(index % 3 == 0))
        for index in range(duplicates):
            job_id = f"hist-{run_id}-{query}-{index % max(1, n)}"
            self.result(task_id, f"{query}-duplicate-{index}", job_id, source_site=platform,
                        url=f"https://{platform}.example/jobs/{query}/duplicate/{index}", sightings=2)

    @staticmethod
    def current_task(query: str, rank: int, *, platform: str = "linkedin", family: str = "access", phase: str = "C_DEEP_BACKFILL", task_key: str | None = None) -> dict:
        return {
            "task_key": task_key or f"key-{platform}-{query}-{phase}", "strategy_profile": "jobbot-broad-qualified-yield",
            "strategy_profile_version": "1.0", "query_family": family, "query_kind": "intent",
            "query_pass": "linkedin_intent", "platform": platform, "query_text": query,
            "window_days": 30, "phase": phase, "priority": 1, "execution_rank": rank,
        }

    def test_telemetry_occurrence_canonical_readiness_cost_and_funnel_attribution(self) -> None:
        run_id = self.run_row()
        t1 = self.task(run_id, "query A", rank=10, pages=2, details=3)
        t2 = self.task(run_id, "query B", kind="title_family", query_pass="linkedin_title_family", rank=20, status="challenged", pages=1, details=1)
        t3 = self.task(run_id, "other platform", platform="indeed", rank=10, pages=1, details=1)
        t4 = self.task(run_id, "same family later phase", phase="A_FASTEST_DOOR_RECENT", rank=99, pages=1, details=1)
        self.conn.execute("UPDATE browser_search_tasks SET started_at='2026-09-21T11:59:00+00:00',completed_at='2026-09-21T12:00:00+00:00',scroll_generation=3,results_seen=4,cards_extracted=4,cards_persistence_succeeded=3,duplicate_cards=1 WHERE task_id=?", (t1,))
        self.job("j1", ready=True)
        self.job("j2", ready=False, source_state="unverified_discovery", destination_state="MISSING", recommendation="SKIP_HARD_GATE")
        self.job("j3", ready=False, source_state="unverified_discovery", destination_state="MISSING")
        self.job("j4", ready=True, app_status="APPLIED", qualification_gates={"no_repeat": {"status": "fail"}})
        self.conn.execute("""INSERT INTO source_occurrences(
          occurrence_key,job_id,source_site,source_job_id,source_url,first_seen,last_seen,
          strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "occ-j1", "j1", "linkedin", "card-a", "https://linkedin.example/jobs/card-a", NOW, NOW,
            "jobbot-broad-qualified-yield", "1.0", "access", "intent", "linkedin_intent", 1,
        ))
        self.result(t1, "card-a", "j1", sightings=2, attempts=1, selected=1)
        self.result(t1, "card-b", "j1", url="https://linkedin.example/jobs/card-b", attempts=2, qa=1)
        self.result(t1, "card-c", "j2", status="PARTIAL", content="PARTIAL", attempts=1)
        self.result(t2, "card-a-on-b", "j1")
        self.result(t2, "card-c-on-b", "j3", status="FAILED", content="MISSING", attempts=1)
        self.result(t3, "indeed-a", "j4", source_site="indeed")
        self.result(t4, "card-a-later-phase", "j1")
        for stage in ("APPLIED", "SCREEN", "INTERVIEW", "OFFER"):
            self.conn.execute("INSERT INTO application_events(job_id,event_type,event_at,source) VALUES(?,?,?,?)", ("j1", stage, NOW, "fixture"))
        self.conn.execute("INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)", (run_id, t2, NOW, "challenge_seen", "fixture", "{}"))

        metrics = search_quality_metrics(self.conn)
        a = next(row for row in metrics["task_metrics"] if row["query"] == "query A")
        b = next(row for row in metrics["task_metrics"] if row["query"] == "query B")
        linkedin = next(row for row in metrics["family_metrics"] if row["platform"] == "linkedin" and row["phase"] == "C_DEEP_BACKFILL")
        later_linkedin = next(row for row in metrics["family_metrics"] if row["platform"] == "linkedin" and row["phase"] == "A_FASTEST_DOOR_RECENT")
        indeed = next(row for row in metrics["family_metrics"] if row["platform"] == "indeed")
        source_occurrence = next(row for row in metrics["source_occurrence_metrics"] if row["platform"] == "linkedin")
        self.assertEqual((a["cards_persisted"], a["unique_source_ids"], a["unique_canonical_jobs"]), (3, 3, 2))
        self.assertEqual(a["duplicate_sighting_ratio"], 0.25)
        self.assertEqual((a["detail_completions"], a["detail_partial"]), (2, 1))
        self.assertAlmostEqual(a["detail_completion_rate"], 2 / 3, places=6)
        self.assertEqual((a["recall_selected"], a["recall_qa_samples"]), (3, 1))
        self.assertEqual((a["evidence_ready_count"], a["qualification_ready_count"], a["actionable_count"]), (1, 1, 1))
        self.assertEqual((a["source_verified_count"], a["application_destination_verified_count"]), (1, 1))
        self.assertEqual((a["hard_reject_count"], a["review_count"], a["actionable_hard_reject_leakage_count"]), (1, 1, 0))
        self.assertEqual((a["pages_visited"], a["scroll_generations"], a["detail_reads"], a["task_elapsed_seconds"]), (2, 3, 3, 60.0))
        self.assertEqual(a["cost_per_actionable"], 5.0)
        self.assertEqual(a["application_count"], 1)
        self.assertEqual((a["screen_count"], a["interview_count"], a["offer_count"]), (1, 1, 1))
        self.assertEqual((b["application_count"], b["challenges"], b["detail_failures"]), (0, 1, 1))
        self.assertEqual(linkedin["unique_canonical_jobs"], 3)
        self.assertEqual(linkedin["multi_query_canonical_jobs"], 1)
        self.assertEqual(linkedin["application_count"], 1)
        self.assertEqual(later_linkedin["application_count"], 0)
        self.assertEqual(indeed["application_count"], 0)
        self.assertEqual(indeed["no_repeat_leakage_count"], 1)
        self.assertEqual((source_occurrence["source_occurrence_rows"], source_occurrence["unique_source_ids"], source_occurrence["unique_canonical_jobs"]), (1, 1, 1))
        self.assertEqual(metrics["metric_definition_version"], "chg114-search-quality-v2")

    def test_handled_success_keeps_trusted_yield_and_preserves_funnel_attribution(self) -> None:
        run_id = self.run_row()
        first_task = self.task(run_id, "handled query A", rank=10, details=1)
        later_task = self.task(run_id, "handled query B", rank=20, details=1)
        self.job("handled-success", ready=True)
        self.result(first_task, "handled-source-a", "handled-success", first_seen=NOW)
        self.result(later_task, "handled-source-b", "handled-success", first_seen="2026-09-22T12:00:00+00:00")
        self.conn.commit()
        current = self.current_task("handled query A", 10)

        before = search_quality_metrics(self.conn)
        before_a = next(row for row in before["task_metrics"] if row["query"] == "handled query A")
        self.assertEqual(_history(self.conn, current, OrderingConfig())["trusted_qualified_yield_jobs"], 1)
        self.assertEqual((before_a["current_actionable_count"], before_a["trusted_qualified_yield_count"]), (1, 1))

        mark(self.conn, "handled-success", "APPLIED", source="chg114-regression")
        row = self.conn.execute("SELECT recommendation,qualification_gates_json FROM jobs WHERE job_id='handled-success'").fetchone()
        self.assertEqual(row["recommendation"], "ALREADY_HANDLED")
        self.assertEqual(json.loads(row["qualification_gates_json"])["no_repeat"]["status"], "fail")
        metrics = search_quality_metrics(self.conn)
        query_a = next(item for item in metrics["task_metrics"] if item["query"] == "handled query A")
        query_b = next(item for item in metrics["task_metrics"] if item["query"] == "handled query B")
        self.assertEqual((query_a["current_actionable_count"], query_a["trusted_qualified_yield_count"], query_a["application_count"]), (0, 1, 1))
        self.assertEqual(_history(self.conn, current, OrderingConfig())["trusted_qualified_yield_jobs"], 1)
        self.assertEqual(query_b["application_count"], 0)

        for stage, metric_key in (("SCREEN", "screen_count"), ("INTERVIEW", "interview_count"), ("OFFER", "offer_count")):
            mark(self.conn, "handled-success", stage, source="chg114-regression")
            metrics = search_quality_metrics(self.conn)
            query_a = next(item for item in metrics["task_metrics"] if item["query"] == "handled query A")
            query_b = next(item for item in metrics["task_metrics"] if item["query"] == "handled query B")
            self.assertEqual(query_a["trusted_qualified_yield_count"], 1)
            self.assertEqual(query_a["current_actionable_count"], 0)
            self.assertEqual(query_a[metric_key], 1)
            self.assertEqual(query_b["application_count"], 0)
            self.assertEqual(_history(self.conn, current, OrderingConfig())["trusted_qualified_yield_jobs"], 1)
        family = next(item for item in metrics["family_metrics"] if item["platform"] == "linkedin" and item["phase"] == "C_DEEP_BACKFILL")
        self.assertEqual((family["application_count"], family["screen_count"], family["interview_count"], family["offer_count"]), (1, 1, 1, 1))

    def test_handled_review_hard_fail_and_incomplete_jobs_never_become_yield(self) -> None:
        run_id = self.run_row()
        task_id = self.task(run_id, "manual handling query", details=3)
        self.job("manual-review", ready=True, recommendation="REVIEW")
        self.job(
            "manual-hard-fail", ready=True, recommendation="SKIP_HARD_GATE",
            qualification_gates={"base_pay_floor": {"status": "fail", "evidence": "below configured floor"}},
            hard_rejects=["employer base pay is below the configured floor"],
        )
        self.job("manual-incomplete", ready=False, recommendation="REVIEW", source_state="unverified_discovery", destination_state="MISSING", identity_state="PARTIAL")
        for job_id in ("manual-review", "manual-hard-fail", "manual-incomplete"):
            self.result(task_id, f"source-{job_id}", job_id)
        self.conn.commit()
        for job_id in ("manual-review", "manual-hard-fail", "manual-incomplete"):
            mark(self.conn, job_id, "APPLIED", source="manual-entry")

        metrics = search_quality_metrics(self.conn)
        query = next(item for item in metrics["task_metrics"] if item["query"] == "manual handling query")
        self.assertEqual(query["application_count"], 3)
        self.assertEqual(query["current_actionable_count"], 0)
        self.assertEqual(query["trusted_qualified_yield_count"], 0)
        for job_id in ("manual-review", "manual-hard-fail", "manual-incomplete"):
            row = self.conn.execute("SELECT recommendation,qualification_gates_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(row["recommendation"], "ALREADY_HANDLED")
            self.assertEqual(json.loads(row["qualification_gates_json"])["no_repeat"]["status"], "fail")

    def test_trusted_source_rescore_revokes_handled_qualified_yield_on_hard_gate_failure(self) -> None:
        description = (
            "Example Health is currently accepting applications for a fully remote United States role. "
            "This is a full-time permanent employee position. The employer provides a $60,000 annual base salary. "
            "No travel required. No required office days. No onsite training. No field work. No in-person events. "
            "Healthcare patient enrollment operations include reviewing enrollment records, verifying documentation, "
            "resolving discrepancies, coordinating workflow handoffs, and preparing accurate case records. "
            "Required Qualifications: 2 years of healthcare enrollment or relevant operations experience. "
            "HIPAA documentation and Excel workflows."
        )
        job = Job(
            source_site="greenhouse", source_job_id="job-12345",
            canonical_url="https://boards.greenhouse.io/example/jobs/12345",
            apply_url="https://boards.greenhouse.io/example/jobs/12345",
            title="Patient Enrollment Specialist", company="Example Health",
            location_raw="Remote — United States", remote_status="remote",
            employment_type="Full-time permanent", salary_text="$60,000/year",
            salary_min=60000, salary_max=60000, salary_currency="USD", salary_period="year",
            posted_at="2026-09-20T12:00:00+00:00", description=description,
            raw={"posting_status": "active"},
        )
        job._mode = "deep"
        score_job(job, self.bundle.strategy, self.bundle.legacy_runtime()["candidate"])
        self.assertIn(job.recommendation, {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}, job.qualification_gates)
        self.assertEqual(job.evidence_readiness_state, "READY")
        self.assertIn(job.evidence_readiness["intrinsic_recommendation"], {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})

        store = PrecisionStore(self.root / "data" / "jobs.sqlite3")
        try:
            job_id = job.job_id
            store.upsert(job)
            run_id = self.run_row()
            task_id = self.task(run_id, "source refresh query", details=1)
            self.result(task_id, "greenhouse-12345", job_id)
            self.conn.commit()
            mark(self.conn, job_id, "APPLIED", source="chg114-regression")
            before = search_quality_metrics(self.conn)
            before_query = next(item for item in before["task_metrics"] if item["query"] == "source refresh query")
            self.assertEqual(before_query["trusted_qualified_yield_count"], 1)

            existing = store.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            changed = job_from_row(existing)
            changed.source_job_id = "job-12345"
            changed.employment_type = "Part-time"
            changed.description = description.replace("full-time permanent", "part-time")
            changed._mode = "deep"
            score_job(changed, self.bundle.strategy, self.bundle.legacy_runtime()["candidate"])
            self.assertEqual(changed.recommendation, "ALREADY_HANDLED")
            self.assertEqual(changed.qualification_gates["full_time"]["status"], "fail")
            self.assertEqual(changed.evidence_readiness_state, "READY")
            self.assertEqual(changed.qualification_readiness_state, "BLOCKED")
            self.assertEqual(changed.evidence_readiness["intrinsic_recommendation"], "PART_TIME_REVIEW")
            store.upsert(changed)

            after = search_quality_metrics(self.conn)
            after_query = next(item for item in after["task_metrics"] if item["query"] == "source refresh query")
            self.assertEqual(after_query["current_actionable_count"], 0)
            self.assertEqual(after_query["trusted_qualified_yield_count"], 0)
            self.assertEqual(_history(self.conn, self.current_task("source refresh query", 10), OrderingConfig())["trusted_qualified_yield_jobs"], 0)
        finally:
            store.close()

    def test_insufficient_and_overlapping_history_preserve_baseline_order(self) -> None:
        self.history("query A", n=3, qualified=3)
        self.history("query B", n=3, qualified=0, rank=20)
        tasks = [self.current_task("query A", 10), self.current_task("query B", 20)]
        ordered, _ = order_tasks(self.conn, tasks, OrderingConfig(min_evaluated_jobs=8))
        self.assertEqual([item["query_text"] for item in sorted(ordered, key=lambda item: item["effective_execution_rank"])], ["query A", "query B"])
        self.assertTrue(all(item["effective_execution_rank"] == item["baseline_execution_rank"] for item in ordered))

        self.history("query A", n=8, qualified=4)
        self.history("query B", n=8, qualified=4, rank=20)
        overlap_order, metadata = order_tasks(self.conn, tasks, OrderingConfig(min_evaluated_jobs=8))
        self.assertFalse(metadata["peer_groups"]["C_DEEP_BACKFILL/linkedin"]["learned"])
        self.assertEqual([item["query_text"] for item in sorted(overlap_order, key=lambda item: item["effective_execution_rank"])], ["query A", "query B"])

    def test_strong_ready_history_reorders_without_card_volume_bias(self) -> None:
        self.history("query A", n=8, qualified=0, duplicates=40, rank=10)
        self.history("query B", n=8, qualified=8, rank=20)
        tasks = [self.current_task("query A", 10), self.current_task("query B", 20)]
        ordered, metadata = order_tasks(self.conn, tasks, OrderingConfig(min_evaluated_jobs=8))
        self.assertTrue(metadata["peer_groups"]["C_DEEP_BACKFILL/linkedin"]["learned"])
        self.assertEqual([item["query_text"] for item in sorted(ordered, key=lambda item: item["effective_execution_rank"])], ["query B", "query A"])
        self.assertEqual(len(ordered), len(tasks))
        self.assertTrue(all(item["learned_order_sample_size"] == 8 for item in ordered))
        metrics = search_quality_metrics(self.conn)
        raw_a = next(row for row in metrics["task_metrics"] if row["query"] == "query A")
        raw_b = next(row for row in metrics["task_metrics"] if row["query"] == "query B")
        self.assertGreater(raw_a["cards_persisted"], raw_b["cards_persisted"])
        self.assertGreater(raw_a["duplicate_sighting_ratio"], raw_b["duplicate_sighting_ratio"])
        self.assertEqual(raw_a["actionable_count"], 0)
        self.assertEqual(raw_b["actionable_count"], 8)

    def test_plan_universe_and_recall_coverage_are_unchanged_by_ordering(self) -> None:
        bundle = load_bundle(PROJECT_ROOT)
        baseline = compile_staged_plan(bundle, ["linkedin", "indeed", "glassdoor"])
        candidate, _ = order_tasks(self.conn, [{
            "task_key": task.task_key, "strategy_profile": task.strategy_profile,
            "strategy_profile_version": task.strategy_profile_version, "query_family": task.query_family,
            "query_kind": task.query_kind, "query_pass": task.query_pass, "platform": task.platform,
            "query_text": task.query, "window_days": task.age_days, "phase": task.phase,
            "priority": task.priority, "execution_rank": task.execution_rank,
        } for task in baseline], OrderingConfig())
        self.assertEqual(len(baseline), 140)
        self.assertEqual(len(candidate), len(baseline))
        identity = lambda task: (
            task["platform"], task["query_text"].casefold(), task["window_days"], task["phase"], task["query_family"], task["query_kind"], task["query_pass"]
        ) if isinstance(task, dict) else (
            task.platform, task.query.casefold(), task.age_days, task.phase, task.query_family, task.query_kind, task.query_pass
        )
        self.assertEqual({identity(task) for task in baseline}, {identity(task) for task in candidate})
        enabled = {(str(f["id"]), platform) for f in bundle.live_search["families"] if f.get("enabled", True) and f.get("minimum_deep_recall", False) for platform in ("linkedin", "indeed", "glassdoor")}
        before_deep = {(task.query_family, task.platform) for task in baseline if task.phase == "C_DEEP_BACKFILL"}
        after_deep = {(task["query_family"], task["platform"]) for task in candidate if task["phase"] == "C_DEEP_BACKFILL"}
        self.assertTrue(enabled <= before_deep)
        self.assertEqual(before_deep, after_deep)
        self.assertTrue(all(item["effective_execution_rank"] == item["baseline_execution_rank"] for item in candidate))

    def test_siblings_are_explainable_and_never_change_canonical_or_occurrence_rows(self) -> None:
        self.job("sib-1", company="Example Health Inc.", source_state="verified_direct_ats", ats_url="https://boards.greenhouse.io/example/jobs/req-1", posted_at="2026-09-20T12:00:00+00:00")
        self.job("sib-2", company="Example Health Inc", source_state="verified_direct_ats", ats_url="https://boards.greenhouse.io/example/jobs/req-2", posted_at="2026-09-22T12:00:00+00:00")
        self.job("sib-conflict", company="Example Health Inc", source_state="identity_mismatch", ats_url="https://boards.greenhouse.io/example/jobs/req-3", posted_at="2026-09-21T12:00:00+00:00")
        self.job("sib-other", company="Other Health", source_state="verified_direct_ats", ats_url="https://boards.greenhouse.io/other/jobs/req-4", posted_at="2026-09-21T12:00:00+00:00")
        for job_id in ("sib-1", "sib-2", "sib-conflict", "sib-other"):
            self.conn.execute("INSERT INTO source_occurrences(occurrence_key,job_id,source_site,source_job_id,source_url,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)", (f"occ-{job_id}", job_id, "linkedin", job_id, f"https://linkedin.com/jobs/{job_id}", NOW, NOW))
        before = (self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], self.conn.execute("SELECT COUNT(*) FROM source_occurrences").fetchone()[0])
        first = probable_sibling_clusters(self.conn)
        second = probable_sibling_clusters(self.conn)
        self.assertEqual(first, second)
        self.assertEqual(first["cluster_count"], 1)
        cluster = first["clusters"][0]
        self.assertEqual(cluster["member_job_ids"], ["sib-1", "sib-2"])
        self.assertTrue(cluster["distinct_verified_requisitions"])
        self.assertIn("distinct verified requisition identities; related sibling records remain separate", cluster["basis"])
        self.assertFalse(cluster["canonical_jobs_merged"])
        self.assertFalse(cluster["source_occurrences_changed"])
        self.assertEqual(before, (self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], self.conn.execute("SELECT COUNT(*) FROM source_occurrences").fetchone()[0]))

    def test_provenance_labels_observed_inferred_and_unknown_and_export_summary(self) -> None:
        self.job("prov", ready=True)
        self.conn.execute("UPDATE jobs SET remote_status='unknown',remote_evidence_state='UNKNOWN',location_evidence_state='OBSERVED',evidence_provenance_json=? WHERE job_id='prov'", (json.dumps({"location": "observed", "posted_at": "observed_search_card_or_detail", "posted_at_source_type": "search_card", "source_type": "board_detail"}),))
        job = dict(self.conn.execute("SELECT * FROM jobs WHERE job_id='prov'").fetchone())
        summary = field_provenance_summary(job)
        self.assertEqual(summary["location"]["state"], "OBSERVED")
        self.assertEqual(summary["posted_current_status"]["state"], "OBSERVED")
        self.assertEqual(summary["posted_current_status"]["source_type"], "search_card")
        self.assertEqual(summary["remote"]["state"], "INFERRED_OR_DERIVED")
        self.assertEqual(summary["salary"]["state"], "INFERRED_OR_DERIVED")
        self.assertEqual(summary["employment_type"]["state"], "INFERRED_OR_DERIVED")
        self.assertEqual(summary["requirements"]["state"], "OBSERVED")
        self.assertEqual(summary["application_destination"]["state"], "OBSERVED")
        self.assertIn("field_provenance", query_jobs(self.conn, {"view": ["all"]})["jobs"][0])
        self.assertIn("field_provenance", job_detail(self.conn, "prov")["job"])
        path = export_selected(self.conn, self.root / "out", ["prov"])
        data = path.read_text(encoding="utf-8-sig")
        self.assertIn("field_provenance_json", data)
        self.assertIn("evidence_provenance_json", data)

    def test_dashboard_exposes_quality_sibling_and_provenance_apis(self) -> None:
        self.job("api-1", company="Example Health", ats_url="https://boards.greenhouse.io/example/jobs/api-1")
        self.job("api-2", company="Example Health", ats_url="https://boards.greenhouse.io/example/jobs/api-2", posted_at="2026-09-21T12:00:00+00:00")
        self.conn.commit()
        server = create_server(self.bundle, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(base + "/api/search-quality", timeout=5) as response:
                quality = json.loads(response.read())
            with urlopen(base + "/api/sibling-clusters", timeout=5) as response:
                siblings = json.loads(response.read())
            with urlopen(base + "/api/jobs/api-1", timeout=5) as response:
                detail = json.loads(response.read())
            self.assertEqual(quality["metric_definition_version"], "chg114-search-quality-v2")
            self.assertIn("current_actionable_count", quality["metric_definitions"])
            self.assertIn("no_repeat", quality["metric_definitions"]["qualified_yield"])
            self.assertIn("current_order", quality)
            self.assertTrue(siblings["advisory_only"])
            self.assertEqual(siblings["clusters"][0]["member_job_ids"], ["api-1", "api-2"])
            self.assertIn("field_provenance", detail["job"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_migration_idempotence_and_task_order_columns_are_durable(self) -> None:
        result = self.database.migrate()
        self.assertEqual(result.applied, ())
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(browser_search_tasks)")}
        self.assertTrue({"baseline_execution_rank", "effective_execution_rank", "learned_order_reason", "learned_order_sample_size", "ordering_algorithm_version"} <= columns)

    def test_rpc_leases_learned_order_with_phase_precedence(self) -> None:
        previous = rpc.BASE
        rpc.BASE = self.bundle.root
        try:
            run_id = self.run_row("lease")
            self.conn.execute("UPDATE browser_runs SET status='running' WHERE browser_run_id=?", (run_id,))
            self.conn.execute("INSERT INTO browser_platform_runs(browser_run_id,platform,auth_status,readiness_state) VALUES(?,?,?,?)", (run_id, "indeed", "verified", "verified"))
            first = self.task(run_id, "deep learned first", platform="indeed", phase="C_DEEP_BACKFILL", rank=10, status="queued")
            second = self.task(run_id, "deep baseline first", platform="indeed", phase="C_DEEP_BACKFILL", rank=20, status="queued")
            recent = self.task(run_id, "recent phase", platform="indeed", phase="A_FASTEST_DOOR_RECENT", rank=90, status="queued")
            self.conn.execute("UPDATE browser_search_tasks SET effective_execution_rank=1 WHERE task_id=?", (second,))
            self.conn.execute("UPDATE browser_search_tasks SET effective_execution_rank=20 WHERE task_id=?", (first,))
            self.conn.execute("UPDATE browser_search_tasks SET effective_execution_rank=999 WHERE task_id=?", (recent,))
            wave_run = self.run_row("wave")
            self.conn.execute("UPDATE browser_runs SET status='running',platform='linkedin,indeed' WHERE browser_run_id=?", (wave_run,))
            for platform in ("linkedin", "indeed"):
                self.conn.execute("INSERT INTO browser_platform_runs(browser_run_id,platform,auth_status,readiness_state) VALUES(?,?,?,?)", (wave_run, platform, "verified", "verified"))
            wave_linkedin = [self.task(wave_run, f"wave-linkedin-{index}", platform="linkedin", phase="A_FASTEST_DOOR_RECENT", rank=index + 1, status="queued") for index in range(3)]
            wave_indeed = self.task(wave_run, "wave-indeed", platform="indeed", phase="A_FASTEST_DOOR_RECENT", rank=1, status="queued")
            self.conn.commit()
            self.conn.close()
            self.assertTrue(rpc.handle({"action": "begin_run", "run_id": run_id})["ok"])
            leased = rpc.handle({"action": "next_task", "run_id": run_id, "platform": "indeed", "worker_id": "chg114-fixture"})["task"]
            self.assertEqual(leased["task_id"], recent)
            self.assertEqual(leased["phase"], "A_FASTEST_DOOR_RECENT")
            rpc.handle({"action": "complete_task", "run_id": run_id, "task_id": recent, "status": "exhausted", "reason": "fixture", "exhausted": True})
            next_task = rpc.handle({"action": "next_task", "run_id": run_id, "platform": "indeed", "worker_id": "chg114-fixture"})["task"]
            self.assertEqual(next_task["task_id"], second)
            self.assertTrue(rpc.handle({"action": "begin_run", "run_id": wave_run})["ok"])
            leased_wave = []
            for _ in range(3):
                task = rpc.handle({"action": "next_task", "run_id": wave_run, "worker_id": "chg114-wave"})["task"]
                leased_wave.append(task["task_id"])
                self.assertEqual(task["platform"], "linkedin")
                rpc.handle({"action": "complete_task", "run_id": wave_run, "task_id": task["task_id"], "status": "exhausted", "reason": "wave fixture", "exhausted": True})
            self.assertEqual(set(leased_wave), set(wave_linkedin))
            next_platform = rpc.handle({"action": "next_task", "run_id": wave_run, "worker_id": "chg114-wave"})["task"]
            self.assertEqual(next_platform["task_id"], wave_indeed)
        finally:
            try:
                self.conn.close()
            except Exception:
                pass
            rpc.BASE = previous


if __name__ == "__main__":
    unittest.main()
