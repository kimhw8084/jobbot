from __future__ import annotations

import csv
import hashlib
import html
import json
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import ConfigBundle


PLATFORM_ORDER = {"linkedin": 0, "indeed": 1, "glassdoor": 2}


@dataclass(frozen=True)
class SearchTask:
    task_key: str
    strategy_profile: str
    strategy_profile_version: str
    query_family: str
    query_kind: str
    query_pass: str
    platform: str
    lane: str
    career_lane: str
    lane_label: str
    allocation_percent: int
    profile: str
    priority: int
    initial_order: int
    career_priority: int
    execution_rank: int
    query: str
    remote_required: bool
    age_days: int
    sort_mode: str
    enabled: bool
    resume_variant: str
    search_url: str
    phase: str
    max_results: None = None


def normalize_search_query(title: str) -> str:
    """Turn a canonical strategy title into natural platform search text."""
    return " ".join(str(title).replace("—", " ").replace("–", " ").split()).casefold()


def normalize_profile_query(query: str) -> str:
    """Collapse profile-query whitespace while preserving exact Boolean syntax."""
    return " ".join(str(query).split())


def build_search_url(platform: str, query: str, age_days: int) -> str:
    encoded = urllib.parse.quote_plus(query)
    if platform == "linkedin":
        seconds = max(1, age_days) * 86400
        return (
            "https://www.linkedin.com/jobs/search/?"
            f"f_TPR=r{seconds}&f_WT=2&keywords={encoded}&location=United+States&sortBy=DD"
        )
    if platform == "indeed":
        return f"https://www.indeed.com/jobs?q={encoded}&l=Remote&fromage={max(1, age_days)}&sort=date"
    if platform == "glassdoor":
        slug = "-".join(query.lower().replace("&", " and ").replace("—", " ").split())
        end = 7 + len(query)
        return f"https://www.glassdoor.com/Job/remote-{urllib.parse.quote(slug)}-jobs-SRCH_IL.0,6_IS11047_KO7,{end}.htm"
    raise ValueError(f"unsupported platform: {platform}")


def _task_key(platform: str, query: str, days: int) -> str:
    raw = f"{platform}|{query.casefold().strip()}|{days}|remote"
    return "T" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18].upper()


def compile_plan(
    bundle: ConfigBundle,
    mode: str,
    platforms: Iterable[str] | None = None,
    *,
    include_fallback: bool = False,
    priority_min: int = 0,
    priority_max: int | None = None,
    phase: str | None = None,
) -> list[SearchTask]:
    if mode not in {"fast", "deep"}:
        raise ValueError("mode must be fast or deep")
    selected = tuple(platforms or bundle.strategy["strategy"]["primary_platforms"])
    invalid = sorted(set(selected) - set(PLATFORM_ORDER))
    if invalid:
        raise ValueError(f"unsupported platform(s): {', '.join(invalid)}")
    live_search = bundle.live_search or bundle.strategy.get("_live_search_profile", {})
    profile_meta = live_search.get("profile", {})
    families = live_search.get("families", [])
    max_priority = int(
        profile_meta.get("fast_max_initial_order", 3) if mode == "fast" and priority_max is None
        else (max((int(f.get("initial_order", 0)) for f in families), default=0) if priority_max is None else priority_max)
    )
    phase_name = phase or ("FAST_RECENT" if mode == "fast" else "DEEP_BACKFILL")
    seen: set[tuple[str, str, int, bool]] = set()
    tasks: list[SearchTask] = []
    lanes = {str(lane["id"]): lane for lane in bundle.strategy.get("lanes", [])}
    for family in sorted(families, key=lambda value: (int(value.get("initial_order", 99)), str(value.get("id", "")))):
        if not family.get("enabled", True):
            continue
        family_order = int(family["initial_order"])
        if family_order < priority_min or family_order > max_priority:
            continue
        lane = lanes.get(str(family.get("career_lane", "")))
        if lane is None or not lane.get("enabled", True):
            continue
        age_days = int(family[f"{mode}_days"])
        platform_passes = {
            "linkedin": (
                ("intent", "linkedin_intent", family.get("linkedin_intent_queries", [])),
                ("title_family", "linkedin_title_family", family.get("linkedin_title_queries", [])),
            ),
            "indeed": (
                ("exact_phrase", "indeed_exact_phrase", family.get("indeed_phrase_queries", [])),
                ("compact_boolean", "indeed_compact_boolean", family.get("indeed_boolean_queries", [])),
            ),
            "glassdoor": (
                ("narrow_title", "glassdoor_narrow_title", family.get("glassdoor_title_queries", [])),
            ),
        }
        for platform in selected:
            for pass_rank, (query_kind, query_pass, queries) in enumerate(platform_passes[platform], start=1):
                for query_rank, raw_query in enumerate(queries, start=1):
                    query = normalize_profile_query(str(raw_query))
                    if not query:
                        continue
                    identity = (platform, query.casefold(), age_days, bool(profile_meta.get("remote_required", True)))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    tasks.append(SearchTask(
                        task_key=_task_key(platform, query, age_days),
                        strategy_profile=str(profile_meta["id"]),
                        strategy_profile_version=str(profile_meta["version"]),
                        query_family=str(family["id"]), query_kind=query_kind, query_pass=query_pass,
                        platform=platform, lane=str(lane["id"]), career_lane=str(lane["id"]),
                        lane_label=str(lane["label"]), allocation_percent=int(lane["allocation_percent"]),
                        profile=str(lane["profile"]), priority=family_order, initial_order=family_order,
                        career_priority=int(lane["priority"]),
                        execution_rank=family_order * 10000 + pass_rank * 1000 + query_rank,
                        query=query, remote_required=bool(profile_meta.get("remote_required", True)),
                        age_days=age_days, sort_mode="newest", enabled=True,
                        resume_variant=str(lane["resume_variant"]),
                        search_url=build_search_url(platform, query, age_days), phase=phase_name, max_results=None,
                    ))
    tasks.sort(key=lambda x: (PLATFORM_ORDER[x.platform], x.execution_rank, x.query_family, x.query.casefold(), x.age_days))
    return tasks


def compile_staged_plan(
    bundle: ConfigBundle,
    platforms: Iterable[str] | None = None,
    phases: Iterable[str] | None = None,
    *,
    include_fallback: bool = False,
) -> list[SearchTask]:
    """Compile the active live-search profile as explicit recent and deep phases.

    Fast phases follow the profile's initial calibration order. The deep phase
    always includes every enabled family marked for minimum recall, independent
    of that initial order or the durable career allocation percentages.
    """
    selected = tuple(platforms or bundle.strategy["strategy"]["primary_platforms"])
    live_search = bundle.live_search or bundle.strategy.get("_live_search_profile", {})
    fast_limit = int(live_search.get("profile", {}).get("fast_max_initial_order", 3))
    last_order = max((int(f.get("initial_order", 0)) for f in live_search.get("families", [])), default=0)
    phase_a = compile_plan(bundle, "fast", selected, priority_max=fast_limit, phase="A_FASTEST_DOOR_RECENT")
    phase_b = compile_plan(
        bundle, "fast", selected, priority_min=fast_limit + 1, priority_max=last_order,
        phase="B_REMAINING_CORE_RECENT",
    )
    phase_c = compile_plan(bundle, "deep", selected, include_fallback=include_fallback, phase="C_DEEP_BACKFILL")
    phase_order = {"A_FASTEST_DOOR_RECENT": 0, "B_REMAINING_CORE_RECENT": 1, "C_DEEP_BACKFILL": 2}
    selected_phases = set(phases or phase_order)
    return sorted(
        [task for task in phase_a + phase_b + phase_c if task.phase in selected_phases],
        key=lambda task: (phase_order[task.phase], PLATFORM_ORDER[task.platform], task.execution_rank, task.priority, task.lane, task.query.casefold(), task.age_days),
    )


def plan_counts(tasks: Iterable[SearchTask]) -> dict[str, Any]:
    by_lane: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_family: dict[str, int] = {}
    by_pass: dict[str, int] = {}
    by_platform_family_pass: dict[str, dict[str, int]] = {}
    lane_platform: dict[str, dict[str, int]] = {}
    total = 0
    for task in tasks:
        total += 1
        by_lane[task.lane] = by_lane.get(task.lane, 0) + 1
        by_platform[task.platform] = by_platform.get(task.platform, 0) + 1
        by_family[task.query_family] = by_family.get(task.query_family, 0) + 1
        by_pass[task.query_pass] = by_pass.get(task.query_pass, 0) + 1
        platform_families = by_platform_family_pass.setdefault(task.platform, {})
        key = f"{task.query_family}/{task.query_pass}/{task.phase}"
        platform_families[key] = platform_families.get(key, 0) + 1
        lane_counts = lane_platform.setdefault(task.lane, {})
        lane_counts[task.platform] = lane_counts.get(task.platform, 0) + 1
    return {
        "total": total, "by_lane": by_lane, "by_platform": by_platform,
        "by_family": by_family, "by_pass": by_pass,
        "platform_family_pass": by_platform_family_pass, "lane_platform": lane_platform,
    }


def write_plan(tasks: list[SearchTask], output_dir: Path, mode: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "search_plan.json"
    csv_path = output_dir / "search_plan.csv"
    html_path = output_dir / "search_plan.html"
    first = tasks[0] if tasks else None
    payload = {
        "version": "3.2.1", "mode": mode,
        "strategy_profile": first.strategy_profile if first else "",
        "strategy_profile_version": first.strategy_profile_version if first else "",
        "counts": plan_counts(tasks), "tasks": [asdict(t) for t in tasks],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(tasks[0]).keys()) if tasks else list(SearchTask.__dataclass_fields__))
        writer.writeheader()
        for task in tasks:
            row = asdict(task)
            for key, value in row.items():
                if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
                    row[key] = "'" + value
            writer.writerow(row)
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else 'UNLIMITED'))}</td>" for value in (
            task.phase, task.strategy_profile, task.strategy_profile_version, task.query_family,
            f"{task.query_kind}/{task.query_pass}", task.platform, task.query, task.career_lane,
            f"{task.allocation_percent}% career", "Remote" if task.remote_required else "Any",
            f"{task.age_days} days", f"{task.priority}/{task.initial_order}", task.execution_rank,
            task.resume_variant, task.search_url, task.max_results,
        )) + "</tr>" for task in tasks
    )
    counts = plan_counts(tasks)
    platform_pills = "".join(
        f'<span class="pill">{html.escape(platform)}: {count}</span>'
        for platform, count in counts["by_platform"].items()
    )
    html_path.write_text(f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>JobBot Search Plan</title>
<style>body{{font:14px system-ui;margin:24px;color:#14213d}}h1{{margin-bottom:4px}}.summary{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}.pill{{background:#edf4ff;border-radius:999px;padding:8px 12px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#fff}}tr:nth-child(even){{background:#fafafa}}</style></head><body><h1>JobBot v3.2 Search Plan — {html.escape(mode)}</h1><p>Every production task is remote-only and unlimited by result count. Total tasks: <strong>{counts['total']}</strong>.</p><div class=\"summary\">{platform_pills}</div>
<table><thead><tr><th>Phase</th><th>Strategy profile</th><th>Version</th><th>Query family</th><th>Query kind/pass</th><th>Platform</th><th>Exact query</th><th>Career lane</th><th>Career allocation</th><th>Remote condition</th><th>Age window</th><th>Priority/order</th><th>Execution rank</th><th>Resume route</th><th>Search URL</th><th>Max results</th></tr></thead><tbody>{rows}</tbody></table></body></html>""", encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "html": html_path}


def compile_and_write(bundle: ConfigBundle, mode: str, platforms: Iterable[str] | None = None, *, open_browser: bool = False, include_fallback: bool = False) -> tuple[list[SearchTask], dict[str, Path]]:
    tasks = compile_plan(bundle, mode, platforms, include_fallback=include_fallback)
    paths = write_plan(tasks, bundle.output_dir, mode)
    if open_browser:
        webbrowser.open(paths["html"].as_uri())
    return tasks, paths


def compile_staged_and_write(bundle: ConfigBundle, platforms: Iterable[str] | None = None, *, open_browser: bool = False, include_fallback: bool = False) -> tuple[list[SearchTask], dict[str, Path]]:
    tasks = compile_staged_plan(bundle, platforms, include_fallback=include_fallback)
    paths = write_plan(tasks, bundle.output_dir, "staged")
    if open_browser:
        webbrowser.open(paths["html"].as_uri())
    return tasks, paths
