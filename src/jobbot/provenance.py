from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_type(job: dict[str, Any]) -> str:
    evidence = _object(job.get("evidence_provenance_json"))
    recorded = str(evidence.get("source_type") or "").lower()
    if recorded in {"search_card", "board_detail", "employer_page", "public_ats_jsonld", "inferred_derived", "unknown"}:
        return recorded
    ats_url = str(job.get("verified_application_url") or job.get("ats_requisition_url") or "")
    try:
        host = (urlsplit(ats_url).hostname or "").lower()
    except ValueError:
        host = ""
    if host and any(name in host for name in ("greenhouse", "lever", "ashby", "smartrecruiters")):
        return "public_ats_jsonld" if evidence.get("detail_source_type") == "employer_job_posting_jsonld" else "public_ats"
    if job.get("employer_job_url"):
        return "employer_page"
    if job.get("board_detail_url"):
        return "board_detail"
    if job.get("discovery_url"):
        return "search_card"
    return "unknown"


def field_provenance_summary(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a concise, conservative view from CHG-113 durable evidence fields."""
    evidence = _object(job.get("evidence_provenance_json"))
    source = _source_type(job)

    def item(name: str, value: Any, evidence_state: str = "", readiness: str = "") -> dict[str, Any]:
        marker = str(evidence.get(name) or "").lower()
        state = evidence_state.upper()
        if state in {"OBSERVED", "SUPPORTED", "VERIFIED_ATS", "VERIFIED_EMPLOYER"} or marker.startswith("observed"):
            value_state = "OBSERVED"
            evidence_type = marker or source
        elif value not in (None, "", [], {}):
            value_state = "INFERRED_OR_DERIVED"
            evidence_type = "inferred/derived"
        else:
            value_state = "UNKNOWN"
            evidence_type = "unknown"
        return {
            "value": value if value not in (None, "") else None,
            "source_type": (str(evidence.get(f"{name}_source_type") or source) if value_state == "OBSERVED" else "inferred_derived") if value_state != "UNKNOWN" else "unknown",
            "evidence_type": evidence_type,
            "state": value_state,
            "readiness": readiness or state or "UNKNOWN",
        }

    location_state = str(job.get("location_evidence_state") or "UNKNOWN")
    remote_state = str(job.get("remote_evidence_state") or "UNKNOWN")
    requirements_state = str(job.get("requirements_evidence_state") or "UNKNOWN")
    source_state = str(job.get("source_verification_state") or job.get("source_verification") or "UNKNOWN")
    destination_state = str(job.get("application_destination_verification_state") or "UNKNOWN")

    remote_value = job.get("remote_status")
    if not remote_value or str(remote_value).lower() == "unknown":
        remote_value = job.get("remote_gate")
    location = item("location", job.get("location_raw"), location_state, location_state)
    remote = item("remote", remote_value, remote_state, remote_state)
    salary = item("salary", job.get("salary_text"), str(evidence.get("salary_state") or ""), str(evidence.get("salary_state") or "UNKNOWN"))
    employment = item("employment_type", job.get("employment_class") or job.get("employment_type"), str(evidence.get("employment_type_state") or ""), str(evidence.get("employment_type_state") or "UNKNOWN"))
    posted = item("posted_at", job.get("posted_at"), str(evidence.get("posted_at_state") or ""), str(evidence.get("posted_at_state") or "UNKNOWN"))
    requirements_value = job.get("required_qualifications") or job.get("requirements_text")
    requirements = item("requirements", requirements_value, requirements_state, requirements_state)
    if requirements_state == "SUPPORTED":
        requirements["state"] = "OBSERVED"
        requirements["evidence_type"] = "employer description requirement extraction"
        requirements["source_type"] = str(evidence.get("requirements_source_type") or source)
    destination_url = job.get("verified_application_url") or job.get("observed_board_apply_url") or job.get("ats_requisition_url")
    destination = item("application_destination", destination_url, destination_state, destination_state)
    if destination_state in {"VERIFIED_ATS", "VERIFIED_EMPLOYER"}:
        destination["state"] = "OBSERVED"
        destination["evidence_type"] = "verified application destination"
        destination["source_type"] = "public_ats" if destination_state == "VERIFIED_ATS" else "employer_page"
    elif destination_url and destination_state in {"OBSERVED_UNVERIFIED", "BOARD_ONLY"}:
        destination["state"] = "OBSERVED_UNVERIFIED"
        destination["evidence_type"] = "observed but not verified"
        destination["source_type"] = str(evidence.get("application_destination_source_type") or source)
    current_status = job.get("posting_status")
    current_status_marker = str(evidence.get("posting_status") or evidence.get("current_status") or "").lower()
    current_status_state = "OBSERVED" if current_status and current_status_marker.startswith("observed") else "INFERRED_OR_DERIVED" if current_status else "UNKNOWN"
    return {
        "remote": remote,
        "location": location,
        "salary": salary,
        "employment_type": employment,
        "posted_current_status": {
            **posted,
            "current_status": current_status or "UNKNOWN",
            "current_status_source_type": str(evidence.get("posting_status_source_type") or source) if current_status_state == "OBSERVED" else "inferred_derived" if current_status else "unknown",
            "current_status_state": current_status_state,
        },
        "requirements": requirements,
        "application_destination": destination,
        "source_verification": {"value": source_state, "state": source_state, "source_type": source},
    }
