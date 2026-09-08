from __future__ import annotations

import copy
import unittest
import urllib.parse

from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle, validate_strategy
from jobbot.search_plan import build_search_url, compile_plan, normalize_search_query, plan_counts


class StrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = load_bundle(PROJECT_ROOT)

    def test_exact_portfolio_and_taxonomy_counts(self) -> None:
        lanes = {x["id"]: x for x in self.bundle.strategy["lanes"] if x.get("core", True)}
        self.assertEqual({k: int(v["allocation_percent"]) for k, v in lanes.items()}, {
            "HEALTHCARE_OPS_ACCESS": 35, "HEALTHCARE_INFO_QA": 15,
            "HEALTHCARE_QUALITY_DATA": 20, "HEALTHCARE_IMPLEMENTATION": 10,
            "HIGHER_ED_EDTECH": 15, "CONTENT_AI_QUALITY": 5,
        })
        self.assertEqual({k: len(v["titles"]) for k, v in lanes.items()}, {
            "HEALTHCARE_OPS_ACCESS": 62, "HEALTHCARE_INFO_QA": 13,
            "HEALTHCARE_QUALITY_DATA": 16, "HEALTHCARE_IMPLEMENTATION": 14,
            "HIGHER_ED_EDTECH": 24, "CONTENT_AI_QUALITY": 10,
        })
        configured = {title for lane in lanes.values() for title in lane["titles"]}
        configured |= {title for lane in lanes.values() for title in lane.get("covered_titles", [])}
        required = {
            "Patient Enrollment Specialist", "Payer Enrollment Specialist", "Care Partner",
            "Remote Patient Monitoring Coordinator", "Customer Operations Coordinator — Healthcare",
            "ROI Medical Records Specialist", "Healthcare Quality Specialist",
            "Healthcare Data Analyst", "Clinical Data Analyst", "Business Process Analyst — Healthcare",
            "Implementation & Compliance Specialist — Healthcare", "HealthTech Implementation Specialist",
            "Credential Evaluator", "EdTech Implementation Specialist", "Spanish Language Reviewer",
            "AI Evaluator", "Education Content Reviewer",
        }
        self.assertTrue(required <= configured, required - configured)

    def test_plan_counts_and_no_production_caps(self) -> None:
        deep = compile_plan(self.bundle, "deep")
        fast = compile_plan(self.bundle, "fast")
        self.assertEqual(plan_counts(deep)["total"], 417)
        self.assertEqual(plan_counts(fast)["total"], 315)
        self.assertEqual(plan_counts(deep)["by_platform"], {"linkedin": 139, "indeed": 139, "glassdoor": 139})
        identities = {(x.platform, x.query.casefold(), x.age_days, x.remote_required) for x in deep}
        self.assertEqual(len(identities), len(deep))
        self.assertTrue(all(x.max_results is None and x.remote_required for x in deep + fast))

    def test_same_query_different_window_remains_distinct(self) -> None:
        strategy = copy.deepcopy(self.bundle.strategy)
        second = copy.deepcopy(next(x for x in strategy["lanes"] if x["id"] == "HEALTHCARE_INFO_QA"))
        second["id"] = "WINDOW_TEST"; second["allocation_percent"] = 0; second["core"] = False
        second["titles"] = ["Patient Enrollment Specialist"]; second["deep_days"] = 45
        strategy["lanes"].append(second)
        clone = ConfigBundle(self.bundle.root, strategy, self.bundle.candidate, self.bundle.runtime)
        tasks = compile_plan(clone, "deep", ["indeed"], include_fallback=True)
        windows = {x.age_days for x in tasks if x.query == "patient enrollment specialist"}
        self.assertEqual(windows, {30, 45})

    def test_authoritative_primary_urls_and_query_normalization(self) -> None:
        self.assertEqual(
            normalize_search_query("Customer Operations Coordinator — Healthcare"),
            "customer operations coordinator healthcare",
        )
        linkedin = urllib.parse.urlsplit(build_search_url("linkedin", "patient access specialist", 7))
        linkedin_query = urllib.parse.parse_qs(linkedin.query)
        self.assertEqual(linkedin_query["location"], ["United States"])
        self.assertEqual(linkedin_query["f_WT"], ["2"])
        self.assertEqual(linkedin_query["f_TPR"], ["r604800"])
        self.assertEqual(linkedin_query["sortBy"], ["DD"])
        indeed_query = urllib.parse.parse_qs(urllib.parse.urlsplit(build_search_url("indeed", "patient access specialist", 7)).query)
        self.assertEqual(indeed_query["l"], ["Remote"])
        self.assertEqual(indeed_query["fromage"], ["7"])
        self.assertEqual(indeed_query["sort"], ["date"])
        glassdoor = build_search_url("glassdoor", "patient access specialist", 7)
        self.assertIn("/Job/remote-patient-access-specialist-jobs-", glassdoor)

    def test_fast_execution_order_is_explicit_fastest_door(self) -> None:
        first = [task.query for task in compile_plan(self.bundle, "fast", ["linkedin"])[:6]]
        self.assertEqual(first, [
            "patient enrollment specialist", "patient enrollment coordinator",
            "healthcare enrollment specialist", "healthcare enrollment coordinator",
            "member enrollment specialist", "member enrollment coordinator",
        ])
        self.assertNotEqual(first[0], "clinical documentation specialist")

    def test_strategy_validator_rejects_allocation_drift(self) -> None:
        strategy = copy.deepcopy(self.bundle.strategy)
        strategy["lanes"][0]["allocation_percent"] = 34
        with self.assertRaises(ValueError): validate_strategy(strategy)


if __name__ == "__main__": unittest.main()
