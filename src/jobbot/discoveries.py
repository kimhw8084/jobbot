from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


DETAIL_PENDING = "PENDING"
DETAIL_RUNNING = "RUNNING"
DETAIL_COMPLETE = "COMPLETE"
DETAIL_PARTIAL = "PARTIAL"
DETAIL_RETRYABLE = "RETRYABLE"
DETAIL_FAILED = "FAILED"
DETAIL_EXTERNAL_BLOCKED = "EXTERNAL_BLOCKED"
DETAIL_SKIPPED_AGE = "SKIPPED_AGE"
DETAIL_DEFERRED_RECALL = "DEFERRED_RECALL"


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
    identity_status: str
    card_metadata_status: str
    content_state: str
    enrichment_priority: int
    recall_selected: bool
    recall_qa_sample: bool
    recall_reason: str
    discovery_url: str
    board_detail_url: str
    observed_board_apply_url: str
    ats_requisition_url: str
    verified_application_url: str
    identity_evidence_state: str
    detail_evidence_state: str
    requirements_evidence_state: str
    source_verification_state: str
    application_destination_verification_state: str
    evidence_readiness_state: str
    evidence_missing_json: str
    evidence_blocking_json: str

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
            identity_status=str(row["identity_status"] or "PERSISTED"),
            card_metadata_status=str(row["card_metadata_status"] or "MISSING"),
            content_state=str(row["content_state"] or "MISSING"),
            enrichment_priority=int(row["enrichment_priority"] or 0),
            recall_selected=bool(row["recall_selected"]),
            recall_qa_sample=bool(row["recall_qa_sample"]),
            recall_reason=str(row["recall_reason"] or ""),
            discovery_url=str(row["discovery_url"] or ""),
            board_detail_url=str(row["board_detail_url"] or ""),
            observed_board_apply_url=str(row["observed_board_apply_url"] or ""),
            ats_requisition_url=str(row["ats_requisition_url"] or ""),
            verified_application_url=str(row["verified_application_url"] or ""),
            identity_evidence_state=str(row["identity_evidence_state"] or "MISSING"),
            detail_evidence_state=str(row["detail_evidence_state"] or "MISSING"),
            requirements_evidence_state=str(row["requirements_evidence_state"] or "MISSING"),
            source_verification_state=str(row["source_verification_state"] or "UNVERIFIED_DISCOVERY"),
            application_destination_verification_state=str(row["application_destination_verification_state"] or "MISSING"),
            evidence_readiness_state=str(row["evidence_readiness_state"] or "REVIEW"),
            evidence_missing_json=str(row["evidence_missing_json"] or "[]"),
            evidence_blocking_json=str(row["evidence_blocking_json"] or "[]"),
        )


def upsert_card(
    conn: sqlite3.Connection, *, run_id: int, task_id: int, platform: str,
    source_job_id: str, source_url: str, title_hint: str = "", company_hint: str = "",
    location_hint: str = "", posted_text: str = "", posted_age_days: float | None = None,
    strategy_profile: str = "", strategy_profile_version: str = "", query_family: str = "",
    query_kind: str = "", query_pass: str = "", initial_order: int = 0,
    card: dict[str, Any] | None = None, eligible_for_detail: bool = True,
    recall_selected: bool = True, recall_qa_sample: bool = False,
    recall_reason: str = "", enrichment_priority: int = 0,
) -> tuple[Discovery, bool]:
    now = _now()
    card_missing = json.dumps([
        "substantive employer job detail is missing",
        "requirements evidence is missing",
        "employer or public ATS source verification is not complete",
        "verified final application destination is missing",
    ], ensure_ascii=False)
    row = conn.execute(
        """SELECT * FROM search_task_results
           WHERE task_id=? AND source_site=? AND source_job_id=? AND source_url=?""",
        (task_id, platform, source_job_id, source_url),
    ).fetchone()
    initial_status = (
        DETAIL_SKIPPED_AGE if not eligible_for_detail else
        DETAIL_PENDING if recall_selected or recall_qa_sample else DETAIL_DEFERRED_RECALL
    )
    metadata_status = "CAPTURED" if any((company_hint, location_hint, posted_text)) else "PARTIAL"
    payload = json.dumps(card or {}, ensure_ascii=False, sort_keys=True)
    if row is None:
        cursor = conn.execute(
            """INSERT INTO search_task_results(
              task_id,source_site,source_job_id,source_url,first_seen_at,last_seen_at,
              browser_run_id,title_hint,company_hint,location_hint,posted_text,posted_age_days,
              observed_at,card_json,detail_status,identity_status,identity_persisted_at,
              card_metadata_status,content_state,enrichment_priority,recall_selected,recall_qa_sample,recall_reason,
              strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, platform, source_job_id, source_url, now, now, run_id, title_hint,
             company_hint, location_hint, posted_text, posted_age_days, now, payload, initial_status,
             "PERSISTED", now, metadata_status, "MISSING", int(enrichment_priority),
             int(bool(recall_selected)), int(bool(recall_qa_sample)), recall_reason,
             strategy_profile, strategy_profile_version, query_family, query_kind, query_pass, int(initial_order)),
        )
        result_id = int(cursor.lastrowid)
        duplicate = False
    else:
        result_id = int(row["result_id"])
        conn.execute(
            """UPDATE search_task_results SET
              browser_run_id=?,last_seen_at=?,sighting_count=sighting_count+1,observed_at=?,
              identity_status='PERSISTED',identity_persisted_at=COALESCE(identity_persisted_at,?),
              title_hint=CASE WHEN ?<>'' THEN ? ELSE title_hint END,
              company_hint=CASE WHEN ?<>'' THEN ? ELSE company_hint END,
              location_hint=CASE WHEN ?<>'' THEN ? ELSE location_hint END,
              posted_text=CASE WHEN ?<>'' THEN ? ELSE posted_text END,
              posted_age_days=COALESCE(?,posted_age_days),card_json=CASE WHEN ?<>'{}' THEN ? ELSE card_json END,
              card_metadata_status=CASE WHEN ? THEN 'CAPTURED' ELSE card_metadata_status END,
              enrichment_priority=MAX(enrichment_priority,?),
              recall_selected=MAX(recall_selected,?),recall_qa_sample=MAX(recall_qa_sample,?),
              recall_reason=CASE WHEN ?<>'' THEN ? ELSE recall_reason END,
              strategy_profile=CASE WHEN ?<>'' THEN ? ELSE strategy_profile END,
              strategy_profile_version=CASE WHEN ?<>'' THEN ? ELSE strategy_profile_version END,
              query_family=CASE WHEN ?<>'' THEN ? ELSE query_family END,
              query_kind=CASE WHEN ?<>'' THEN ? ELSE query_kind END,
              query_pass=CASE WHEN ?<>'' THEN ? ELSE query_pass END,
              initial_order=CASE WHEN ?>0 THEN ? ELSE initial_order END,
              detail_status=CASE WHEN detail_status='SKIPPED_AGE' AND ? THEN
                CASE WHEN ? OR ? THEN 'PENDING' ELSE 'DEFERRED_RECALL' END ELSE detail_status END
              WHERE result_id=?""",
            (run_id, now, now, now, title_hint, title_hint, company_hint, company_hint,
             location_hint, location_hint, posted_text, posted_text, posted_age_days,
             payload, payload, int(bool(metadata_status == "CAPTURED")), int(enrichment_priority),
             int(bool(recall_selected)), int(bool(recall_qa_sample)), recall_reason, recall_reason,
             strategy_profile, strategy_profile, strategy_profile_version, strategy_profile_version,
             query_family, query_family, query_kind, query_kind, query_pass, query_pass,
             int(initial_order), int(initial_order),
             1 if eligible_for_detail else 0, int(bool(recall_selected)), int(bool(recall_qa_sample)), result_id),
        )
        duplicate = True
    conn.execute(
        """UPDATE search_task_results SET discovery_url=?,identity_evidence_state=CASE
             WHEN length(trim(COALESCE(title_hint,'')))>0 AND length(trim(COALESCE(company_hint,'')))>0 THEN 'COMPLETE' ELSE 'PARTIAL' END,
             detail_evidence_state=CASE WHEN content_state='COMPLETE' THEN 'COMPLETE' WHEN content_state='PARTIAL' THEN 'PARTIAL' ELSE 'MISSING' END,
             requirements_evidence_state=CASE WHEN content_state='COMPLETE' THEN requirements_evidence_state WHEN content_state='PARTIAL' THEN 'PARTIAL' ELSE 'MISSING' END,
             source_verification_state=CASE WHEN source_verification_state='' THEN 'UNVERIFIED_DISCOVERY' ELSE source_verification_state END,
             application_destination_verification_state=CASE WHEN application_destination_verification_state='MISSING' THEN 'BOARD_ONLY' ELSE application_destination_verification_state END,
             evidence_readiness_state=CASE WHEN content_state='MISSING' THEN 'REVIEW' ELSE evidence_readiness_state END,
             evidence_missing_json=CASE WHEN content_state='COMPLETE' THEN evidence_missing_json ELSE ? END,
             evidence_blocking_json=CASE WHEN content_state='COMPLETE' THEN evidence_blocking_json ELSE ? END
           WHERE result_id=?""",
        (source_url, card_missing, card_missing, result_id),
    )
    saved = conn.execute("SELECT * FROM search_task_results WHERE result_id=?", (result_id,)).fetchone()
    return Discovery.from_row(saved), duplicate


def claim_next_detail(
    conn: sqlite3.Connection, *, run_id: int, task_id: int, worker_id: str,
    lease_seconds: int = 180, include_recall_negatives: bool = False,
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
    statuses = "('PENDING','RETRYABLE','DEFERRED_RECALL')" if include_recall_negatives else "('PENDING','RETRYABLE')"
    row = conn.execute(
        f"""SELECT * FROM search_task_results
           WHERE browser_run_id=? AND task_id=?
             AND (detail_status IN {statuses}
               OR (detail_status='RUNNING' AND detail_lease_owner=?))
           ORDER BY CASE WHEN recall_selected=1 THEN 0 WHEN recall_qa_sample=1 THEN 1 ELSE 2 END,
                    CASE detail_status WHEN 'RUNNING' THEN 0 WHEN 'RETRYABLE' THEN 1 ELSE 2 END,result_id
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


def finish_detail(
    conn: sqlite3.Connection, result_id: int, canonical_job_id: str | None = None,
    *, content_state: str = "COMPLETE",
) -> None:
    status = DETAIL_COMPLETE if content_state == "COMPLETE" else DETAIL_PARTIAL
    conn.execute(
        """UPDATE search_task_results SET canonical_job_id=COALESCE(?,canonical_job_id),detail_read=1,
             detail_status=?,content_state=?,detail_completed_at=?,detail_error='',detail_lease_owner='',detail_lease_until=NULL
           WHERE result_id=?""",
        (canonical_job_id, status, content_state, _now(), result_id),
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
        """UPDATE search_task_results SET detail_status='EXTERNAL_BLOCKED',content_state='MISSING',detail_error=?,
             detail_lease_owner='',detail_lease_until=NULL WHERE result_id=?""",
        (message[:1000], result_id),
    )
