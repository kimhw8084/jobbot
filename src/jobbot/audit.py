from __future__ import annotations

import html
import sqlite3
from pathlib import Path
from typing import Any


TASK_STATUSES = (
    "queued", "running", "exhausted", "incomplete", "challenged",
    "auth_required", "deferred_by_platform", "failed", "paused", "stopped",
)


def _n(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, args).fetchone()[0] or 0)


def _thresholds(strategy: dict[str, Any] | None) -> tuple[float, float]:
    scoring = (strategy or {}).get("strategy", {}).get("scoring", {})
    return (float(scoring.get("minimum_relevance_for_apply", 80)), float(scoring.get("minimum_qualification_for_apply", 72)))


def _run_id(conn: sqlite3.Connection, requested: int | None) -> int | None:
    if requested is not None:
        return requested if conn.execute("SELECT 1 FROM browser_runs WHERE browser_run_id=?", (requested,)).fetchone() else None
    row = conn.execute("SELECT browser_run_id FROM browser_runs ORDER BY browser_run_id DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def _scope(alias: str, run_id: int | None) -> tuple[str, tuple[Any, ...]]:
    return (f" WHERE {alias}.browser_run_id=?", (run_id,)) if run_id is not None else (" WHERE 0", ())


def _platforms(conn: sqlite3.Connection, run_id: int | None, include_all: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    where, args = (" WHERE 1", ()) if include_all else _scope("t", run_id)
    for platform in ("linkedin", "indeed", "glassdoor"):
        values = {name: _n(conn, f"SELECT COUNT(*) FROM browser_search_tasks t{where} AND t.platform=? AND t.status=?", (*args, platform, name)) for name in TASK_STATUSES}
        values.update({
            "planned": _n(conn, f"SELECT COUNT(*) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "results_encountered": _n(conn, f"SELECT COALESCE(SUM(t.results_seen),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "details_read": _n(conn, f"SELECT COALESCE(SUM(t.detail_count_read),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "cards_extracted": _n(conn, f"SELECT COALESCE(SUM(t.cards_extracted),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "cards_persistence_attempted": _n(conn, f"SELECT COALESCE(SUM(t.cards_persistence_attempted),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "cards_persisted": _n(conn, f"SELECT COALESCE(SUM(t.cards_persistence_succeeded),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "cards_persistence_failed": _n(conn, f"SELECT COALESCE(SUM(t.cards_persistence_failed),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "duplicate_sightings": _n(conn, f"SELECT COALESCE(SUM(t.duplicate_sightings),0) FROM browser_search_tasks t{where} AND t.platform=?", (*args, platform)),
            "unexplained": _n(conn, f"SELECT COUNT(*) FROM browser_search_tasks t{where} AND t.platform=? AND t.status NOT IN ({','.join('?' for _ in TASK_STATUSES)})", (*args, platform, *TASK_STATUSES)),
        })
        phase_rows = conn.execute(f"SELECT COALESCE(t.phase,'UNSPECIFIED') phase,COUNT(*) n FROM browser_search_tasks t{where} AND t.platform=? GROUP BY t.phase ORDER BY t.phase", (*args, platform)).fetchall()
        values["phases"] = {str(row["phase"]): int(row["n"]) for row in phase_rows}
        result[platform] = values
    return result


def _global(conn: sqlite3.Connection, run_id: int | None, strategy: dict[str, Any] | None) -> dict[str, Any]:
    where, args = _scope("t", run_id)
    relevance, qualification = _thresholds(strategy)
    def result_count(condition: str) -> int:
        return _n(conn, f"SELECT COUNT(*) FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id{where} AND {condition}", args)
    return {
        "sightings": result_count("1=1"),
        "canonical_jobs": result_count("r.canonical_job_id IS NOT NULL"),
        "detail_pending": result_count("r.detail_status='PENDING'"),
        "detail_running": result_count("r.detail_status='RUNNING'"),
        "detail_complete": result_count("r.detail_status='COMPLETE'"),
        "detail_retryable": result_count("r.detail_status='RETRYABLE'"),
        "detail_failed": result_count("r.detail_status='FAILED'"),
        "detail_external_blocked": result_count("r.detail_status='EXTERNAL_BLOCKED'"),
        "relevant": _n(conn, "SELECT COUNT(*) FROM jobs WHERE COALESCE(relevance_score,0)>=?", (relevance,)),
        "qualified": _n(conn, "SELECT COUNT(*) FROM jobs WHERE COALESCE(qualification_score,0)>=? AND COALESCE(relevance_score,0)>=?", (qualification, relevance)),
        "apply_now": _n(conn, "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='APPLY_NOW'"),
        "apply_volume": _n(conn, "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='APPLY_VOLUME'"),
        "stretch": _n(conn, "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='HIGH_VALUE_STRETCH'"),
        "description_complete": _n(conn, "SELECT COUNT(*) FROM jobs WHERE description_state='COMPLETE'"),
        "description_missing": _n(conn, "SELECT COUNT(*) FROM jobs WHERE description_state='MISSING'"),
        "description_partial": _n(conn, "SELECT COUNT(*) FROM jobs WHERE description_state='PARTIAL_TOO_SHORT'"),
        "applied": _n(conn, "SELECT COUNT(*) FROM jobs WHERE upper(application_status) IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED')"),
    }


def _classification(conn: sqlite3.Connection, run_id: int | None) -> str:
    if run_id is None:
        return "COMPLETED_WITH_INTERNAL_INCOMPLETE"
    run = conn.execute("SELECT status,stop_requested FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
    statuses = [row[0] for row in conn.execute("SELECT status FROM browser_search_tasks WHERE browser_run_id=?", (run_id,))]
    if not run or str(run[0]) == "stopped" or int(run[1] or 0):
        return "STOPPED" if run else "COMPLETED_WITH_INTERNAL_INCOMPLETE"
    if "failed" in statuses or str(run[0]) == "failed":
        return "FAILED_FATAL"
    if any(x in statuses for x in ("queued", "running", "incomplete", "paused", "stopped")):
        return "COMPLETED_WITH_INTERNAL_INCOMPLETE"
    if any(x in statuses for x in ("challenged", "auth_required", "deferred_by_platform")):
        return "COMPLETED_PARTIAL_EXTERNAL"
    if str(run[0]) == "completed" and statuses and all(x == "exhausted" for x in statuses):
        return "COMPLETED_FULL"
    return "COMPLETED_WITH_INTERNAL_INCOMPLETE"


def collect(conn: sqlite3.Connection, run_id: int | None = None, strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    current_id = _run_id(conn, run_id)
    platforms = _platforms(conn, current_id)
    reconciliation: dict[str, Any] = {"ok": True, "failures": []}
    for platform, values in platforms.items():
        if values["cards_extracted"] != values["cards_persistence_attempted"]:
            reconciliation["failures"].append(f"{platform}: extracted cards != persistence attempts")
        if values["cards_persistence_attempted"] != values["cards_persisted"] + values["cards_persistence_failed"]:
            reconciliation["failures"].append(f"{platform}: persistence attempts != succeeded + failed")
        if values["cards_extracted"] > 0 and values["cards_persisted"] == 0:
            reconciliation["failures"].append(f"{platform}: extracted cards have zero persisted cards")
    reconciliation["ok"] = not reconciliation["failures"]
    diagnosis = [f"FAIL: {x}" for x in reconciliation["failures"]]
    if any(v["planned"] and v["exhausted"] < v["planned"] and not (v["challenged"] or v["auth_required"] or v["deferred_by_platform"]) for v in platforms.values()):
        diagnosis.append("Search coverage is internally incomplete: planned tasks remain non-terminal.")
    if any(v["challenged"] or v["auth_required"] or v["deferred_by_platform"] for v in platforms.values()):
        diagnosis.append("At least one primary platform is externally blocked; its work is isolated and checkpointed.")
    cumulative = {
        "sightings": _n(conn, "SELECT COUNT(*) FROM source_occurrences"),
        "canonical_jobs": _n(conn, "SELECT COUNT(*) FROM jobs"),
        "detail_complete": _n(conn, "SELECT COUNT(*) FROM search_task_results WHERE detail_status='COMPLETE'"),
    }
    return {
        "current_run_id": current_id,
        "terminal_classification": _classification(conn, current_id),
        "platforms": platforms,
        "global": _global(conn, current_id, strategy),
        "cumulative": {"global": cumulative, "platforms": _platforms(conn, None, include_all=True)},
        "reconciliation": reconciliation,
        "diagnosis": diagnosis,
    }


def render_terminal(audit: dict[str, Any]) -> str:
    lines = ["JOBBOT RETRIEVAL AUDIT", f"CURRENT RUN: {audit.get('current_run_id') or 'none'}", f"TERMINAL: {audit.get('terminal_classification')}", ""]
    for platform, values in audit["platforms"].items():
        lines.append(f"{platform.upper()}: planned={values['planned']} queued={values['queued']} running={values['running']} exhausted={values['exhausted']} incomplete={values['incomplete']} challenged={values['challenged']} auth_required={values['auth_required']} deferred_by_platform={values['deferred_by_platform']} failed={values['failed']} stopped={values['stopped']} unexplained={values['unexplained']} extracted={values['cards_extracted']} attempted={values['cards_persistence_attempted']} persisted={values['cards_persisted']} persistence_failed={values['cards_persistence_failed']} duplicates={values['duplicate_sightings']} phases={values['phases']}")
    lines.extend(["", *[f"{key}={value}" for key, value in audit["global"].items()], f"RECONCILIATION_OK={audit['reconciliation']['ok']}"])
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
