from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence


EXPORT_COLUMNS = (
    "job_id", "recommendation", "title", "company", "career_lane", "canonical_source_site",
    "posted_at", "first_seen", "last_seen", "salary_text", "employment_class", "remote_gate",
    "location_raw", "relevance_score", "qualification_score", "landing_score", "career_score",
    "door_score", "resume_variant", "application_status", "canonical_url", "apply_url",
    "description", "requirement_matches_json", "requirement_gaps_json", "score_reasons_json",
)


def safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text


def _rows(conn: sqlite3.Connection, where: str = "1=1", args: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(f"SELECT {','.join(EXPORT_COLUMNS)} FROM jobs WHERE {where} ORDER BY door_score DESC,last_seen DESC", args))


def _csv(path: Path, rows: Iterable[sqlite3.Row]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXPORT_COLUMNS)
        for row in rows:
            writer.writerow([safe_cell(row[column]) for column in EXPORT_COLUMNS])


def export_selected(conn: sqlite3.Connection, output_dir: Path, job_ids: Sequence[str]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not job_ids:
        rows: list[sqlite3.Row] = []
    else:
        marks = ",".join("?" for _ in job_ids)
        rows = _rows(conn, f"job_id IN ({marks})", tuple(job_ids))
    path = output_dir / "selected_jobs.csv"
    _csv(path, rows)
    return path


def export_all(conn: sqlite3.Connection, output_dir: Path, *, batch_size: int = 20) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    terminal = "('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED','WITHDRAWN','SKIP','CLOSED')"
    qualified = "is_active=1 AND remote_gate='pass' AND recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')"
    definitions = {
        "all_jobs.csv": ("1=1", ()),
        "active_jobs.csv": ("is_active=1", ()),
        "qualified_jobs.csv": (qualified, ()),
        "unapplied_jobs.csv": (qualified + f" AND upper(application_status) NOT IN {terminal}", ()),
        "apply_now.csv": ("is_active=1 AND recommendation='APPLY_NOW'", ()),
        "apply_volume.csv": ("is_active=1 AND recommendation='APPLY_VOLUME'", ()),
        "stretch.csv": ("is_active=1 AND recommendation='HIGH_VALUE_STRETCH'", ()),
        "application_tracker.csv": ("upper(application_status)!='NEW'", ()),
        "recent_updates.csv": ("change_status IN ('NEW','UPDATED','CLOSED','REOPENED')", ()),
    }
    paths: dict[str, Path] = {}
    for name, (where, args) in definitions.items():
        path = output_dir / name
        _csv(path, _rows(conn, where, args))
        paths[name] = path

    jsonl_path = output_dir / "jobs.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in _rows(conn):
            handle.write(json.dumps({column: row[column] for column in EXPORT_COLUMNS}, ensure_ascii=False) + "\n")
    paths[jsonl_path.name] = jsonl_path

    batch_rows = _rows(conn, qualified + f" AND upper(application_status) NOT IN {terminal}")[:max(1, batch_size)]
    markdown = ["# JobBot final-review batch", "", "Read-only review packet; application decisions remain human-controlled.", ""]
    for index, row in enumerate(batch_rows, 1):
        markdown.extend([
            f"## {index}. {row['title']} — {row['company']}", "",
            f"- Job ID: `{row['job_id']}`",
            f"- Recommendation: {row['recommendation']}",
            f"- Salary: {row['salary_text'] or 'not stated'}",
            f"- Remote: {row['remote_gate']} — {row['location_raw']}",
            f"- Employment: {row['employment_class']}",
            f"- Scores: relevance {row['relevance_score'] or 0}; qualification {row['qualification_score'] or 0}; landing fit {row['landing_score'] or 0}; career value {row['career_score'] or 0}; door {row['door_score'] or 0}",
            f"- Resume: {row['resume_variant'] or 'review'}",
            f"- URL: {row['apply_url'] or row['canonical_url']}", "",
            "### Fit and gaps", "",
            str(row["requirement_matches_json"] or "[]"), "", str(row["requirement_gaps_json"] or "[]"), "",
            "### Description", "", str(row["description"] or "Description unavailable"), "",
        ])
    batch_path = output_dir / "chatgpt_batch.md"
    batch_path.write_text("\n".join(markdown), encoding="utf-8")
    paths[batch_path.name] = batch_path
    return paths
