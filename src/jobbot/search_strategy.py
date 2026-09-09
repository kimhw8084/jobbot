"""Search precision controls shared by planning, hydration, and reporting.

Bands are a retrieval ordering/cadence concern.  They never reject a card or
change the scoring thresholds used for application recommendations.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


BANDS = ("GOLD", "SILVER", "GROWTH", "HEDGE", "DEEP_TAIL")
BAND_ORDER = {name: index for index, name in enumerate(BANDS)}
DEFAULT_BAND_CADENCE_HOURS = {
    "GOLD": 6,
    "SILVER": 12,
    "GROWTH": 24,
    "HEDGE": 48,
    "DEEP_TAIL": 168,
}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").replace("—", " ").replace("–", " ").split()).casefold()


def _band_titles(strategy: Mapping[str, Any], key: str) -> set[str]:
    configured = strategy.get("strategy", {}).get("search_bands", {})
    return {_norm(value) for value in configured.get(f"{key.lower()}_titles", []) if _norm(value)}


def band_cadence_hours(strategy: Mapping[str, Any], band: str) -> int:
    configured = strategy.get("strategy", {}).get("search_band_cadence", {})
    value = configured.get(f"{band.lower()}_hours", DEFAULT_BAND_CADENCE_HOURS[band])
    return max(1, int(value))


def search_band(title: str, lane_id: str = "", strategy: Mapping[str, Any] | None = None) -> str:
    """Return the configured band without using lane priority as a proxy.

    Explicit title lists win.  Lane defaults only classify titles that have no
    explicit market-band assignment, so a broad care/credentialing title does
    not accidentally become a fastest-door query merely because it is in the
    healthcare operations lane.
    """
    strategy = strategy or {}
    normalized = _norm(title)
    for band in BANDS:
        if normalized in _band_titles(strategy, band):
            return band
    lane = _norm(lane_id)
    configured_hedge_lanes = {
        _norm(value) for value in strategy.get("strategy", {}).get("search_bands", {}).get("hedge_lanes", [])
    }
    if lane in configured_hedge_lanes or lane in {"higher_ed_edtech", "content_ai_quality"}:
        return "HEDGE"
    if lane in {"healthcare_quality_data", "healthcare_implementation"}:
        return "GROWTH"
    return "DEEP_TAIL"


def routed_resume_variant(strategy: Mapping[str, Any], lane: Mapping[str, Any]) -> tuple[str, str]:
    """Return an existing truthful variant and the documented routing reason."""
    lane_id = str(lane.get("id") or "")
    configured = strategy.get("strategy", {}).get("resume_routing", {}).get(lane_id)
    if isinstance(configured, Mapping):
        return str(configured.get("variant") or lane.get("resume_variant") or ""), str(configured.get("rationale") or "")
    if isinstance(configured, str):
        return configured, "configured strategy resume route"
    return str(lane.get("resume_variant") or ""), "lane-configured resume variant"


def query_definition(title: str, strategy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve canonical title, actual query text, and optional aliases."""
    strategy = strategy or {}
    canonical = str(title).strip()
    overrides = strategy.get("strategy", {}).get("query_text_overrides", {})
    override = overrides.get(canonical) or overrides.get(_norm(canonical))
    if isinstance(override, str):
        query_text, aliases = override, []
    elif isinstance(override, Mapping):
        query_text = str(override.get("query_text") or canonical)
        aliases = [str(item) for item in override.get("aliases", []) if str(item).strip()]
    else:
        query_text, aliases = canonical, []
    return {"canonical_title": canonical, "query_text": _norm(query_text), "aliases": tuple(aliases)}


def canonical_band_counts(tasks: list[Any] | tuple[Any, ...] | Any) -> dict[str, int]:
    result = {band: 0 for band in BANDS}
    for task in tasks:
        band = str(getattr(task, "search_band", "DEEP_TAIL") or "DEEP_TAIL").upper()
        result[band if band in result else "DEEP_TAIL"] += 1
    return result


def cadence_economics(counts: Mapping[str, int], *, baseline_recent_hours: int = 6,
                      baseline_deep_hours: int = 24) -> dict[str, Any]:
    """Estimate steady-state definition starts without reducing coverage."""
    baseline_daily = sum(int(counts.get(band, 0)) / baseline_recent_hours * 24 for band in BANDS)
    baseline_daily += sum(int(counts.get(band, 0)) / baseline_deep_hours * 24 for band in BANDS)
    band_daily = {
        band: (int(counts.get(band, 0)) / DEFAULT_BAND_CADENCE_HOURS[band] * 24)
        for band in BANDS
    }
    new_daily = sum(band_daily.values())
    reduction = 0.0 if baseline_daily <= 0 else max(0.0, 1.0 - new_daily / baseline_daily)
    return {
        "baseline_definitions_per_day": round(baseline_daily, 2),
        "band_definitions_per_day": {key: round(value, 2) for key, value in band_daily.items()},
        "steady_state_definitions_per_day": round(new_daily, 2),
        "theoretical_reduction_percent": round(reduction * 100, 1),
        "coverage_preserved": True,
    }


def detail_priority(*, title: str, query_text: str, search_band: str,
                    posted_age_days: float | None = None,
                    strategy: Mapping[str, Any] | None = None) -> tuple[int, str]:
    """Rank hydration only; every persisted card remains eligible eventually."""
    normalized_title = _norm(title)
    normalized_query = _norm(query_text)
    band = str(search_band or "DEEP_TAIL").upper()
    score = {"GOLD": 500, "SILVER": 400, "GROWTH": 300, "HEDGE": 200, "DEEP_TAIL": 100}.get(band, 100)
    reasons = [band.lower()]
    if normalized_query and normalized_query in normalized_title:
        score += 80
        reasons.append("title_matches_query")
    markers = (strategy or {}).get("strategy", {}).get("recall", {}).get("healthcare_title_markers", [])
    if any(_norm(marker) in normalized_title for marker in markers):
        score += 35
        reasons.append("healthcare_title")
    if any(marker in normalized_title for marker in ("bilingual", "spanish", "english/spanish")):
        score += 20
        reasons.append("bilingual")
    if posted_age_days is not None:
        if posted_age_days <= 3:
            score += 25
            reasons.append("fresh")
        elif posted_age_days > 14:
            score -= 15
            reasons.append("older")
    if re.search(r"\b(?:senior|staff|principal|director|vice president|vp)\b", normalized_title):
        score -= 80
        reasons.append("seniority_review")
    if re.search(r"\b(?:engineer|developer|sales|account executive|controller|finance)\b", normalized_title):
        score -= 160
        reasons.append("occupation_review")
    return max(0, int(score)), ";".join(reasons)


def _parse_dates(value: str | None) -> set[str]:
    import json
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        parsed = []
    return {str(item) for item in parsed if str(item)}


def wilson_lower_bound(successes: int, observations: int, z: float = 1.96) -> float:
    if observations <= 0:
        return 0.0
    p = max(0.0, min(1.0, successes / observations))
    denominator = 1 + z * z / observations
    centre = p + z * z / (2 * observations)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * observations)) / observations)
    return max(0.0, (centre - margin) / denominator)


def yield_is_eligible(row: Mapping[str, Any], *, minimum_descriptions: int = 30,
                      minimum_run_dates: int = 2) -> bool:
    return (int(row.get("completed_descriptions", 0) or 0) >= minimum_descriptions
            and len(_parse_dates(row.get("observation_dates"))) >= minimum_run_dates)


def query_yield_estimate(row: Mapping[str, Any], *, minimum_descriptions: int = 30,
                         minimum_run_dates: int = 2) -> dict[str, Any]:
    completed = int(row.get("completed_descriptions", 0) or 0)
    ready = int(row.get("apply_now", 0) or 0) + int(row.get("apply_volume", 0) or 0)
    eligible = yield_is_eligible(row, minimum_descriptions=minimum_descriptions, minimum_run_dates=minimum_run_dates)
    minutes = max(0.001, float(row.get("total_browser_ms", 0) or 0) / 60000.0)
    return {
        "apply_ready": ready,
        "apply_ready_rate": round(ready / completed, 4) if completed else 0.0,
        "apply_now_rate": round(int(row.get("apply_now", 0) or 0) / completed, 4) if completed else 0.0,
        "wilson_lower_bound": round(wilson_lower_bound(ready, completed), 4),
        "actionable_jobs_per_minute": round(ready / minutes, 3) if row.get("total_browser_ms") else 0.0,
        "sample_eligible": eligible,
        "sample_requirement": f">={minimum_descriptions} completed descriptions across >={minimum_run_dates} run dates",
    }


def next_due(completed_at: str, cadence_hours: int) -> str:
    moment = datetime.fromisoformat(completed_at).astimezone(timezone.utc)
    return (moment + timedelta(hours=max(1, int(cadence_hours)))).isoformat(timespec="seconds")
