from __future__ import annotations

import unittest
from pathlib import Path

from jobbot.sources import GlassdoorAdapter, IndeedAdapter, LinkedInAdapter


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class BrowserAdapterFixtureTests(unittest.TestCase):
    def text(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_search_cards_for_all_primary_platforms(self) -> None:
        cases = [
            (IndeedAdapter(), "indeed_search.html", "https://www.indeed.com/jobs?q=x", "indeed-1001"),
            (LinkedInAdapter(), "linkedin_search.html", "https://www.linkedin.com/jobs/search/", "2001"),
            (GlassdoorAdapter(), "glassdoor_search.html", "https://www.glassdoor.com/Job/", "3001"),
        ]
        for adapter, fixture, base, expected in cases:
            with self.subTest(platform=adapter.platform):
                cards = adapter.extract_result_cards(self.text(fixture), base)
                self.assertIn(expected, {x.source_job_id for x in cards})

    def test_jsonld_details_for_all_primary_platforms(self) -> None:
        cases = [
            (IndeedAdapter(), "indeed_detail.html", "https://www.indeed.com/viewjob?jk=indeed-1001", "Patient Enrollment Specialist"),
            (LinkedInAdapter(), "linkedin_detail.html", "https://www.linkedin.com/jobs/view/2001/", "Healthcare Quality Specialist"),
            (GlassdoorAdapter(), "glassdoor_detail.html", "https://www.glassdoor.com/job-listing/x-JV_?jl=3001", "Patient Access Specialist"),
        ]
        for adapter, fixture, url, expected in cases:
            with self.subTest(platform=adapter.platform):
                detail = adapter.extract_job_detail(self.text(fixture), url)
                self.assertEqual(detail.title, expected)
                self.assertTrue(detail.description)

    def test_auth_challenge_exhaustion_and_progress_fixtures(self) -> None:
        indeed = IndeedAdapter()
        self.assertEqual(indeed.inspect_auth(self.text("auth_required.html")), "auth_required")
        self.assertEqual(indeed.inspect_auth(self.text("challenge.html")), "challenged")
        self.assertEqual(indeed.detect_exhaustion(self.text("no_results.html"))[0], True)
        self.assertTrue(indeed.extract_result_cards(self.text("pagination.html"), "https://www.indeed.com/jobs"))
        self.assertTrue(LinkedInAdapter().extract_result_cards(self.text("infinite_scroll.html"), "https://www.linkedin.com/jobs/search/"))

    def test_closed_and_updated_detail_fixtures(self) -> None:
        adapter = IndeedAdapter()
        closed = adapter.extract_job_detail(self.text("closed_job.html"), "https://www.indeed.com/viewjob?jk=closed")
        updated = adapter.extract_job_detail(self.text("updated_job.html"), "https://www.indeed.com/viewjob?jk=updated")
        self.assertIn("closed", closed.description.lower())
        self.assertIn("Updated salary", updated.description)


if __name__ == "__main__": unittest.main()
