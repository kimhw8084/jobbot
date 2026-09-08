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
    platform: str
    lane: str
    lane_label: str
    allocation_percent: int
    profile: str
    priority: int
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
    mode_cfg = bundle.strategy["strategy"]["run_modes"][mode]
    max_priority = int(mode_cfg["max_priority"] if priority_max is None else priority_max)
    phase_name = phase or ("FAST_RECENT" if mode == "fast" else "DEEP_BACKFILL")
    seen: set[tuple[str, str, int, bool]] = set()
    tasks: list[SearchTask] = []
    execution_cfg = bundle.strategy.get("strategy", {}).get("execution", {})
    fast_prefix = {
        normalize_search_query(title): index
        for index, title in enumerate(execution_cfg.get("fast_first_queries", []), start=1)
    }
    for lane in bundle.strategy.get("lanes", []):
        if not lane.get("enabled", True):
            continue
        if not lane.get("core", True) and not include_fallback:
            continue
        if (int(lane["priority"]) < priority_min or int(lane["priority"]) > max_priority) and lane.get("core", True):
            continue
        age_days = int(lane[f"{mode}_days"])
        for title in lane.get("titles", []):
            query = normalize_search_query(str(title))
            for platform in selected:
                identity = (platform, query.casefold(), age_days, True)
                if identity in seen:
                    continue
                seen.add(identity)
                tasks.append(SearchTask(
                    task_key=_task_key(platform, query, age_days), platform=platform,
                    lane=str(lane["id"]), lane_label=str(lane["label"]),
                    allocation_percent=int(lane["allocation_percent"]), profile=str(lane["profile"]),
                    priority=int(lane["priority"]), query=query, remote_required=True,
                    execution_rank=(fast_prefix.get(query, 1000 + int(lane.get("execution_rank", 99)) * 100 + len(tasks))),
                    age_days=age_days, sort_mode="newest", enabled=True,
                    resume_variant=str(lane["resume_variant"]),
                    search_url=build_search_url(platform, query, age_days), phase=phase_name, max_results=None,
                ))
    tasks.sort(key=lambda x: (PLATFORM_ORDER[x.platform], x.execution_rank, x.priority, x.lane, x.query.casefold(), x.age_days))
    return tasks


def compile_staged_plan(bundle: ConfigBundle, platforms: Iterable[str] | None = None, phases: Iterable[str] | None = None) -> list[SearchTask]:
    """Compile the complete primary production cycle as explicit phases.

    Phase A preserves the researched fastest-door order. Phase B fills the
    remaining enabled core titles at their recent window. Phase C repeats all
    enabled core titles at each lane's deep window; durable dedupe/versioning
    is expected to absorb the intentional overlap.
    """
    selected = tuple(platforms or bundle.strategy["strategy"]["primary_platforms"])
    phase_a = compile_plan(bundle, "fast", selected, phase="A_FASTEST_DOOR_RECENT")
    phase_b = compile_plan(
        bundle, "fast", selected, priority_min=2, priority_max=3,
        phase="B_REMAINING_CORE_RECENT",
    )
    phase_c = compile_plan(bundle, "deep", selected, phase="C_DEEP_BACKFILL")
    phase_order = {"A_FASTEST_DOOR_RECENT": 0, "B_REMAINING_CORE_RECENT": 1, "C_DEEP_BACKFILL": 2}
    selected_phases = set(phases or phase_order)
    return sorted(
        [task for task in phase_a + phase_b + phase_c if task.phase in selected_phases],
        key=lambda task: (phase_order[task.phase], PLATFORM_ORDER[task.platform], task.execution_rank, task.priority, task.lane, task.query.casefold(), task.age_days),
    )


def plan_counts(tasks: Iterable[SearchTask]) -> dict[str, Any]:
    by_lane: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    lane_platform: dict[str, dict[str, int]] = {}
    total = 0
    for task in tasks:
        total += 1
        by_lane[task.lane] = by_lane.get(task.lane, 0) + 1
        by_platform[task.platform] = by_platform.get(task.platform, 0) + 1
        lane_counts = lane_platform.setdefault(task.lane, {})
        lane_counts[task.platform] = lane_counts.get(task.platform, 0) + 1
    return {"total": total, "by_lane": by_lane, "by_platform": by_platform, "lane_platform": lane_platform}


def write_plan(tasks: list[SearchTask], output_dir: Path, mode: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "search_plan.json"
    csv_path = output_dir / "search_plan.csv"
    html_path = output_dir / "search_plan.html"
    payload = {"version": "3.2.1", "mode": mode, "counts": plan_counts(tasks), "tasks": [asdict(t) for t in tasks]}
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
            task.phase, task.lane, f"{task.allocation_percent}%", task.profile, task.platform, task.query,
            "Remote" if task.remote_required else "Any", f"{task.age_days} days", task.priority,
            task.execution_rank, "enabled" if task.enabled else "disabled", task.resume_variant, task.max_results,
        )) + "</tr>" for task in tasks
    )
    counts = plan_counts(tasks)
    platform_pills = "".join(
        f'<span class="pill">{html.escape(platform)}: {count}</span>'
        for platform, count in counts["by_platform"].items()
    )
    html_path.write_text(f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>JobBot Search Plan</title>
<style>body{{font:14px system-ui;margin:24px;color:#14213d}}h1{{margin-bottom:4px}}.summary{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}.pill{{background:#edf4ff;border-radius:999px;padding:8px 12px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#fff}}tr:nth-child(even){{background:#fafafa}}</style></head><body><h1>JobBot v3.2 Search Plan — {html.escape(mode)}</h1><p>Every production task is remote-only and unlimited by result count. Total tasks: <strong>{counts['total']}</strong>.</p><div class=\"summary\">{platform_pills}</div>
<table><thead><tr><th>Phase</th><th>Lane</th><th>Allocation</th><th>Profile</th><th>Platform</th><th>Exact query</th><th>Condition</th><th>Age</th><th>Priority</th><th>Execution rank</th><th>State</th><th>Resume</th><th>Max results</th></tr></thead><tbody>{rows}</tbody></table></body></html>""", encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "html": html_path}


def compile_and_write(bundle: ConfigBundle, mode: str, platforms: Iterable[str] | None = None, *, open_browser: bool = False) -> tuple[list[SearchTask], dict[str, Path]]:
    tasks = compile_plan(bundle, mode, platforms)
    paths = write_plan(tasks, bundle.output_dir, mode)
    if open_browser:
        webbrowser.open(paths["html"].as_uri())
    return tasks, paths


def compile_staged_and_write(bundle: ConfigBundle, platforms: Iterable[str] | None = None, *, open_browser: bool = False) -> tuple[list[SearchTask], dict[str, Path]]:
    tasks = compile_staged_plan(bundle, platforms)
    paths = write_plan(tasks, bundle.output_dir, "staged")
    if open_browser:
        webbrowser.open(paths["html"].as_uri())
    return tasks, paths
