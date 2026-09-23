from __future__ import annotations

import json
import sqlite3

from ..evidence import ats_url_identity
from .m0001_ledger import _ensure


VERSION = 19
NAME = "CHG-113 evidence readiness and application URL provenance"

_JOB_FIELDS = {
    "discovery_url": "TEXT NOT NULL DEFAULT ''",
    "board_detail_url": "TEXT NOT NULL DEFAULT ''",
    "observed_board_apply_url": "TEXT NOT NULL DEFAULT ''",
    "employer_job_url": "TEXT NOT NULL DEFAULT ''",
    "ats_requisition_url": "TEXT NOT NULL DEFAULT ''",
    "verified_application_url": "TEXT NOT NULL DEFAULT ''",
    "identity_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "detail_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "requirements_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "source_verification_state": "TEXT NOT NULL DEFAULT 'UNVERIFIED_DISCOVERY'",
    "application_destination_verification_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "evidence_readiness_state": "TEXT NOT NULL DEFAULT 'REVIEW'",
    "qualification_readiness_state": "TEXT NOT NULL DEFAULT 'REVIEW'",
    "evidence_missing_json": "TEXT NOT NULL DEFAULT '[]'",
    "evidence_blocking_json": "TEXT NOT NULL DEFAULT '[]'",
    "evidence_readiness_json": "TEXT NOT NULL DEFAULT '{}'",
}

_OCCURRENCE_FIELDS = {
    "discovery_url": "TEXT NOT NULL DEFAULT ''",
    "board_detail_url": "TEXT NOT NULL DEFAULT ''",
    "observed_board_apply_url": "TEXT NOT NULL DEFAULT ''",
    "employer_job_url": "TEXT NOT NULL DEFAULT ''",
    "ats_requisition_url": "TEXT NOT NULL DEFAULT ''",
    "verified_application_url": "TEXT NOT NULL DEFAULT ''",
    "identity_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "detail_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "requirements_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "source_verification_state": "TEXT NOT NULL DEFAULT 'UNVERIFIED_DISCOVERY'",
    "application_destination_verification_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "evidence_readiness_state": "TEXT NOT NULL DEFAULT 'REVIEW'",
    "evidence_missing_json": "TEXT NOT NULL DEFAULT '[]'",
    "evidence_blocking_json": "TEXT NOT NULL DEFAULT '[]'",
}

_RESULT_FIELDS = {
    "discovery_url": "TEXT NOT NULL DEFAULT ''",
    "board_detail_url": "TEXT NOT NULL DEFAULT ''",
    "observed_board_apply_url": "TEXT NOT NULL DEFAULT ''",
    "ats_requisition_url": "TEXT NOT NULL DEFAULT ''",
    "verified_application_url": "TEXT NOT NULL DEFAULT ''",
    "identity_evidence_state": "TEXT NOT NULL DEFAULT 'PERSISTED'",
    "detail_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "requirements_evidence_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "source_verification_state": "TEXT NOT NULL DEFAULT 'UNVERIFIED_DISCOVERY'",
    "application_destination_verification_state": "TEXT NOT NULL DEFAULT 'MISSING'",
    "evidence_readiness_state": "TEXT NOT NULL DEFAULT 'REVIEW'",
    "evidence_missing_json": "TEXT NOT NULL DEFAULT '[]'",
    "evidence_blocking_json": "TEXT NOT NULL DEFAULT '[]'",
}


def _is_ats(url: str) -> bool:
    return bool(ats_url_identity(url)[0])


def upgrade(conn: sqlite3.Connection) -> None:
    _ensure(conn, "jobs", _JOB_FIELDS)
    _ensure(conn, "source_occurrences", _OCCURRENCE_FIELDS)
    _ensure(conn, "search_task_results", _RESULT_FIELDS)

    for row in conn.execute("SELECT * FROM jobs WHERE evidence_readiness_json='{}' OR evidence_readiness_json='' ").fetchall():
        values = dict(row)
        source = str(values.get("canonical_source_site") or "").lower()
        canonical = str(values.get("canonical_url") or "")
        apply = str(values.get("apply_url") or "")
        description = str(values.get("description") or "").strip()
        occurrence = conn.execute(
            "SELECT * FROM source_occurrences WHERE job_id=? ORDER BY first_seen,occurrence_key LIMIT 1",
            (values["job_id"],),
        ).fetchone()
        occurrence = dict(occurrence) if occurrence else {}
        board = source in {"linkedin", "indeed", "glassdoor"}
        ats_url = next((url for url in (apply, canonical) if _is_ats(url)), "")
        verified_destination = ats_url if int(values.get("canonical_verified") or 0) and str(values.get("source_verification") or "") in {
            "verified_direct_ats", "verified_canonical_ats", "verified_jsonld",
        } else ""
        discovery_url = str(occurrence.get("source_url") or (canonical if board else ""))
        board_detail_url = str(occurrence.get("source_url") or "") if board and description else ""
        observed_board_apply_url = str(occurrence.get("apply_url") or "") if board else ""
        if observed_board_apply_url == discovery_url:
            observed_board_apply_url = ""
        lower_canonical = canonical.lower()
        employer_job_url = canonical if canonical and not _is_ats(canonical) and not board and not any(site in lower_canonical for site in ("linkedin.com", "indeed.com", "glassdoor.com")) else ""
        identity_state = "COMPLETE" if values.get("title") and values.get("company") and (canonical or discovery_url) else "PARTIAL" if values.get("title") or canonical else "MISSING"
        detail_state = "COMPLETE" if len(description) >= 250 else "PARTIAL" if description else "MISSING"
        requirements_state = "UNRESOLVED" if detail_state == "COMPLETE" else detail_state
        source_state = str(values.get("source_verification") or "unverified_discovery")
        if source_state == "identity_mismatch":
            destination_state = "IDENTITY_MISMATCH"
        elif verified_destination:
            destination_state = "VERIFIED_ATS"
        elif observed_board_apply_url or ats_url:
            destination_state = "OBSERVED_UNVERIFIED"
        elif board:
            destination_state = "BOARD_ONLY"
        else:
            destination_state = "MISSING"
        missing = []
        if identity_state != "COMPLETE":
            missing.append("canonical identity requires title, company, and a source URL")
        if detail_state != "COMPLETE":
            missing.append("substantive employer job detail is missing or partial")
        if requirements_state != "SUPPORTED":
            missing.append("requirements evidence requires rescoring under CHG-113")
        if source_state not in {"verified_direct_ats", "verified_canonical_ats", "verified_jsonld"}:
            missing.append("employer or public ATS source verification is not complete")
        if destination_state not in {"VERIFIED_ATS", "VERIFIED_EMPLOYER"}:
            missing.append("final employer or public ATS application destination is not verified")
        blocking = list(missing)
        readiness = {
            "decision": "REVIEW",
            "qualification_readiness": "REVIEW",
            "migration": NAME,
            "observed": {
                "identity": {"state": identity_state, "title": values.get("title") or "", "company": values.get("company") or "", "url": canonical or discovery_url},
                "detail": {"state": detail_state, "description_characters": len(description)},
                "requirements": {"state": requirements_state},
                "source": {"state": source_state, "canonical_verified": bool(values.get("canonical_verified"))},
                "application_destination": {"state": destination_state, "url": verified_destination},
                "url_roles": {
                    "discovery_url": discovery_url,
                    "board_detail_url": board_detail_url,
                    "observed_board_apply_url": observed_board_apply_url,
                    "employer_job_url": employer_job_url,
                    "ats_requisition_url": ats_url,
                    "verified_application_url": verified_destination,
                },
            },
            "missing": missing,
            "blocking": blocking,
        }
        conn.execute(
            """UPDATE jobs SET discovery_url=COALESCE(NULLIF(discovery_url,''),?),
                 board_detail_url=COALESCE(NULLIF(board_detail_url,''),?),
                 observed_board_apply_url=COALESCE(NULLIF(observed_board_apply_url,''),?),
                 employer_job_url=COALESCE(NULLIF(employer_job_url,''),?),
                 ats_requisition_url=COALESCE(NULLIF(ats_requisition_url,''),?),
                 verified_application_url=COALESCE(NULLIF(verified_application_url,''),?),
                 identity_evidence_state=?,detail_evidence_state=?,requirements_evidence_state=?,
                 source_verification_state=?,application_destination_verification_state=?,
                 evidence_readiness_state='REVIEW',qualification_readiness_state='REVIEW',
                 evidence_missing_json=?,evidence_blocking_json=?,evidence_readiness_json=?
               WHERE job_id=?""",
            (discovery_url, board_detail_url, observed_board_apply_url, employer_job_url, ats_url, verified_destination,
             identity_state, detail_state, requirements_state, source_state, destination_state,
             json.dumps(missing, ensure_ascii=False), json.dumps(blocking, ensure_ascii=False),
             json.dumps(readiness, ensure_ascii=False, sort_keys=True), values["job_id"]),
        )

    # Old actionable labels are held until a current strategy pass writes the complete contract.
    conn.execute(
        """UPDATE jobs SET recommendation='REVIEW'
           WHERE recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')
             AND (evidence_readiness_state<>'READY' OR qualification_readiness_state<>'READY')"""
    )

    conn.execute("""UPDATE search_task_results SET
       discovery_url=source_url,
       identity_evidence_state=CASE WHEN length(trim(COALESCE(title_hint,'')))>0 AND length(trim(COALESCE(source_url,'')))>0 THEN 'COMPLETE' ELSE 'PARTIAL' END,
       detail_evidence_state=CASE WHEN content_state='COMPLETE' THEN 'COMPLETE' WHEN content_state='PARTIAL' THEN 'PARTIAL' ELSE 'MISSING' END,
       requirements_evidence_state=CASE WHEN content_state='COMPLETE' THEN 'UNRESOLVED' ELSE 'MISSING' END,
       evidence_readiness_state='REVIEW',
       evidence_missing_json='[\"canonical employer detail, supported requirements, source verification, and verified application destination require canonical-job scoring\"]',
       evidence_blocking_json='[\"canonical employer detail, supported requirements, source verification, and verified application destination require canonical-job scoring\"]'
       WHERE discovery_url='' AND evidence_missing_json='[]'""")
    conn.execute("""UPDATE source_occurrences SET discovery_url=COALESCE(NULLIF(discovery_url,''),source_url),
       observed_board_apply_url=CASE WHEN observed_board_apply_url='' AND source_site IN ('linkedin','indeed','glassdoor') AND apply_url<>source_url THEN apply_url ELSE observed_board_apply_url END,
       board_detail_url=CASE WHEN board_detail_url='' AND source_site IN ('linkedin','indeed','glassdoor')
         AND EXISTS(SELECT 1 FROM jobs j WHERE j.job_id=source_occurrences.job_id AND length(trim(COALESCE(j.description,'')))>=250)
         THEN source_url ELSE board_detail_url END,
       employer_job_url=COALESCE(NULLIF(employer_job_url,''),(SELECT j.employer_job_url FROM jobs j WHERE j.job_id=source_occurrences.job_id)),
       ats_requisition_url=COALESCE(NULLIF(ats_requisition_url,''),(SELECT j.ats_requisition_url FROM jobs j WHERE j.job_id=source_occurrences.job_id)),
       verified_application_url=COALESCE(NULLIF(verified_application_url,''),(SELECT j.verified_application_url FROM jobs j WHERE j.job_id=source_occurrences.job_id)),
       identity_evidence_state=COALESCE((SELECT j.identity_evidence_state FROM jobs j WHERE j.job_id=source_occurrences.job_id),identity_evidence_state),
       detail_evidence_state=COALESCE((SELECT j.detail_evidence_state FROM jobs j WHERE j.job_id=source_occurrences.job_id),detail_evidence_state),
       requirements_evidence_state=COALESCE((SELECT j.requirements_evidence_state FROM jobs j WHERE j.job_id=source_occurrences.job_id),requirements_evidence_state),
       source_verification_state=COALESCE((SELECT j.source_verification_state FROM jobs j WHERE j.job_id=source_occurrences.job_id),source_verification_state),
       application_destination_verification_state=COALESCE((SELECT j.application_destination_verification_state FROM jobs j WHERE j.job_id=source_occurrences.job_id),application_destination_verification_state),
       evidence_readiness_state='REVIEW',
       evidence_missing_json=COALESCE((SELECT j.evidence_missing_json FROM jobs j WHERE j.job_id=source_occurrences.job_id),'[]'),
       evidence_blocking_json=COALESCE((SELECT j.evidence_blocking_json FROM jobs j WHERE j.job_id=source_occurrences.job_id),'[]')
       WHERE discovery_url=''""")
