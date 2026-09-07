from __future__ import annotations

import unittest

from jobbot.config import PROJECT_ROOT, load_bundle
from jobbot.scoring import detect_required_credential, phrase_present, years_required

from tests.helpers import scored


class ScoringRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.strategy = load_bundle(PROJECT_ROOT).strategy

    def test_negative_occupation_families(self) -> None:
        titles = [
            "Senior AI Engineer", "Staff AI Engineer", "Senior Software Engineer",
            "Principal R&D/Product Development Engineer", "Product Security Analyst", "Controller",
            "Senior Finance Manager", "Senior Deal Desk Manager", "Sales Engineer", "Account Executive",
            "Territory Manager", "Marketing Director",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(scored(title, "Remote healthcare quality compliance workflow data implementation.").recommendation, "OUT_OF_SCOPE")

    def test_positive_role_families(self) -> None:
        titles = [
            "Patient Enrollment Specialist", "Healthcare Enrollment Specialist", "Patient Access Specialist",
            "Insurance Verification Specialist", "Credentialing Associate", "Credentialing & Enrollment Coordinator",
            "Care Coordinator", "Care Partner", "Patient Services Coordinator", "Healthcare Operations Coordinator",
            "Customer Operations Coordinator — Healthcare", "Medical Records Specialist", "Health Information Specialist",
            "Healthcare Quality Specialist", "Data Operations Specialist", "Healthcare Implementation Coordinator",
        ]
        description = "Fully remote US healthcare role. Required Qualifications: 2 years of relevant operations experience. Full-time permanent employee with benefits. HIPAA documentation and Excel workflows."
        for title in titles:
            with self.subTest(title=title):
                self.assertIn(scored(title, description).recommendation, {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})

    def test_growth_roles_do_not_reward_job_owned_technical_skills(self) -> None:
        sql = scored("Healthcare Data Analyst", "Fully remote full-time healthcare analytics. Required Qualifications: SQL and 3 years of healthcare reporting.")
        self.assertNotEqual(sql.recommendation, "APPLY_NOW")
        self.assertTrue(any("SQL" in gap for gap in sql.requirement_gaps))
        interoperability = scored("Clinical Data Analyst", "Fully remote full-time. Required Qualifications: SQL, HL7, FHIR, CCDA, ADT and 5 years healthcare interoperability.")
        self.assertNotIn(interoperability.recommendation, {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})

    def test_fuzzy_title_and_section_heading_precision(self) -> None:
        analytics = scored(
            "Healthcare Data and Analytics Specialist - Remote - USA",
            "Fully remote, full-time role. Required Qualifications: SQL and 3 years of healthcare analytics.",
            source="linkedin",
        )
        self.assertEqual(analytics.search_profile, "P1-healthcare-quality-data")
        self.assertNotEqual(analytics.normalized_title_family, "Healthcare Coordinator")
        sales = scored(
            "Patient Access Specialist",
            "Meet requirements per FDA rules. Essential Requirements: 5 years of healthcare sales and account management. Desired Requirements: MBA.",
        )
        self.assertEqual(sales.years_required, 5)
        self.assertIn("5 years of healthcare sales", sales.required_qualifications)
        self.assertNotIn("MBA", sales.required_qualifications)
        self.assertNotIn(sales.recommendation, {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})

    def test_complete_trusted_primary_detail_can_enter_qualified_queue(self) -> None:
        description = "Fully remote US healthcare enrollment role. Required Qualifications: 2 years relevant operations experience. Full-time permanent employee with benefits. HIPAA, Excel, patient communication, intake, and documentation accuracy. " * 5
        job = scored("Patient Enrollment Specialist", description, source="linkedin")
        self.assertGreaterEqual(job.extraction_confidence, 85)
        self.assertIn(job.recommendation, {"APPLY_NOW", "APPLY_VOLUME"})

    def test_remote_clinical_management_and_employment_gates(self) -> None:
        linkedin_shape = scored("Patient Access Specialist", "Healthcare enrollment operations role.", location="Remote", source="linkedin", remote_status="unknown")
        self.assertEqual(linkedin_shape.remote_gate, "pass")
        self.assertEqual(scored("Patient Access Specialist", "#LI-Remote. Hybrid required with three mandatory office days.").recommendation, "SKIP_HARD_GATE")
        self.assertEqual(scored("Patient Enrollment Specialist — Offshore Philippines", "Remote role.", location="USA").recommendation, "SKIP_HARD_GATE")
        self.assertEqual(scored("Healthcare Quality Specialist", "Fully remote. Active RN license required.").recommendation, "SKIP_HARD_GATE")
        self.assertEqual(scored("Certified Nurse-Midwife", "Fully remote. Active CNM required.").recommendation, "OUT_OF_SCOPE")
        self.assertNotIn(scored("Senior Healthcare Operations Manager", "Fully remote. Required: manage a team of 20 with direct reports.").recommendation, {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"})
        self.assertEqual(scored("Patient Enrollment Specialist", "Fully remote role.", employment="1099 independent contractor").recommendation, "CONTRACT_REVIEW")
        self.assertEqual(scored("Patient Enrollment Specialist", "Fully remote role.", employment="12-month contract").recommendation, "FIXED_TERM_REVIEW")
        self.assertEqual(scored("Patient Enrollment Specialist", "Fully remote role.", employment="Part-time").recommendation, "PART_TIME_REVIEW")

    def test_boundary_and_requirement_parser_regressions(self) -> None:
        self.assertFalse(phrase_present("SIS", "analysis"))
        self.assertFalse(phrase_present("Lean", "clean"))
        self.assertFalse(detect_required_credential("Requirements: do the work and do it well.", "DO", self.strategy))
        self.assertEqual(years_required("8+ years of program management"), 8)
        html_job = scored("Patient Enrollment Specialist", "<h2>Required Qualifications</h2><ul><li>2+ years of healthcare enrollment</li></ul><h2>Preferred</h2><p>SQL preferred</p>")
        self.assertEqual(html_job.years_required, 2)
        self.assertFalse(any("SQL" in x for x in html_job.required_skills))


if __name__ == "__main__": unittest.main()
