from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.db import Database
from jobbot.discoveries import upsert_card
from jobbot.evidence import ats_url_identity
from jobbot.legacy_engine import PrecisionStore, score_job
from jobbot.migrations.m0019_chg113_evidence_readiness import upgrade as upgrade_evidence_readiness
from jobbot.scoring import Job

from tests.helpers import bundle_with_database


ACTIONABLE = {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}
HEALTHCARE_DETAIL = (
    "Example Health is currently accepting applications for a fully remote United States role. "
    "This is a full-time permanent employee position. The employer provides a $60,000 annual base salary. "
    "No travel required. No required office days. No onsite training. No field work. No in-person events. "
    "The healthcare patient enrollment operations team reviews patient enrollment records, verifies documentation, "
    "resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records. "
    "Required Qualifications: 2 years of healthcare enrollment or relevant operations experience. "
    "HIPAA documentation and Excel workflows."
)


class Chg113EvidenceReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_bundle(PROJECT_ROOT)
        cls.strategy = cls.bundle.strategy
        cls.candidate = cls.bundle.legacy_runtime()["candidate"]

    def score(self, job: Job, strategy: dict | None = None) -> Job:
        setattr(job, "_mode", "deep")
        return score_job(job, strategy or self.strategy, self.candidate)

    def direct_job(self, title: str = "Patient Enrollment Specialist", description: str = HEALTHCARE_DETAIL) -> Job:
        return self.score(Job(
            source_site="greenhouse", source_job_id="req-100",
            canonical_url="https://boards.greenhouse.io/example/jobs/100",
            apply_url="https://boards.greenhouse.io/example/jobs/100",
            title=title, company="Example Health", location_raw="Remote — United States",
            remote_status="remote", employment_type="Full-time permanent",
            salary_text="$60,000 per year", salary_min=60000, salary_max=60000,
            salary_currency="USD", salary_period="year", posted_at="2026-09-21T12:00:00+00:00",
            description=description, raw={"posting_status": "active"},
        ))

    def test_public_ats_identity_requires_a_real_host_boundary(self) -> None:
        self.assertEqual(ats_url_identity("https://boards.greenhouse.io/example/jobs/100")[0], "greenhouse")
        self.assertEqual(ats_url_identity("https://fakegreenhouse.io/example/jobs/100")[0], "")
        self.assertEqual(ats_url_identity("https://boards.greenhouse.io/example")[0], "")

    def test_title_only_collision_cards_remain_recall_review_and_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PrecisionStore(Path(temp) / "jobs.sqlite3")
            try:
                conn = store.conn
                now = "2026-09-22T12:00:00+00:00"
                run_id = int(conn.execute(
                    "INSERT INTO browser_runs(version,mode,platform,status,created_at) VALUES(?,?,?,?,?)",
                    ("test", "search", "linkedin", "running", now),
                ).lastrowid)
                task_id = int(conn.execute(
                    "INSERT INTO browser_search_tasks(browser_run_id,platform,query_text,window_days,search_url,created_at) VALUES(?,?,?,?,?,?)",
                    (run_id, "linkedin", '"registrar"', 30, "https://www.linkedin.com/jobs/search/?keywords=registrar", now),
                ).lastrowid)
                fixtures = (
                    ("Trauma Registrar", "li-trauma-1"),
                    ("Cancer Registrar 2", "li-cancer-2"),
                    ("Weekend Admissions Screener", "li-admissions-3"),
                )
                for title, source_id in fixtures:
                    result, _ = upsert_card(
                        conn, run_id=run_id, task_id=task_id, platform="linkedin",
                        source_job_id=source_id, source_url=f"https://www.linkedin.com/jobs/view/{source_id}/",
                        title_hint=title, company_hint="Example Organization", card={"title": title, "company": "Example Organization"},
                        recall_selected=True, recall_reason="historical title collision",
                    )
                    self.assertEqual(result.identity_status, "PERSISTED")
                    self.assertEqual(result.detail_status, "PENDING")
                    self.assertEqual(result.content_state, "MISSING")
                    job = self.score(Job(
                        source_site="linkedin", source_job_id=source_id,
                        canonical_url=f"https://www.linkedin.com/jobs/view/{source_id}/", title=title,
                        company="Example Organization", description="", raw={"query_family": "registrar_recall"},
                    ))
                    self.assertEqual(job.recommendation, "REVIEW", title)
                    self.assertNotIn(job.recommendation, ACTIONABLE)
                    self.assertEqual(job.detail_evidence_state, "MISSING")
                    self.assertEqual(job.evidence_readiness_state, "REVIEW")
                    store.upsert(job)
                row = conn.execute("SELECT * FROM search_task_results ORDER BY result_id LIMIT 1").fetchone()
                self.assertEqual(row["discovery_url"], "https://www.linkedin.com/jobs/view/li-trauma-1/")
                self.assertEqual(row["detail_evidence_state"], "MISSING")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_occurrences").fetchone()[0], 3)
            finally:
                store.close()

    def test_readiness_migration_is_idempotent_for_ready_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = bundle_with_database(Path(temp) / "jobs.sqlite3")
            Database(bundle).migrate()
            store = PrecisionStore(bundle.database_path)
            try:
                job = self.direct_job()
                store.upsert(job)
                job_id = store.resolve_job_id(job)
                upgrade_evidence_readiness(store.conn)
                upgrade_evidence_readiness(store.conn)
                row = store.conn.execute(
                    "SELECT recommendation,evidence_readiness_state,qualification_readiness_state FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(tuple(row), (job.recommendation, "READY", "READY"))
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (job_id,)).fetchone()[0], 1)
            finally:
                store.close()

    def test_verified_higher_ed_and_nonclinical_healthcare_examples_can_be_actionable(self) -> None:
        healthcare = self.direct_job()
        self.assertIn(healthcare.recommendation, ACTIONABLE, healthcare.qualification_gates)
        self.assertEqual(healthcare.evidence_readiness_state, "READY")
        self.assertEqual(healthcare.qualification_readiness_state, "READY")
        self.assertEqual(healthcare.application_destination_verification_state, "VERIFIED_ATS")
        self.assertEqual(healthcare.evidence_readiness["recommendation"], healthcare.recommendation)
        self.assertTrue(healthcare.evidence_readiness["recommendation_reason"])

        higher_ed_description = HEALTHCARE_DETAIL.replace("Example Health", "Example University").replace(
            "The healthcare patient enrollment operations team reviews patient enrollment records, verifies documentation, "
            "resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records.",
            "The academic records office reviews and maintains student records, evaluates transcripts, reconciles "
            "registration files, and documents registrar workflow outcomes.",
        ).replace(
            "Required Qualifications: 2 years of healthcare enrollment or relevant operations experience. HIPAA documentation and Excel workflows.",
            "Required Qualifications: 1 year of relevant administrative operations experience. Excel and accurate record documentation.",
        )
        registrar = self.direct_job("Registrar Coordinator", higher_ed_description)
        registrar.company = "Example University"
        registrar = self.score(registrar)
        self.assertEqual(registrar.qualification_gates["responsibility_domain"]["status"], "pass", registrar.qualification_gates)
        self.assertIn(registrar.recommendation, ACTIONABLE, registrar.qualification_gates)
        self.assertEqual(registrar.evidence_readiness_state, "READY")

    def test_enriched_true_negatives_keep_evidence_backed_reasons(self) -> None:
        clinical = self.direct_job(description=HEALTHCARE_DETAIL + " Active RN license required.")
        self.assertNotIn(clinical.recommendation, ACTIONABLE)
        self.assertEqual(clinical.qualification_gates["requirements_supported"]["status"], "review")
        self.assertTrue(any("RN" in item for item in clinical.hard_reject_reasons))

        sales_text = HEALTHCARE_DETAIL.replace(
            "reviews patient enrollment records, verifies documentation, resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records",
            "sells enrollment services, manages sales quotas, closes new employer accounts, and expands a regional sales pipeline",
        ) + " Required Qualifications: 3 years of healthcare sales experience."
        sales = self.direct_job("Healthcare Sales Specialist", sales_text)
        self.assertNotIn(sales.recommendation, ACTIONABLE)
        self.assertIn(sales.recommendation, {"OUT_OF_SCOPE", "SKIP_HARD_GATE", "REVIEW"})

    def test_board_detail_does_not_verify_final_application_destination(self) -> None:
        job = self.direct_job()
        job.source_site = "indeed"
        job.source_job_id = "indeed-201"
        job.canonical_url = "https://www.indeed.com/viewjob?jk=indeed-201"
        job.apply_url = ""
        job.ats_requisition_url = ""; job.verified_application_url = ""
        job.raw = {"posting_status": "active", "discovery_url": job.canonical_url, "board_detail_url": job.canonical_url}
        job = self.score(job)
        self.assertNotIn(job.recommendation, ACTIONABLE)
        self.assertEqual(job.evidence_readiness_state, "REVIEW")
        self.assertEqual(job.application_destination_verification_state, "BOARD_ONLY")
        self.assertEqual(job.verified_application_url, "")

    def test_board_to_ats_reconciliation_retains_board_occurrence_and_role_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PrecisionStore(Path(temp) / "jobs.sqlite3")
            try:
                board_url = "https://www.indeed.com/viewjob?jk=indeed-301"
                ats_url = "https://boards.greenhouse.io/example/jobs/301"
                board = self.direct_job()
                board.source_site = "indeed"; board.source_job_id = "indeed-301"
                board.canonical_url = board_url; board.apply_url = ats_url
                board.ats_requisition_url = ""; board.verified_application_url = ""
                board.raw = {"posting_status": "active", "_discovery_company": "Example Health", "discovery_url": board_url,
                             "board_detail_url": board_url, "observed_board_apply_url": ats_url}
                board = self.score(board)
                self.assertNotIn(board.recommendation, ACTIONABLE)
                store.upsert(board)
                canonical_id = store.resolve_job_id(board)

                # Trusted enrichment upgrades the same board occurrence in place.
                board.canonical_url = ats_url
                board.apply_url = ats_url
                board.raw["ats_enrichment"] = {"id": "301"}
                board = self.score(board)
                store.upsert(board)
                enriched_occurrence = store.conn.execute(
                    "SELECT * FROM source_occurrences WHERE source_site='indeed'"
                ).fetchone()
                self.assertEqual(enriched_occurrence["source_url"], board_url)
                self.assertEqual(enriched_occurrence["verified_application_url"], ats_url)

                ats = self.direct_job()
                ats.source_job_id = "greenhouse-301"; ats.canonical_url = ats_url; ats.apply_url = ats_url
                ats.ats_requisition_url = ""; ats.verified_application_url = ""
                ats = self.score(ats)
                store.upsert(ats)

                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM source_occurrences WHERE job_id=?", (canonical_id,)).fetchone()[0], 2)
                row = store.conn.execute("SELECT * FROM jobs WHERE job_id=?", (canonical_id,)).fetchone()
                self.assertEqual(row["discovery_url"], board_url)
                self.assertEqual(row["board_detail_url"], board_url)
                self.assertEqual(row["observed_board_apply_url"], ats_url)
                self.assertEqual(row["ats_requisition_url"], ats_url)
                self.assertEqual(row["verified_application_url"], ats_url)
                occurrence = store.conn.execute("SELECT * FROM source_occurrences WHERE source_site='indeed'").fetchone()
                self.assertEqual(occurrence["source_url"], board_url)
                self.assertEqual(occurrence["observed_board_apply_url"], ats_url)
                self.assertIn(store.conn.execute("SELECT recommendation FROM jobs WHERE job_id=?", (canonical_id,)).fetchone()[0], ACTIONABLE)
            finally:
                store.close()

    def test_identity_mismatch_never_upgrades_canonical_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PrecisionStore(Path(temp) / "jobs.sqlite3")
            try:
                ats_url = "https://boards.greenhouse.io/example/jobs/401"
                board_url = "https://www.linkedin.com/jobs/view/li-401/"
                board = self.direct_job()
                board.source_site = "linkedin"; board.source_job_id = "li-401"
                board.canonical_url = board_url; board.apply_url = ats_url
                board.ats_requisition_url = ""; board.verified_application_url = ""
                board.raw = {"posting_status": "active", "_discovery_company": "Example Health", "discovery_url": board_url,
                             "board_detail_url": board_url, "observed_board_apply_url": ats_url}
                board = self.score(board); store.upsert(board)
                canonical_id = store.resolve_job_id(board)

                wrong = self.direct_job()
                wrong.source_job_id = "wrong-401"; wrong.canonical_url = ats_url; wrong.apply_url = ats_url
                wrong.ats_requisition_url = ""; wrong.verified_application_url = ""
                wrong.company = "Different Employer"
                wrong = self.score(wrong)
                store.upsert(wrong)

                original = store.conn.execute("SELECT * FROM jobs WHERE job_id=?", (canonical_id,)).fetchone()
                self.assertEqual(original["canonical_verified"], 0)
                self.assertEqual(original["source_verification"], "direct_ats_link")
                self.assertEqual(original["verified_application_url"], "")
                mismatched = store.conn.execute("SELECT * FROM jobs WHERE source_verification='identity_mismatch'").fetchone()
                self.assertIsNotNone(mismatched)
                self.assertEqual(mismatched["canonical_verified"], 0)
                self.assertEqual(mismatched["evidence_readiness_state"], "BLOCKED")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM source_occurrences WHERE source_site='linkedin'").fetchone()[0], 1)
            finally:
                store.close()

    def test_strategy_rescore_is_score_only_but_source_change_versions_and_recalculates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = bundle_with_database(Path(temp) / "jobs.sqlite3")
            Database(bundle).migrate()
            store = PrecisionStore(bundle.database_path)
            try:
                job = self.direct_job()
                job.raw = {"posting_status": "active"}
                job = self.score(job)
                store.upsert(job)
                job_id = store.resolve_job_id(job)
                before = store.conn.execute("SELECT application_priority_score FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
                version_count = store.conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (job_id,)).fetchone()[0]

                strategy = copy.deepcopy(self.strategy)
                strategy["_live_search_profile"] = {"profile": {"preferences": {"negative_terms": ["Example Health"], "maximum_adjustment": 6}}}
                store.rescore_all(strategy, self.candidate)
                after = store.conn.execute("SELECT application_priority_score FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
                self.assertNotEqual(before, after)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (job_id,)).fetchone()[0], version_count)

                changed = self.direct_job(description=HEALTHCARE_DETAIL.replace("HIPAA documentation and Excel workflows.", "HIPAA documentation and Excel workflows. SQL required."))
                changed.raw = {"posting_status": "active"}
                changed = self.score(changed)
                self.assertEqual(store.upsert(changed), "updated")
                row = store.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job_versions WHERE job_id=?", (job_id,)).fetchone()[0], version_count + 1)
                self.assertEqual(row["detail_evidence_state"], "COMPLETE")
                self.assertEqual(row["requirements_evidence_state"], "UNRESOLVED")
                self.assertNotIn(row["recommendation"], ACTIONABLE)
                self.assertIn("requirements_supported", json.loads(row["qualification_gates_json"]))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
