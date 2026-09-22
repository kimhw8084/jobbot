from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigError(ValueError):
    """Raised when executable configuration is incomplete or contradictory."""


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        value = tomllib.load(handle)
    if not isinstance(value, dict):
        raise ConfigError(f"TOML root must be a table: {path}")
    return value


def resolve_path(value: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(frozen=True)
class ConfigBundle:
    root: Path
    strategy: dict[str, Any]
    candidate: dict[str, Any]
    runtime: dict[str, Any]
    live_search: dict[str, Any] = field(default_factory=dict)

    @property
    def database_path(self) -> Path:
        return resolve_path(self.runtime["runtime"]["database_path"], root=self.root)

    @property
    def output_dir(self) -> Path:
        return resolve_path(self.runtime["runtime"]["output_dir"], root=self.root)

    @property
    def crawl_observations_path(self) -> Path:
        return resolve_path(self.runtime["runtime"].get("crawl_observations_path", "data/crawl_observations.sqlite3"), root=self.root)

    def legacy_runtime(self) -> dict[str, Any]:
        """Compatibility shape for the retained, tested v3.1 ledger/fetch engine."""
        cfg = copy.deepcopy(self.runtime)
        cfg["_base"] = str(self.root)
        run = cfg.setdefault("run", {})
        runtime = cfg["runtime"]
        run.update({
            "strategy_file": "config/strategy.toml",
            "db_path": runtime["database_path"],
            "output_dir": runtime["output_dir"],
            "cache_dir": runtime["cache_dir"],
            "cache_minutes": int(runtime.get("cache_minutes", 0)),
            "http_timeout_seconds": runtime["http_timeout_seconds"],
            "user_agent": runtime["user_agent"],
            "safety_max_jobs_per_source": runtime["safety_max_jobs_per_source"],
        })
        cfg["app"] = dict(run)
        cfg["candidate"] = copy.deepcopy(self.candidate["candidate"])
        cfg["candidate"]["capabilities"] = copy.deepcopy(self.candidate.get("capabilities", []))
        for key in ("sources", "ats_runtime", "ats_discovery", "enrichment", "ledger", "coverage"):
            if key in self.runtime:
                cfg[key] = copy.deepcopy(self.runtime[key])
        return cfg


def _legacy_searches(strategy: dict[str, Any]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for lane in strategy.get("lanes", []):
        if not lane.get("enabled", True):
            continue
        profiles.append({
            "name": lane["profile"],
            "enabled": True,
            "priority": int(lane["priority"]),
            "career_lane": lane["id"],
            "resume_variant": lane["resume_variant"],
            "domain": lane["domain"],
            "keywords": list(lane.get("titles", [])),
            "generic_title_patterns": list(lane.get("generic_title_patterns", [])),
            "domain_markers": list(lane.get("domain_markers", [])),
            "responsibility_markers": list(lane.get("responsibility_markers", [])),
            "normal_freshness_hours": int(lane["fast_days"]) * 24,
            "bootstrap_backfill_days": int(lane["deep_days"]),
        })
    return profiles


def load_bundle(root: Path | None = None) -> ConfigBundle:
    base = (root or PROJECT_ROOT).resolve()
    strategy = load_toml(base / "config" / "strategy.toml")
    strategy["searches"] = _legacy_searches(strategy)
    live_search = load_toml(base / "config" / "live_search.toml")
    candidate_raw = load_toml(base / "config" / "candidate.toml")
    runtime = load_toml(base / "config" / "runtime.toml")
    if os.environ.get("JOBBOT_DATABASE_PATH"):
        runtime["runtime"]["database_path"] = os.environ["JOBBOT_DATABASE_PATH"]
    if os.environ.get("JOBBOT_OUTPUT_DIR"):
        runtime["runtime"]["output_dir"] = os.environ["JOBBOT_OUTPUT_DIR"]
    if os.environ.get("JOBBOT_CRAWL_OBSERVATIONS_PATH"):
        runtime["runtime"]["crawl_observations_path"] = os.environ["JOBBOT_CRAWL_OBSERVATIONS_PATH"]
    if os.environ.get("JOBBOT_DASHBOARD_PORT"):
        runtime["runtime"]["dashboard_port"] = int(os.environ["JOBBOT_DASHBOARD_PORT"])
    validate_strategy(strategy)
    validate_live_search(live_search, strategy)
    strategy["_live_search_profile"] = copy.deepcopy(live_search)
    validate_candidate(candidate_raw)
    validate_runtime(runtime)
    return ConfigBundle(base, strategy, candidate_raw, runtime, live_search)


def validate_strategy(strategy: dict[str, Any]) -> None:
    meta = strategy.get("strategy", {})
    if meta.get("version") != "3.2.0":
        raise ConfigError("strategy.version must be 3.2.0")
    lanes = strategy.get("lanes", [])
    core = [lane for lane in lanes if lane.get("core", True) and lane.get("enabled", True)]
    allocations = {lane["id"]: int(lane["allocation_percent"]) for lane in core}
    expected = {
        "HEALTHCARE_OPS_ACCESS": 35,
        "HEALTHCARE_INFO_QA": 15,
        "HEALTHCARE_QUALITY_DATA": 20,
        "HEALTHCARE_IMPLEMENTATION": 10,
        "HIGHER_ED_EDTECH": 15,
        "CONTENT_AI_QUALITY": 5,
    }
    if allocations != expected:
        raise ConfigError(f"career lane allocations must preserve the 55/25/15/5 architecture; got {allocations}")
    if sum(allocations.values()) != 100:
        raise ConfigError("career architecture allocations must total 100")
    architecture = meta.get("career_architecture", {})
    expected_architecture = {
        "healthcare_regulated_operations_data_quality": 55,
        "healthcare_analytics_project_implementation": 25,
        "education_learning_edtech": 15,
        "bilingual_ai_content_quality": 5,
    }
    if architecture != expected_architecture:
        raise ConfigError(f"career architecture must remain 55/25/15/5; got {architecture}")
    grouped: dict[str, int] = {}
    for lane in core:
        group = str(lane.get("career_group", ""))
        if group not in architecture:
            raise ConfigError(f"unknown career group for lane {lane['id']}: {group!r}")
        grouped[group] = grouped.get(group, 0) + int(lane["allocation_percent"])
    if grouped != architecture:
        raise ConfigError(f"lane allocations do not match career architecture: {grouped}")
    seen_ids: set[str] = set()
    for lane in lanes:
        lane_id = str(lane.get("id", ""))
        if not lane_id or lane_id in seen_ids:
            raise ConfigError(f"duplicate or missing lane id: {lane_id!r}")
        seen_ids.add(lane_id)
        if lane.get("enabled", True) and not lane.get("titles"):
            raise ConfigError(f"enabled lane has no titles: {lane_id}")
        if int(lane.get("fast_days", 0)) <= 0 or int(lane.get("deep_days", 0)) <= 0:
            raise ConfigError(f"invalid age window for lane {lane_id}")
    if meta.get("production_max_results") is not None:
        raise ConfigError("production_max_results must be null/unlimited")


def validate_live_search(profile: dict[str, Any], strategy: dict[str, Any]) -> None:
    meta = profile.get("profile", {})
    if not str(meta.get("id", "")).strip() or not str(meta.get("version", "")).strip():
        raise ConfigError("live-search profile requires a stable id and version")
    if meta.get("remote_required") is not True:
        raise ConfigError("live-search profile must remain remote-only")
    if not any(family.get("enabled", True) for family in profile.get("families", [])):
        raise ConfigError("live-search profile requires at least one enabled family")
    lanes = {str(lane["id"]): lane for lane in strategy.get("lanes", [])}
    family_ids: set[str] = set()
    orders: set[int] = set()
    exact_queries: set[tuple[str, str]] = set()
    for family in profile.get("families", []):
        family_id = str(family.get("id", ""))
        if not family_id or family_id in family_ids:
            raise ConfigError(f"duplicate or missing live query family id: {family_id!r}")
        family_ids.add(family_id)
        if not family.get("enabled", True):
            continue
        if family.get("minimum_deep_recall") is not True:
            raise ConfigError(f"enabled query family lacks minimum deep recall: {family_id}")
        if int(family.get("initial_order", 0)) <= 0 or int(family["initial_order"]) in orders:
            raise ConfigError(f"live query family order must be unique and positive: {family_id}")
        orders.add(int(family["initial_order"]))
        lane_id = str(family.get("career_lane", ""))
        if lane_id not in lanes:
            raise ConfigError(f"live query family maps to unknown career lane: {family_id} -> {lane_id}")
        if not lanes[lane_id].get("enabled", True):
            raise ConfigError(f"live query family maps to a disabled career lane: {family_id} -> {lane_id}")
        if int(family.get("fast_days", 0)) <= 0 or int(family.get("deep_days", 0)) <= 0:
            raise ConfigError(f"live query family has invalid age windows: {family_id}")
        query_groups = (
            ("linkedin", "linkedin_intent_queries", 1),
            ("linkedin", "linkedin_title_queries", 1),
            ("indeed", "indeed_phrase_queries", 2),
            ("indeed", "indeed_boolean_queries", 1),
            ("glassdoor", "glassdoor_title_queries", 2),
        )
        for platform, field_name, minimum_queries in query_groups:
            queries = family.get(field_name, [])
            if len(queries) < minimum_queries:
                raise ConfigError(f"enabled query family needs at least {minimum_queries} {platform} queries in {field_name}: {family_id}")
            for query in queries:
                exact = " ".join(str(query).split())
                identity = (platform, exact.casefold())
                if not exact or len(exact) > 220:
                    raise ConfigError(f"empty or oversized {platform} query in {family_id}")
                if identity in exact_queries:
                    raise ConfigError(f"duplicate live query causes ambiguous family provenance: {platform}: {exact}")
                exact_queries.add(identity)


def validate_candidate(data: dict[str, Any]) -> None:
    candidate = data.get("candidate", {})
    for field in ("name", "country", "state", "professional_positioning"):
        if not str(candidate.get(field, "")).strip():
            raise ConfigError(f"candidate.{field} is required")
    allowed = {"proven", "strong_transfer", "training", "not_evidenced"}
    names: set[str] = set()
    for cap in data.get("capabilities", []):
        name = str(cap.get("name", "")).strip()
        level = cap.get("level")
        if not name or name in names or level not in allowed:
            raise ConfigError(f"invalid capability: {cap}")
        names.add(name)


def validate_runtime(data: dict[str, Any]) -> None:
    runtime = data.get("runtime", {})
    if runtime.get("bridge_host") != "127.0.0.1":
        raise ConfigError("bridge_host must be 127.0.0.1")
    if int(runtime.get("heartbeat_seconds", 0)) not in range(15, 31):
        raise ConfigError("heartbeat_seconds must be between 15 and 30")
