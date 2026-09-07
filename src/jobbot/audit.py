from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TASK_STATUSES = ("queued", "running", "exhausted", "incomplete", "challenged", "auth_required", "failed", "paused", "stopped")


def _n(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, args).fetchone()[0] or 0)


def collect(conn: sqlite3.Connection) -> dict[str, Any]:
    platforms: dict[str, Any] = {}
    for platform in ("linkedin", "indeed", "glassdoor"):
        status = {name: _n(conn, "SELECT COUNT(*) FROM browser_search_tasks WHERE platform=? AND status=?", (platform, name)) for name in TASK_STATUSES}
        platforms[platform] = {
            "planned": _n(conn, "SELECT COUNT(*) FROM browser_search_tasks WHERE platform=?", (platform,)),
            **status,
            "results_encountered": _n(conn, "SELECT COALESCE(SUM(results_seen),0) FROM browser_search_tasks WHERE platform=?", (platform,)),
            "details_read": _n(conn, "SELECT COALESCE(SUM(detail_count_read),0) FROM browser_search_tasks WHERE platform=?", (platform,)),
            "unique_jobs": _n(conn, "SELECT COUNT(DISTINCT canonical_job_id) FROM search_task_results WHERE source_site=? AND canonical_job_id IS NOT NULL", (platform,)),
            "duplicate_sightings": _n(conn, "SELECT COALESCE(SUM(duplicate_sightings),0) FROM browser_search_tasks WHERE platform=?", (platform,)),
        }
    global_counts = {
        "sightings": _n(conn, "SELECT COALESCE(SUM(seen_count),0) FROM source_occurrences"),
        "canonical_jobs": _n(conn, "SELECT COUNT(*) FROM jobs"),
        "full_descriptions": _n(conn, "SELECT COUNT(*) FROM jobs WHERE length(trim(COALESCE(description,'')))>=250"),
        "missing_descriptions": _n(conn, "SELECT COUNT(*) FROM jobs WHERE length(trim(COALESCE(description,'')))<250"),
        "remote_confirmed": _n(conn, "SELECT COUNT(*) FROM jobs WHERE remote_gate='pass'"),
        "remote_rejected": _n(conn, "SELECT COUNT(*) FROM jobs WHERE remote_gate='reject'"),
        "remote_uncertain": _n(conn, "SELECT COUNT(*) FROM jobs WHERE remote_gate NOT IN ('pass','reject') OR remote_gate IS NULL"),
        "relevant": _n(conn, "SELECT COUNT(*) FROM jobs WHERE COALESCE(relevance_score,0)>=72"),
        "qualified": _n(conn, "SELECT COUNT(*) FROM jobs WHERE COALESCE(qualification_score,0)>=72 AND COALESCE(relevance_score,0)>=72"),
        "apply_now": _n(conn, "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='APPLY_NOW'"),
        "apply_volume": _n(conn, "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='APPLY_VOLUME'"),
        "stretch": _n(conn, "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='HIGH_VALUE_STRETCH'"),
        "contract_review": _n(conn, "SELECT COUNT(*) FROM jobs WHERE recommendation IN ('CONTRACT_REVIEW','FIXED_TERM_REVIEW','PART_TIME_REVIEW')"),
        "unapplied_reservoir": _n(conn, """SELECT COUNT(*) FROM jobs WHERE is_active=1 AND remote_gate='pass'
          AND recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')
          AND upper(application_status) NOT IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED','WITHDRAWN','SKIP','CLOSED')"""),
        "applied": _n(conn, "SELECT COUNT(*) FROM jobs WHERE upper(application_status) IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED')"),
    }
    diagnosis: list[str] = []
    if any(v["planned"] and v["exhausted"] < v["planned"] for v in platforms.values()):
        diagnosis.append("Search coverage is incomplete: planned tasks remain non-exhausted.")
    if global_counts["missing_descriptions"] > global_counts["full_descriptions"]:
        diagnosis.append("Description enrichment is the dominant funnel loss.")
    if any(v["challenged"] or v["auth_required"] for v in platforms.values()):
        diagnosis.append("At least one primary platform is blocked by challenge or authentication state.")
    if global_counts["relevant"] and not global_counts["qualified"]:
        diagnosis.append("Relevant jobs exist, but qualification gates produce no qualified inventory.")
    if not diagnosis and global_counts["unapplied_reservoir"] < 10:
        diagnosis.append("Coverage and enrichment show no obvious failure; current qualified market volume is small.")
    return {"platforms": platforms, "global": global_counts, "diagnosis": diagnosis}


def render_terminal(audit: dict[str, Any]) -> str:
    lines = ["JOBBOT RETRIEVAL AUDIT", ""]
    for platform, values in audit["platforms"].items():
        lines.append(f"{platform.upper()}: planned={values['planned']} exhausted={values['exhausted']} incomplete={values['incomplete']} challenged={values['challenged']} auth_required={values['auth_required']} failed={values['failed']} results={values['results_encountered']} details={values['details_read']} unique={values['unique_jobs']} duplicates={values['duplicate_sightings']}")
    lines.append("")
    lines.extend(f"{key}={value}" for key, value in audit["global"].items())
    if audit["diagnosis"]:
        lines.extend(["", "DIAGNOSIS:", *[f"- {value}" for value in audit["diagnosis"]]])
    return "\n".join(lines)


def write_reports(audit: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "retrieval_audit.md"
    html_path = output_dir / "retrieval_audit.html"
    terminal = render_terminal(audit)
    markdown_path.write_text("# JobBot retrieval audit\n\n```text\n" + terminal + "\n```\n", encoding="utf-8")
    html_path.write_text("<!doctype html><meta charset='utf-8'><title>JobBot Retrieval Audit</title><style>body{font:15px system-ui;max-width:1200px;margin:30px auto;padding:0 20px}pre{white-space:pre-wrap;background:#f5f7fa;padding:20px;border-radius:12px}</style><h1>JobBot retrieval audit</h1><pre>" + html.escape(terminal) + "</pre>", encoding="utf-8")
    return {"markdown": markdown_path, "html": html_path}
