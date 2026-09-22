from __future__ import annotations

import copy
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from jobbot.cli import command_search_plan, parser
from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle, validate_live_search, validate_strategy
from jobbot.search_plan import (
    build_search_url, compile_plan, compile_staged_plan, normalize_search_query,
    normalize_profile_query, plan_counts, write_plan,
)


class StrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = load_bundle(PROJECT_ROOT)

    def test_career_architecture_is_independent_of_live_query_budget(self) -> None:
        lanes = {x["id"]: x for x in self.bundle.strategy["lanes"] if x.get("core", True)}
        self.assertEqual(self.bundle.strategy["strategy"]["career_architecture"], {
            "healthcare_regulated_operations_data_quality": 55,
            "healthcare_analytics_project_implementation": 25,
            "education_learning_edtech": 15,
            "bilingual_ai_content_quality": 5,
        })
        self.assertEqual({k: int(v["allocation_percent"]) for k, v in lanes.items()}, {
            "HEALTHCARE_OPS_ACCESS": 35, "HEALTHCARE_INFO_QA": 15,
            "HEALTHCARE_QUALITY_DATA": 20, "HEALTHCARE_IMPLEMENTATION": 10,
            "HIGHER_ED_EDTECH": 15, "CONTENT_AI_QUALITY": 5,
        })
        profile = self.bundle.live_search["profile"]
        self.assertEqual(profile["id"], "jobbot-broad-qualified-yield")
        self.assertEqual(profile["version"], "2026-09-22.1")
        families = self.bundle.live_search["families"]
        self.assertEqual(len(families), 7)
        self.assertEqual([f["id"] for f in sorted(families, key=lambda x: x["initial_order"])], [
            "provider_lifecycle_credentialing_data",
            "healthcare_quality_data_documentation_compliance",
            "healthcare_project_implementation_program_support",
            "higher_ed_academic_back_office",
            "bounded_transferable_records_data_process_ops",
            "backoffice_patient_access_eligibility_enrollment",
            "bilingual_ai_content_quality",
        ])
        counts = plan_counts(compile_plan(self.bundle, "deep"))
        self.assertEqual(set(counts["by_family"]), {f["id"] for f in families})
        self.assertNotEqual(counts["by_family"], {k: v["allocation_percent"] for k, v in lanes.items()})

    def test_deep_plan_has_each_credible_family_on_every_platform(self) -> None:
        deep = compile_plan(self.bundle, "deep")
        families = {f["id"] for f in self.bundle.live_search["families"] if f.get("enabled", True) and f.get("minimum_deep_recall")}
        self.assertTrue(deep)
        for platform in ("linkedin", "indeed", "glassdoor"):
            with self.subTest(platform=platform):
                covered = {task.query_family for task in deep if task.platform == platform}
                self.assertEqual(covered, families)
                self.assertTrue(all(task.phase == "DEEP_BACKFILL" for task in deep if task.platform == platform))
        kinds = {platform: {task.query_kind for task in deep if task.platform == platform} for platform in ("linkedin", "indeed", "glassdoor")}
        self.assertEqual(kinds["linkedin"], {"intent", "title_family"})
        self.assertEqual(kinds["indeed"], {"exact_phrase", "compact_boolean"})
        self.assertEqual(kinds["glassdoor"], {"narrow_title"})

    def test_initial_order_prioritizes_calibration_seed_without_changing_deep_universe(self) -> None:
        fast = compile_plan(self.bundle, "fast")
        deep = compile_plan(self.bundle, "deep")
        ordered = [f["id"] for f in sorted(self.bundle.live_search["families"], key=lambda x: x["initial_order"])]
        self.assertEqual({t.query_family for t in fast}, set(ordered[:3]))
        for platform in ("linkedin", "indeed", "glassdoor"):
            rows = [t for t in fast if t.platform == platform]
            self.assertEqual([t.query_family for t in rows], sorted((t.query_family for t in rows), key=lambda family: ordered.index(family)))
        self.assertEqual({t.query_family for t in deep}, set(ordered))
        self.assertEqual([f["initial_order"] for f in sorted(self.bundle.live_search["families"], key=lambda x: x["initial_order"])], list(range(1, 8)))

    def test_cli_exposes_staged_search_plan(self) -> None:
        args = parser().parse_args(["search-plan", "--mode", "staged"])
        self.assertEqual(args.mode, "staged")
        self.assertIs(args.func, command_search_plan)

    def test_plan_is_deterministic_and_task_identity_keeps_existing_semantics(self) -> None:
        first = compile_plan(self.bundle, "deep")
        second = compile_plan(self.bundle, "deep")
        self.assertEqual(first, second)
        identities = {(x.platform, x.query.casefold(), x.age_days, x.remote_required) for x in first}
        self.assertEqual(len(identities), len(first))
        self.assertTrue(all(x.max_results is None and x.remote_required for x in first))
        self.assertTrue(all(x.task_key for x in first))
        linked_boolean = next(task for task in first if task.platform == "linkedin" and task.query_kind == "title_family")
        self.assertIn(" OR ", linked_boolean.query)
        self.assertEqual(normalize_profile_query('  "provider enrollment" OR   credentialing  '), '"provider enrollment" OR credentialing')

    def test_same_exact_query_different_window_remains_distinct(self) -> None:
        strategy = copy.deepcopy(self.bundle.strategy)
        live_search = copy.deepcopy(self.bundle.live_search)
        second = copy.deepcopy(live_search["families"][0])
        shared_query = second["linkedin_intent_queries"][0]
        second["id"] = "WINDOW_TEST"
        second["initial_order"] = 99
        second["deep_days"] = 45
        second["linkedin_intent_queries"] = [shared_query]
        second["linkedin_title_queries"] = ["window test title"]
        second["indeed_phrase_queries"] = ["window test phrase"]
        second["indeed_boolean_queries"] = ["window test boolean"]
        second["glassdoor_title_queries"] = ["window test glassdoor"]
        live_search["families"].append(second)
        clone = ConfigBundle(self.bundle.root, strategy, self.bundle.candidate, self.bundle.runtime, live_search)
        tasks = compile_plan(clone, "deep", ["linkedin"], priority_max=99)
        windows = {task.age_days for task in tasks if task.query == shared_query}
        self.assertEqual(windows, {30, 45})

    def test_plan_export_exposes_profile_family_pass_and_exact_query(self) -> None:
        tasks = compile_plan(self.bundle, "deep", ["indeed"])
        with tempfile.TemporaryDirectory() as temp:
            paths = write_plan(tasks, Path(temp), "deep")
            payload = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(payload["tasks"][0]["strategy_profile"], self.bundle.live_search["profile"]["id"])
            self.assertIn("query_family", payload["tasks"][0])
            self.assertIn("query_kind", payload["tasks"][0])
            self.assertIn("query_pass", payload["tasks"][0])
            self.assertIn("exact query", paths["html"].read_text(encoding="utf-8").lower())
            csv_text = paths["csv"].read_text(encoding="utf-8-sig")
            self.assertIn("query_family", csv_text.splitlines()[0])
            self.assertIn(tasks[0].query, csv_text)

    def test_authoritative_primary_urls_and_query_normalization(self) -> None:
        self.assertEqual(normalize_search_query("Customer Operations Coordinator — Healthcare"), "customer operations coordinator healthcare")
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
        self.assertIn("/Job/remote-patient-access-specialist-jobs-", build_search_url("glassdoor", "patient access specialist", 7))

    def test_strategy_validator_rejects_career_architecture_drift(self) -> None:
        strategy = copy.deepcopy(self.bundle.strategy)
        strategy["lanes"][0]["allocation_percent"] = 34
        with self.assertRaises(ValueError):
            validate_strategy(strategy)

    def test_live_profile_validator_preserves_each_platform_pass(self) -> None:
        profile = copy.deepcopy(self.bundle.live_search)
        profile["families"][0]["glassdoor_title_queries"] = ["provider enrollment specialist"]
        with self.assertRaisesRegex(ValueError, "at least 2 glassdoor queries"):
            validate_live_search(profile, self.bundle.strategy)
        profile = copy.deepcopy(self.bundle.live_search)
        for family in profile["families"]:
            family["enabled"] = False
        with self.assertRaisesRegex(ValueError, "at least one enabled family"):
            validate_live_search(profile, self.bundle.strategy)


if __name__ == "__main__":
    unittest.main()
