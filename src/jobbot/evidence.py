from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


BOARD_SITES = {"linkedin", "indeed", "glassdoor"}
ATS_SITES = {"greenhouse", "lever", "ashby", "smartrecruiters"}
VERIFIED_SOURCE_STATES = {"verified_direct_ats", "verified_canonical_ats", "verified_jsonld"}
ACTIONABLE_RECOMMENDATIONS = {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}


def ats_url_identity(url: str) -> tuple[str, str, str]:
    """Return public ATS type, employer board key, and requisition key when present."""
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower()
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if host in {"greenhouse.io", "greenhouse.com"} or host.endswith((".greenhouse.io", ".greenhouse.com")):
            if "jobs" in parts:
                index = parts.index("jobs")
                board = parts[index - 1] if index else ""
                requisition = parts[index + 1] if index + 1 < len(parts) else ""
                return ("greenhouse", board, requisition) if board and requisition else ("", "", "")
            requisition = (parse_qs(parsed.query).get("gh_jid") or [""])[0]
            board = parts[0] if parts else ""
            return ("greenhouse", board, requisition) if board and requisition else ("", "", "")
        if host == "jobs.lever.co":
            board, requisition = parts[0] if parts else "", parts[1] if len(parts) > 1 else ""
            return ("lever", board, requisition) if board and requisition else ("", "", "")
        if host == "api.lever.co" and len(parts) >= 4 and parts[:2] == ["v0", "postings"]:
            return "lever", parts[2], parts[3]
        if host == "jobs.ashbyhq.com":
            board, requisition = parts[0] if parts else "", parts[1] if len(parts) > 1 else ""
            return ("ashby", board, requisition) if board and requisition else ("", "", "")
        if host == "smartrecruiters.com" or host.endswith(".smartrecruiters.com"):
            board, requisition = parts[0] if parts else "", parts[1] if len(parts) > 1 else ""
            return ("smartrecruiters", board, requisition) if board and requisition else ("", "", "")
    except (TypeError, ValueError):
        pass
    return "", "", ""


def _valid_web_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return url
    except ValueError:
        pass
    return ""


def _is_board_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == name or host.endswith("." + name) for name in ("linkedin.com", "indeed.com", "glassdoor.com"))
    except ValueError:
        return False


def derive_url_roles(job: Any) -> dict[str, str]:
    raw = job.raw if isinstance(getattr(job, "raw", None), dict) else {}
    source = str(getattr(job, "source_site", "") or "").lower()
    canonical = _valid_web_url(getattr(job, "canonical_url", ""))
    apply = _valid_web_url(getattr(job, "apply_url", ""))
    description = str(getattr(job, "description", "") or "").strip()
    source_payload = raw.get("source_payload") if isinstance(raw.get("source_payload"), dict) else {}

    roles = {
        "discovery_url": _valid_web_url(raw.get("discovery_url") or getattr(job, "discovery_url", "")),
        "board_detail_url": _valid_web_url(raw.get("board_detail_url") or getattr(job, "board_detail_url", "")),
        "observed_board_apply_url": _valid_web_url(raw.get("observed_board_apply_url") or getattr(job, "observed_board_apply_url", "")),
        "employer_job_url": _valid_web_url(raw.get("employer_job_url") or getattr(job, "employer_job_url", "")),
        "ats_requisition_url": _valid_web_url(raw.get("ats_requisition_url") or getattr(job, "ats_requisition_url", "")),
        "verified_application_url": _valid_web_url(raw.get("verified_application_url") or getattr(job, "verified_application_url", "")),
    }

    if source in BOARD_SITES:
        roles["discovery_url"] = roles["discovery_url"] or canonical
        if description:
            roles["board_detail_url"] = roles["board_detail_url"] or canonical
        observed_apply = source_payload.get("apply_url") or source_payload.get("applyUrl")
        roles["observed_board_apply_url"] = roles["observed_board_apply_url"] or _valid_web_url(observed_apply) or (apply if apply and apply != canonical else "")
    elif canonical and not ats_url_identity(canonical)[0] and not _is_board_url(canonical):
        roles["employer_job_url"] = roles["employer_job_url"] or canonical
    roles["discovery_url"] = roles["discovery_url"] or canonical

    ats_url = next((url for url in (roles["ats_requisition_url"], apply, canonical) if ats_url_identity(url)[0]), "")
    roles["ats_requisition_url"] = roles["ats_requisition_url"] or ats_url

    source_verification = str(getattr(job, "source_verification", "") or "")
    canonical_verified = bool(getattr(job, "canonical_verified", 0))
    verification_url = apply if ats_url_identity(apply)[0] else canonical if ats_url_identity(canonical)[0] else ""
    if canonical_verified and source_verification in VERIFIED_SOURCE_STATES and verification_url:
        roles["verified_application_url"] = verification_url
    elif source_verification == "verified_jsonld" and canonical_verified:
        jsonld = raw.get("jsonld_enrichment") if isinstance(raw.get("jsonld_enrichment"), dict) else {}
        explicit = _valid_web_url(jsonld.get("applyUrl") or jsonld.get("applicationUrl") or raw.get("verified_application_url"))
        explicit_host = (urlsplit(explicit).hostname or "").lower() if explicit else ""
        if explicit and not ats_url_identity(explicit)[0] and not any(site in explicit_host for site in BOARD_SITES):
            roles["verified_application_url"] = explicit
    else:
        roles["verified_application_url"] = ""

    for key, value in roles.items():
        setattr(job, key, value)
    return roles


def assess_evidence_readiness(job: Any) -> dict[str, Any]:
    roles = derive_url_roles(job)
    title = str(getattr(job, "title", "") or "").strip()
    company = str(getattr(job, "company", "") or "").strip()
    source_id = str(getattr(job, "source_job_id", "") or "").strip()
    identity_url = _valid_web_url(getattr(job, "canonical_url", "")) or roles["discovery_url"]
    if title and company and identity_url and (source_id or identity_url):
        identity_state = "COMPLETE"
    elif title or identity_url:
        identity_state = "PARTIAL"
    else:
        identity_state = "MISSING"

    description = str(getattr(job, "description", "") or "").strip()
    description_length = len(description)
    detail_state = "COMPLETE" if description_length >= 250 else "PARTIAL" if description else "MISSING"
    gates = getattr(job, "qualification_gates", {})
    if not isinstance(gates, dict):
        gates = {}
    requirements_gate = gates.get("requirements_supported", {})
    responsibility_gate = gates.get("responsibility_domain", {})
    requirement_status = str(requirements_gate.get("status", "review")).lower()
    if detail_state == "MISSING":
        requirements_state = "MISSING"
    elif detail_state != "COMPLETE":
        requirements_state = "PARTIAL"
    elif requirement_status == "pass":
        requirements_state = "SUPPORTED"
    elif requirement_status == "fail":
        requirements_state = "UNSUPPORTED"
    else:
        requirements_state = "UNRESOLVED"

    source_state = str(getattr(job, "source_verification", "unverified_discovery") or "unverified_discovery")
    source_reason = str(getattr(job, "source_verification_reason", "") or "")
    raw = getattr(job, "raw", {})
    raw = raw if isinstance(raw, dict) else {}
    source_site = str(getattr(job, "source_site", "") or "").lower()
    if raw.get("ats_enrichment") or source_site in ATS_SITES:
        detail_source_type = "public_ats_requisition"
    elif raw.get("jsonld_enrichment"):
        detail_source_type = "employer_job_posting_jsonld"
    elif roles["board_detail_url"]:
        detail_source_type = "board_detail"
    else:
        detail_source_type = "unverified_description_source"
    destination = roles["verified_application_url"]
    if source_state == "identity_mismatch":
        destination_state = "IDENTITY_MISMATCH"
    elif destination and ats_url_identity(destination)[0]:
        destination_state = "VERIFIED_ATS"
    elif destination:
        destination_state = "VERIFIED_EMPLOYER"
    elif roles["observed_board_apply_url"] or roles["ats_requisition_url"]:
        destination_state = "OBSERVED_UNVERIFIED"
    elif str(getattr(job, "source_site", "")).lower() in BOARD_SITES or _is_board_url(
        _valid_web_url(getattr(job, "canonical_url", "")) or _valid_web_url(getattr(job, "apply_url", ""))
    ):
        destination_state = "BOARD_ONLY"
    else:
        destination_state = "MISSING"

    missing: list[str] = []
    blocking: list[str] = []
    if identity_state != "COMPLETE":
        missing.append("canonical identity requires title, company, and a source URL")
    if detail_state != "COMPLETE":
        missing.append("substantive employer job detail is missing or partial")
    if requirements_state in {"MISSING", "PARTIAL", "UNRESOLVED"}:
        missing.append("requirements evidence is missing, partial, or unresolved")
    if str(responsibility_gate.get("status", "review")).lower() != "pass":
        missing.append("substantive responsibility and domain evidence is unresolved")
    if source_state not in VERIFIED_SOURCE_STATES:
        missing.append("employer or public ATS source verification is not complete")
    if destination_state not in {"VERIFIED_ATS", "VERIFIED_EMPLOYER"}:
        missing.append("final employer or public ATS application destination is not verified")

    if identity_state != "COMPLETE":
        blocking.append("identity evidence is incomplete")
    if source_state == "identity_mismatch":
        blocking.append(source_reason or "source identity does not match the discovered employer or requisition")
    for gate_name, gate in gates.items():
        status = str(gate.get("status", "review")).lower() if isinstance(gate, dict) else "review"
        if status != "pass":
            evidence = str(gate.get("evidence", "evidence unresolved")) if isinstance(gate, dict) else "evidence unresolved"
            blocking.append(f"{gate_name}: {evidence}")
    blocking.extend(item for item in missing if item not in blocking)

    evidence_ready = not missing and identity_state == "COMPLETE"
    if source_state == "identity_mismatch":
        evidence_state = "BLOCKED"
    else:
        evidence_state = "READY" if evidence_ready else "REVIEW"
    qualification_ready = evidence_state == "READY" and all(
        isinstance(gate, dict) and str(gate.get("status", "review")).lower() == "pass"
        for gate in gates.values()
    )
    qualification_state = "BLOCKED" if source_state == "identity_mismatch" or any(
        isinstance(gate, dict) and str(gate.get("status", "review")).lower() == "fail"
        for gate in gates.values()
    ) else "READY" if qualification_ready else "REVIEW"

    observed = {
        "identity": {"state": identity_state, "evidence_type": "discovery_card_or_source_identity", "title": title, "company": company, "source_job_id": source_id, "url": identity_url},
        "detail": {"state": detail_state, "evidence_type": detail_source_type, "description_characters": description_length, "source_site": str(getattr(job, "source_site", "") or "")},
        "requirements": {"state": requirements_state, "evidence_type": "employer_description_requirement_extraction", "extracted_required_text": str(getattr(job, "required_qualifications", "") or "")[:4000], "gate": requirements_gate},
        "responsibility_domain": responsibility_gate,
        "source": {"state": source_state, "reason": source_reason, "canonical_verified": bool(getattr(job, "canonical_verified", 0))},
        "application_destination": {"state": destination_state, "evidence_type": destination_state.lower(), "url": destination},
        "url_roles": roles,
        "qualification_gates": gates,
    }
    return {
        "identity_evidence_state": identity_state,
        "detail_evidence_state": detail_state,
        "requirements_evidence_state": requirements_state,
        "source_verification_state": source_state,
        "application_destination_verification_state": destination_state,
        "evidence_readiness_state": evidence_state,
        "qualification_readiness_state": qualification_state,
        "evidence_missing": sorted(set(missing)),
        "evidence_blocking": sorted(set(blocking)),
        "evidence_readiness": {
            "decision": evidence_state,
            "qualification_readiness": qualification_state,
            "observed": observed,
            "missing": sorted(set(missing)),
            "blocking": sorted(set(blocking)),
        },
    }


def apply_evidence_readiness(job: Any) -> dict[str, Any]:
    result = assess_evidence_readiness(job)
    for key, value in result.items():
        if key in {"evidence_missing", "evidence_blocking", "evidence_readiness"}:
            setattr(job, key, value)
        else:
            setattr(job, key, value)
    gates = getattr(job, "qualification_gates", {})
    if not isinstance(gates, dict):
        gates = {}
    readiness_status = "pass" if result["evidence_readiness_state"] == "READY" else "fail" if result["evidence_readiness_state"] == "BLOCKED" else "review"
    readiness_reason = "all required identity, detail, requirement, source, and application evidence is verified" if readiness_status == "pass" else "; ".join(result["evidence_blocking"][:5])
    gates["evidence_readiness"] = {"status": readiness_status, "evidence": readiness_reason}
    setattr(job, "qualification_gates", gates)
    # Re-evaluate the final qualification state after exposing evidence readiness as a gate.
    result = assess_evidence_readiness(job)
    for key, value in result.items():
        setattr(job, key, value)
    recommendation = str(getattr(job, "recommendation", "") or "")
    if recommendation in ACTIONABLE_RECOMMENDATIONS and result["qualification_readiness_state"] != "READY":
        job.recommendation = "REVIEW"
    readiness = result["evidence_readiness_state"]
    if readiness != "READY":
        reason = "held for evidence review: " + "; ".join(result["evidence_blocking"][:5])
    elif result["qualification_readiness_state"] != "READY":
        reason = "evidence is complete but qualification gates remain unresolved: " + "; ".join(result["evidence_blocking"][:5])
    else:
        reason = f"evidence and qualification readiness passed; scoring selected {job.recommendation}"
    explanation = dict(getattr(job, "evidence_readiness", {}) or {})
    explanation["recommendation"] = str(getattr(job, "recommendation", "") or "")
    explanation["recommendation_reason"] = reason
    setattr(job, "evidence_readiness", explanation)
    setattr(job, "evidence_recommendation_reason", reason)
    return result
