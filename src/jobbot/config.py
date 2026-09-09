from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .search_strategy import BANDS, DEFAULT_BAND_CADENCE_HOURS, classification_only_titles, deep_cadence_hours, routed_resume_variant


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

    @property
    def database_path(self) -> Path:
        return resolve_path(self.runtime["runtime"]["database_path"], root=self.root)

    @property
    def output_dir(self) -> Path:
        return resolve_path(self.runtime["runtime"]["output_dir"], root=self.root)

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
        resume_variant, resume_route_reason = routed_resume_variant(strategy, lane)
        profiles.append({
            "name": lane["profile"],
            "enabled": True,
            "priority": int(lane["priority"]),
            "career_lane": lane["id"],
            "resume_variant": resume_variant,
            "resume_route_reason": resume_route_reason,
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
    candidate_raw = load_toml(base / "config" / "candidate.toml")
    runtime = load_toml(base / "config" / "runtime.toml")
    if os.environ.get("JOBBOT_DATABASE_PATH"):
        runtime["runtime"]["database_path"] = os.environ["JOBBOT_DATABASE_PATH"]
    if os.environ.get("JOBBOT_OUTPUT_DIR"):
        runtime["runtime"]["output_dir"] = os.environ["JOBBOT_OUTPUT_DIR"]
    if os.environ.get("JOBBOT_DASHBOARD_PORT"):
        runtime["runtime"]["dashboard_port"] = int(os.environ["JOBBOT_DASHBOARD_PORT"])
    validate_strategy(strategy)
    validate_candidate(candidate_raw)
    validate_runtime(runtime)
    return ConfigBundle(base, strategy, candidate_raw, runtime)


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
        raise ConfigError(f"core portfolio must be exactly 35/15/20/10/15/5; got {allocations}")
    if sum(allocations.values()) != 100:
        raise ConfigError("core portfolio allocation must total 100")
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
    bands = meta.get("search_bands", {})
    cadence = meta.get("search_band_cadence", {})
    lane_titles = {" ".join(str(value).replace("—", " ").replace("–", " ").split()).casefold()
                   for lane in lanes if lane.get("enabled", True) for value in lane.get("titles", [])}
    configured_titles: set[str] = set()
    for band in BANDS:
        if int(cadence.get(f"{band.lower()}_hours", DEFAULT_BAND_CADENCE_HOURS[band])) <= 0:
            raise ConfigError(f"search band cadence must be positive: {band}")
        titles = bands.get(f"{band.lower()}_titles", [])
        if not isinstance(titles, list):
            raise ConfigError(f"search band title list must be an array: {band}")
        normalized = {" ".join(str(value).replace("—", " ").replace("–", " ").split()).casefold() for value in titles}
        overlap = configured_titles & normalized
        if overlap:
            raise ConfigError(f"search band title appears in multiple bands: {sorted(overlap)}")
        configured_titles.update(normalized)
    classification_only = classification_only_titles(strategy)
    orphan = configured_titles - lane_titles - classification_only
    if orphan:
        raise ConfigError(f"search band titles are not executable or classification_only: {sorted(orphan)}")
    if not classification_only <= configured_titles:
        raise ConfigError("classification_only_titles must be present in an explicit band")
    if deep_cadence_hours(strategy) <= 0:
        raise ConfigError("deep backfill cadence must be positive")
    for title, override in meta.get("query_text_overrides", {}).items():
        if isinstance(override, dict):
            variants = override.get("query_variants", [])
            if not isinstance(variants, list):
                raise ConfigError(f"query_variants must be an array: {title}")
            seen_queries = set()
            for variant in variants:
                query = variant.get("query_text") if isinstance(variant, dict) else variant
                normalized_query = " ".join(str(query or "").replace("—", " ").replace("–", " ").split()).casefold()
                if not normalized_query or normalized_query in seen_queries:
                    raise ConfigError(f"duplicate/empty executable query variant: {title}")
                seen_queries.add(normalized_query)
    routing = meta.get("resume_routing", {})
    for lane in lanes:
        route = routing.get(str(lane.get("id")))
        if route is not None and not isinstance(route, (str, dict)):
            raise ConfigError(f"invalid resume routing for {lane.get('id')}")


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
