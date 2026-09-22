from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.legacy_engine import PrecisionStore, score_job, select_daily_plan
from jobbot.scoring import Job


ACTIONABLE = {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}


class QualificationGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_bundle(PROJECT_ROOT)
        cls.strategy = cls.bundle.strategy
        cls.candidate = cls.bundle.legacy_runtime()["candidate"]

    def job(self, *, title: str = "Patient Enrollment Specialist", description: str | None = None, **overrides) -> Job:
        body = description or (
            "Example Health is currently accepting applications for a fully remote United States role. "
            "This is a full-time permanent employee position. The employer provides a $60,000 to $65,000 "
            "annual base salary. No travel required. No required office days. No onsite training. "
            "No field work. No in-person events. The healthcare patient enrollment operations team "
            "reviews patient enrollment records, verifies documentation, resolves discrepancies, "
            "coordinates workflow handoffs, and prepares accurate case records. "
            "Required Qualifications: 2 years of healthcare enrollment or relevant operations experience. "
            "HIPAA documentation and Excel workflows. "
        )
        values = {
            "source_site": "greenhouse",
            "source_job_id": "qualified-100",
            "canonical_url": "https://boards.greenhouse.io/example/jobs/100",
            "apply_url": "https://boards.greenhouse.io/example/jobs/100",
            "title": title,
            "company": "Example Health",
            "location_raw": "Remote — United States",
            "remote_status": "remote",
            "employment_type": "Full-time permanent",
            "salary_text": "$60,000 to $65,000 per year",
            "salary_min": 60000,
            "salary_max": 65000,
            "salary_currency": "USD",
            "salary_period": "year",
            "posted_at": "2026-09-21T12:00:00+00:00",
            "description": body,
            "raw": {"posting_status": "active"},
        }
        values.update(overrides)
        job = Job(**values)
        setattr(job, "_mode", "deep")
        return job

    def score(self, job: Job) -> Job:
        return score_job(job, self.strategy, self.candidate)

    def test_complete_direct_posting_passes_every_gate(self) -> None:
        job = self.score(self.job())
        self.assertIn(job.recommendation, ACTIONABLE, job.qualification_gates)
        self.assertTrue(all(gate["status"] == "pass" for gate in job.qualification_gates.values()), job.qualification_gates)

    def test_unknown_remote_and_texas_facts_are_non_actionable(self) -> None:
        remote_unknown = self.job(description=self.job().description.replace("fully remote", "remote"))
        scored_remote = self.score(remote_unknown)
        self.assertEqual(scored_remote.qualification_gates["home_remote"]["status"], "review")
        self.assertNotIn(scored_remote.recommendation, ACTIONABLE)

        tx_excluded = self.score(self.job(description=self.job().description + " We are not hiring residents of Texas."))
        self.assertEqual(tx_excluded.qualification_gates["texas_eligibility"]["status"], "fail")
        self.assertNotIn(tx_excluded.recommendation, ACTIONABLE)

        tx_unknown = self.score(self.job(description=self.job().description + " Must reside in an eligible state listed in a separate notice."))
        self.assertEqual(tx_unknown.qualification_gates["texas_eligibility"]["status"], "review")
        self.assertNotIn(tx_unknown.recommendation, ACTIONABLE)

    def test_full_time_and_permanence_unknown_or_failed_are_non_actionable(self) -> None:
        base = self.job().description
        fulltime_unknown = self.score(self.job(description=base.replace("full-time permanent employee", "employee"), employment_type=""))
        self.assertEqual(fulltime_unknown.qualification_gates["full_time"]["status"], "review")
        self.assertNotIn(fulltime_unknown.recommendation, ACTIONABLE)

        part_time = self.score(self.job(description=base + " This position is part-time."))
        self.assertNotIn(part_time.recommendation, ACTIONABLE)

        permanent_unknown = self.score(self.job(description=base.replace(" permanent", ""), employment_type="Full-time"))
        self.assertEqual(permanent_unknown.qualification_gates["permanent_employee"]["status"], "review")
        self.assertNotIn(permanent_unknown.recommendation, ACTIONABLE)

        contract = self.score(self.job(description=base + " This is a 12-month contract."))
        self.assertNotIn(contract.recommendation, ACTIONABLE)
        self.assertEqual(contract.qualification_gates["permanent_employee"]["status"], "fail")

    def test_employer_provided_salary_floor_is_fail_closed(self) -> None:
        missing = self.score(self.job(salary_text="", salary_min=None, salary_max=None))
        self.assertEqual(missing.qualification_gates["base_pay_floor"]["status"], "review")
        self.assertNotIn(missing.recommendation, ACTIONABLE)

        estimated = self.score(self.job(salary_text="Estimated $60,000/year"))
        self.assertEqual(estimated.qualification_gates["base_pay_floor"]["status"], "review")
        self.assertNotIn(estimated.recommendation, ACTIONABLE)

        under_floor = self.score(self.job(salary_text="$49,919/year", salary_min=49919, salary_max=49919))
        self.assertEqual(under_floor.qualification_gates["base_pay_floor"]["status"], "fail")
        self.assertNotIn(under_floor.recommendation, ACTIONABLE)

        hourly = self.job(salary_text="$24/hour", salary_min=24, salary_max=30, salary_period="hour")
        hourly.description = hourly.description.replace("$60,000 to $65,000 annual base salary", "$24 to $30 hourly base salary")
        self.score(hourly)
        self.assertEqual(hourly.qualification_gates["base_pay_floor"]["status"], "pass")
        self.assertEqual(hourly.salary_annual_min, 49920)

    def test_unknown_or_required_travel_and_in_person_conditions_are_non_actionable(self) -> None:
        base = self.job().description
        unknown = self.score(self.job(description=base.replace("No travel required. ", "").replace("No required office days. ", "").replace("No onsite training. ", "").replace("No field work. ", "").replace("No in-person events. ", "")))
        self.assertEqual(unknown.qualification_gates["mandatory_presence"]["status"], "review")
        self.assertNotIn(unknown.recommendation, ACTIONABLE)

        for required in (
            "Mandatory travel required 10%.",
            "Mandatory office attendance is required monthly.",
            "Mandatory onsite training is required.",
            "Field work is required.",
            "Required in-person events are held quarterly.",
        ):
            with self.subTest(required=required):
                text = base.replace("No travel required. ", "").replace("No required office days. ", "").replace("No onsite training. ", "").replace("No field work. ", "").replace("No in-person events. ", "") + required
                scored = self.score(self.job(description=text))
                self.assertEqual(scored.qualification_gates["mandatory_presence"]["status"], "fail", scored.qualification_gates)
                self.assertNotIn(scored.recommendation, ACTIONABLE)

        conflict = self.score(self.job(description=base + " However, mandatory travel is required."))
        self.assertEqual(conflict.qualification_gates["mandatory_presence"]["status"], "fail")
        self.assertNotIn(conflict.recommendation, ACTIONABLE)

    def test_open_current_unknown_and_closed_are_non_actionable(self) -> None:
        unknown_text = self.job().description.replace(" currently accepting applications", "")
        unknown = self.score(self.job(raw={}, description=unknown_text))
        self.assertEqual(unknown.qualification_gates["open_current"]["status"], "review")
        self.assertNotIn(unknown.recommendation, ACTIONABLE)

        expired = self.score(self.job(raw={"posting_status": "closed"}))
        self.assertEqual(expired.qualification_gates["open_current"]["status"], "fail")
        self.assertNotIn(expired.recommendation, ACTIONABLE)

    def test_unsupported_mandatory_credential_skills_and_experience_are_non_actionable(self) -> None:
        base = self.job().description
        credential = self.score(self.job(description=base + " Active RN license required."))
        self.assertNotIn(credential.recommendation, ACTIONABLE)
        self.assertTrue(any("RN" in reason for reason in credential.hard_reject_reasons))

        unsupported_credential = self.score(self.job(description=base + " A Certified Registrar certification is required."))
        self.assertEqual(unsupported_credential.qualification_gates["requirements_supported"]["status"], "review", unsupported_credential.required_qualifications)
        self.assertNotIn(unsupported_credential.recommendation, ACTIONABLE)

        unsupported_skill = self.score(self.job(description=base.replace("HIPAA documentation and Excel", "SQL required and advanced database optimization")))
        self.assertEqual(unsupported_skill.qualification_gates["requirements_supported"]["status"], "review")
        self.assertNotIn(unsupported_skill.recommendation, ACTIONABLE)

        unsupported_experience = self.score(self.job(description=base.replace("2 years of healthcare enrollment or relevant operations experience", "10 years of direct healthcare operations experience")))
        self.assertEqual(unsupported_experience.qualification_gates["requirements_supported"]["status"], "review")
        self.assertNotIn(unsupported_experience.recommendation, ACTIONABLE)

    def test_preference_mismatch_changes_ranking_without_removing_discovery(self) -> None:
        preferred = self.score(self.job())
        busy_text = self.job().description + " This high-volume call queue has continuous calls and live intake demands."
        busy = self.score(self.job(title="Admissions Sales Specialist", description=busy_text))
        self.assertLess(busy.preference_adjustment, preferred.preference_adjustment)
        self.assertNotEqual(busy.recommendation, "OUT_OF_SCOPE")
        self.assertTrue(busy.recall_reason)

    def test_ambiguous_titles_require_substantive_responsibility_evidence(self) -> None:
        baseline = self.job().description
        registrar = self.score(self.job(
            title="Registrar Coordinator",
            description=baseline.replace("Example Health", "Example University").replace(
                "The healthcare patient enrollment operations team reviews patient enrollment records, verifies documentation, resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records.",
                "The office coordinates meetings and deadlines and responds to routine requests.",
            ).replace("healthcare enrollment or relevant operations experience", "relevant office experience"),
        ))
        self.assertEqual(registrar.qualification_gates["responsibility_domain"]["status"], "review")
        self.assertNotIn(registrar.recommendation, ACTIONABLE)

        substantive = self.score(self.job(
            title="Registrar Coordinator",
            description=baseline.replace("Example Health", "Example University").replace(
                "The healthcare patient enrollment operations team reviews patient enrollment records, verifies documentation, resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records.",
                "The academic records team reviews and maintains student records, evaluates transcripts, reconciles application files, and documents registrar workflow outcomes.",
            ).replace("healthcare enrollment or relevant operations experience", "higher education records experience"),
        ))
        self.assertEqual(substantive.qualification_gates["responsibility_domain"]["status"], "pass")

        provider = self.score(self.job(
            title="Provider Enrollment Specialist",
            description=baseline.replace(
                "The healthcare patient enrollment operations team reviews patient enrollment records, verifies documentation, resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records.",
                "The team coordinates recurring records and tracks deadlines for internal projects.",
            ).replace("healthcare enrollment or relevant operations experience", "relevant operations experience"),
        ))
        self.assertEqual(provider.qualification_gates["responsibility_domain"]["status"], "review")
        self.assertNotIn(provider.recommendation, ACTIONABLE)

        provider_experience = self.score(self.job(
            title="Provider Enrollment Specialist",
            description=baseline + " Prior provider enrollment and credentialing operations experience is required.",
        ))
        self.assertEqual(provider_experience.qualification_gates["requirements_supported"]["status"], "review")
        self.assertIn("direct provider enrollment/credentialing experience", provider_experience.qualification_gates["requirements_supported"]["evidence"])
        self.assertNotIn(provider_experience.recommendation, ACTIONABLE)

        title_only_body = baseline.replace("Example Health", "Example Organization").replace(
            "The healthcare patient enrollment operations team reviews patient enrollment records, verifies documentation, resolves discrepancies, coordinates workflow handoffs, and prepares accurate case records.",
            "The team communicates with internal peers, follows established deadlines, and completes routine administrative tasks.",
        ).replace("healthcare enrollment or relevant operations experience", "relevant office experience").replace(
            "HIPAA documentation and Excel workflows", "routine office paperwork and spreadsheets",
        )
        for title in (
            "Admissions Coordinator", "Intake Coordinator", "Operations Coordinator",
            "Healthcare Operations Coordinator", "Implementation Coordinator", "Project Coordinator",
        ):
            with self.subTest(title=title):
                ambiguous = self.score(self.job(
                    title=title, company="Example Organization", description=title_only_body,
                    raw={"posting_status": "active", "query_family": "bounded_transferable_records_data_process_ops"},
                ))
                self.assertNotIn(ambiguous.recommendation, ACTIONABLE)
                gates = getattr(ambiguous, "qualification_gates", {})
                if gates:
                    self.assertEqual(gates["responsibility_domain"]["status"], "review")

    def test_handled_opportunity_is_not_resurfaced_and_history_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PrecisionStore(Path(temp) / "jobs.sqlite3")
            job = self.job(raw={
                "posting_status": "active",
                "strategy_profile": "jobbot-broad-qualified-yield",
                "strategy_profile_version": "2026-09-22.1",
                "query_family": "backoffice_patient_access_eligibility_enrollment",
                "query_kind": "exact_phrase",
                "query_pass": "indeed_exact_phrase",
                "initial_order": 6,
            })
            self.score(job)
            self.assertIn(job.recommendation, ACTIONABLE)
            store.upsert(job)
            stored_id = store.conn.execute("SELECT job_id FROM jobs").fetchone()[0]
            store.mark(stored_id, "applied", "sent through employer portal")
            handled = store.conn.execute("SELECT recommendation,qualification_gates_json FROM jobs WHERE job_id=?", (stored_id,)).fetchone()
            self.assertEqual(handled["recommendation"], "ALREADY_HANDLED")
            self.assertEqual(json.loads(handled["qualification_gates_json"])["no_repeat"]["status"], "fail")

            duplicate = self.job(raw=job.raw)
            self.score(duplicate)
            store.upsert(duplicate)
            row = store.conn.execute("SELECT * FROM jobs WHERE job_id=?", (stored_id,)).fetchone()
            gates = json.loads(row["qualification_gates_json"])
            self.assertEqual(row["recommendation"], "ALREADY_HANDLED")
            self.assertEqual(gates["no_repeat"]["status"], "fail")
            self.assertEqual(row["application_status"].upper(), "APPLIED")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM application_events WHERE job_id=?", (stored_id,)).fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT seen_count FROM source_occurrences WHERE job_id=?", (stored_id,)).fetchone()[0], 2)
            self.assertEqual(store.conn.execute("SELECT query_family FROM source_occurrences WHERE job_id=?", (stored_id,)).fetchone()[0], "backoffice_patient_access_eligibility_enrollment")
            self.assertEqual(select_daily_plan(store.rows(), self.strategy), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
