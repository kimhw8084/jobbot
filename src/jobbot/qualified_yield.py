from __future__ import annotations

import json
from typing import Any


ACTIONABLE_RECOMMENDATIONS = {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}
VERIFIED_SOURCE_STATES = {"verified_direct_ats", "verified_canonical_ats", "verified_jsonld"}
VERIFIED_DESTINATIONS = {"VERIFIED_ATS", "VERIFIED_EMPLOYER"}
STABLE_EMPLOYMENT_CLASSES = {"full_time_employee", "full_time_permanent"}
REQUIRED_SUBSTANTIVE_GATES = {
    "evidence_readiness", "home_remote", "work_authorization", "texas_eligibility",
    "full_time", "permanent_employee", "base_pay_floor", "mandatory_presence",
    "open_current", "requirements_supported", "responsibility_domain",
}


def _object(value: Any) -> dict[str, Any] | None:
    try:
        result = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _array(value: Any) -> list[Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return result if isinstance(result, list) else None


def trusted_qualified_yield(job: Any) -> bool:
    """Whether current durable evidence proves intrinsic CHG-170 qualification.

    The no-repeat gate is deliberately omitted from qualification truth. Its
    recommendation override is admitted only when the durable scorer snapshot
    records an actionable intrinsic recommendation.
    """
    if str(job.get("evidence_readiness_state") or "").upper() != "READY":
        return False
    if str(job.get("identity_evidence_state") or "").upper() != "COMPLETE":
        return False
    if str(job.get("source_verification_state") or job.get("source_verification") or "").lower() not in VERIFIED_SOURCE_STATES:
        return False
    if str(job.get("application_destination_verification_state") or "").upper() not in VERIFIED_DESTINATIONS:
        return False
    if str(job.get("remote_gate") or "").lower() != "pass":
        return False
    if str(job.get("employment_class") or "").lower() not in STABLE_EMPLOYMENT_CLASSES:
        return False
    if str(job.get("posting_status") or "").lower() != "active":
        return False
    try:
        if int(job.get("is_active") or 0) != 1:
            return False
    except (TypeError, ValueError):
        return False

    hard_rejects = _array(job.get("hard_reject_reasons_json"))
    if hard_rejects is None or hard_rejects:
        return False

    gates = _object(job.get("qualification_gates_json"))
    if gates is None or not REQUIRED_SUBSTANTIVE_GATES.issubset(gates):
        return False
    if any(
        not isinstance(gate, dict) or str(gate.get("status") or "").lower() != "pass"
        for name, gate in gates.items()
        if name != "no_repeat"
    ):
        return False
    no_repeat = gates.get("no_repeat") if isinstance(gates.get("no_repeat"), dict) else {}
    qualification_state = str(job.get("qualification_readiness_state") or "").upper()
    no_repeat_failed = str(no_repeat.get("status") or "").lower() == "fail"
    if qualification_state != "READY" and not (qualification_state == "BLOCKED" and no_repeat_failed):
        return False

    recommendation = str(job.get("recommendation") or "").upper()
    if recommendation in ACTIONABLE_RECOMMENDATIONS:
        return True
    if recommendation != "ALREADY_HANDLED" or str(job.get("application_status") or "NEW").upper() == "NEW":
        return False

    evidence = _object(job.get("evidence_readiness_json"))
    if evidence is None:
        return False
    intrinsic_recommendation = str(
        evidence.get("intrinsic_recommendation") or evidence.get("recommendation") or ""
    ).upper()
    return intrinsic_recommendation in ACTIONABLE_RECOMMENDATIONS
