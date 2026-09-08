from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


DETAIL_PENDING = "PENDING"
DETAIL_RUNNING = "RUNNING"
DETAIL_COMPLETE = "COMPLETE"
DETAIL_RETRYABLE = "RETRYABLE"
DETAIL_FAILED = "FAILED"
DETAIL_EXTERNAL_BLOCKED = "EXTERNAL_BLOCKED"
DETAIL_SKIPPED_AGE = "SKIPPED_AGE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Discovery:
    result_id: int
    run_id: int
    task_id: int
    platform: str
    source_job_id: str
    source_url: str
    title_hint: str
    company_hint: str
    location_hint: str
    posted_text: str
    posted_age_days: float | None
    observed_at: str
    detail_status: str
    detail_attempts: int
    card: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Discovery":
        try:
            card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            card = {}
        return cls(
            result_id=int(row["result_id"]), run_id=int(row["browser_run_id"] or 0),
            task_id=int(row["task_id"]), platform=str(row["source_site"]),
            source_job_id=str(row["source_job_id"] or ""), source_url=str(row["source_url"] or ""),
            title_hint=str(row["title_hint"] or ""), company_hint=str(row["company_hint"] or ""),
            location_hint=str(row["location_hint"] or ""), posted_text=str(row["posted_text"] or ""),
            posted_age_days=None if row["posted_age_days"] is None else float(row["posted_age_days"]),
            observed_at=str(row["observed_at"] or row["first_seen_at"]),
            detail_status=str(row["detail_status"]), detail_attempts=int(row["detail_attempts"] or 0),
            card=card if isinstance(card, dict) else {},
        )


def upsert_card(
    conn: sqlite3.Connection, *, run_id: int, task_id: int, platform: str,
    source_job_id: str, source_url: str, title_hint: str = "", company_hint: str = "",
    location_hint: str = "", posted_text: str = "", posted_age_days: float | None = None,
    card: dict[str, Any] | None = None, eligible_for_detail: bool = True,
) -> tuple[Discovery, bool]:
    now = _now()
    row = conn.execute(
        """SELECT * FROM search_task_results
           WHERE task_id=? AND source_site=? AND source_job_id=? AND source_url=?""",
        (task_id, platform, source_job_id, source_url),
    ).fetchone()
    initial_status = DETAIL_PENDING if eligible_for_detail else DETAIL_SKIPPED_AGE
    payload = json.dumps(card or {}, ensure_ascii=False, sort_keys=True)
    if row is None:
        cursor = conn.execute(
            """INSERT INTO search_task_results(
              task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,
              browser_run_id,title_hint,company_hint,location_hint,posted_text,posted_age_days,
              observed_at,card_json,detail_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, platform, source_job_id, source_url, now, now, run_id, title_hint,
             company_hint, location_hint, posted_text, posted_age_days, now, payload, initial_status),
        )
        result_id = int(cursor.lastrowid)
        duplicate = False
    else:
        result_id = int(row["result_id"])
        conn.execute(
            """UPDATE search_task_results SET
              browser_run_id=?,last_seen_at=?,sighting_count=sighting_count+1,observed_at=?,
              title_hint=CASE WHEN ?<>'' THEN ? ELSE title_hint END,
              company_hint=CASE WHEN ?<>'' THEN ? ELSE company_hint END,
              location_hint=CASE WHEN ?<>'' THEN ? ELSE location_hint END,
              posted_text=CASE WHEN ?<>'' THEN ? ELSE posted_text END,
              posted_age_days=COALESCE(?,posted_age_days),card_json=CASE WHEN ?<>'{}' THEN ? ELSE card_json END,
              detail_status=CASE WHEN detail_status='SKIPPED_AGE' AND ? THEN 'PENDING' ELSE detail_status END
              WHERE result_id=?""",
            (run_id, now, now, title_hint, title_hint, company_hint, company_hint,
             location_hint, location_hint, posted_text, posted_text, posted_age_days,
             payload, payload, 1 if eligible_for_detail else 0, result_id),
        )
        duplicate = True
    saved = conn.execute("SELECT * FROM search_task_results WHERE result_id=?", (result_id,)).fetchone()
    return Discovery.from_row(saved), duplicate


def claim_next_detail(
    conn: sqlite3.Connection, *, run_id: int, task_id: int, worker_id: str,
    lease_seconds: int = 180,
) -> Discovery | None:
    now = _now()
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=max(15, lease_seconds))).isoformat(timespec="seconds")
    conn.execute(
        """UPDATE search_task_results SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL,
             detail_error=CASE WHEN detail_error='' THEN 'reclaimed expired detail lease' ELSE detail_error END
           WHERE browser_run_id=? AND task_id=? AND detail_status='RUNNING'
             AND COALESCE(detail_lease_until,'')<?""",
        (run_id, task_id, now),
    )
    row = conn.execute(
        """SELECT * FROM search_task_results
           WHERE browser_run_id=? AND task_id=?
             AND (detail_status IN ('PENDING','RETRYABLE')
               OR (detail_status='RUNNING' AND detail_lease_owner=?))
           ORDER BY CASE detail_status WHEN 'RUNNING' THEN 0 WHEN 'RETRYABLE' THEN 1 ELSE 2 END,result_id
           LIMIT 1""",
        (run_id, task_id, worker_id),
    ).fetchone()
    if row is None:
        return None
    result_id = int(row["result_id"])
    was_running = str(row["detail_status"]) == DETAIL_RUNNING
    conn.execute(
        """UPDATE search_task_results SET detail_status='RUNNING',detail_started_at=COALESCE(detail_started_at,?),
             detail_attempts=detail_attempts+?,detail_lease_owner=?,detail_lease_until=?,detail_error=''
           WHERE result_id=?""",
        (now, 0 if was_running else 1, worker_id, lease_until, result_id),
    )
    claimed = conn.execute("SELECT * FROM search_task_results WHERE result_id=?", (result_id,)).fetchone()
    return Discovery.from_row(claimed)


def finish_detail(conn: sqlite3.Connection, result_id: int, canonical_job_id: str | None = None) -> None:
    conn.execute(
        """UPDATE search_task_results SET canonical_job_id=COALESCE(?,canonical_job_id),detail_read=1,
             detail_status='COMPLETE',detail_completed_at=?,detail_error='',detail_lease_owner='',detail_lease_until=NULL
           WHERE result_id=?""",
        (canonical_job_id, _now(), result_id),
    )


def fail_detail(conn: sqlite3.Connection, result_id: int, message: str, *, max_attempts: int = 3) -> str:
    row = conn.execute("SELECT detail_attempts FROM search_task_results WHERE result_id=?", (result_id,)).fetchone()
    if row is None:
        return DETAIL_FAILED
    status = DETAIL_FAILED if int(row[0] or 0) >= max(1, max_attempts) else DETAIL_RETRYABLE
    conn.execute(
        """UPDATE search_task_results SET detail_status=?,detail_error=?,detail_lease_owner='',detail_lease_until=NULL
           WHERE result_id=?""", (status, message[:1000], result_id),
    )
    return status


def block_detail(conn: sqlite3.Connection, result_id: int, message: str) -> None:
    conn.execute(
        """UPDATE search_task_results SET detail_status='EXTERNAL_BLOCKED',detail_error=?,
             detail_lease_owner='',detail_lease_until=NULL WHERE result_id=?""",
        (message[:1000], result_id),
    )
