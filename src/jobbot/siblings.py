from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from .evidence import VERIFIED_SOURCE_STATES, ats_url_identity


ALGORITHM_VERSION = "chg114-probable-siblings-v1"
_TOKEN = re.compile(r"[a-z0-9]+")
_GENERIC = {"the", "and", "for", "with", "from", "this", "that", "will", "you", "our", "your", "are", "all", "role", "job", "work", "team", "to", "of", "in", "a", "an"}


def _norm(value: Any) -> str:
    return " ".join(_TOKEN.findall(str(value or "").lower()))


def _date(value: Any) -> date | None:
    try:
        raw = str(value or "")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _remote_bucket(job: dict[str, Any]) -> str:
    remote = str(job.get("remote_status") or "").lower()
    location = str(job.get("location_raw") or "").lower()
    if remote in {"remote", "fully remote", "100 remote", "us remote"} or "remote" in location:
        return "remote"
    if remote not in {"", "unknown", "unspecified"} or location:
        return "location_bound"
    return "unknown"


def _location_key(job: dict[str, Any]) -> str:
    bucket = _remote_bucket(job)
    if bucket != "location_bound":
        return bucket
    return _norm(job.get("location_raw"))


def _description_tokens(job: dict[str, Any]) -> set[str]:
    return {token for token in _TOKEN.findall(str(job.get("description") or "").lower()) if len(token) > 2 and token not in _GENERIC}


def _compatible(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, float, list[str], bool]:
    employer_left, employer_right = _norm(left.get("company")), _norm(right.get("company"))
    title_left, title_right = _norm(left.get("title")), _norm(right.get("title"))
    if not employer_left or employer_left != employer_right or not title_left or title_left != title_right:
        return False, 0.0, [], False
    for job in (left, right):
        state = str(job.get("source_verification_state") or job.get("source_verification") or "").lower()
        if state in {"identity_mismatch", "verified_identity_conflict", "identity_conflict"}:
            return False, 0.0, [], False
    if _location_key(left) != _location_key(right):
        return False, 0.0, [], False

    basis = ["normalized employer match", "normalized title match", f"location/remote bucket match: {_location_key(left) or 'unknown'}"]
    confidence = 0.78
    left_date, right_date = _date(left.get("posted_at")), _date(right.get("posted_at"))
    if left_date and right_date:
        distance = abs((left_date - right_date).days)
        if distance > 30:
            return False, 0.0, [], False
        confidence += 0.08
        basis.append(f"posted dates within {distance} days")
    left_tokens, right_tokens = _description_tokens(left), _description_tokens(right)
    similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens) if left_tokens and right_tokens else 0.0
    if similarity >= 0.5:
        confidence += 0.08
        basis.append(f"description token similarity {similarity:.2f}")
    elif not (left_date and right_date):
        return False, 0.0, [], False

    verified_left = str(left.get("source_verification_state") or left.get("source_verification") or "").lower() in VERIFIED_SOURCE_STATES
    verified_right = str(right.get("source_verification_state") or right.get("source_verification") or "").lower() in VERIFIED_SOURCE_STATES
    req_left = ats_url_identity(str(left.get("ats_requisition_url") or left.get("verified_application_url") or ""))[2] if verified_left else ""
    req_right = ats_url_identity(str(right.get("ats_requisition_url") or right.get("verified_application_url") or ""))[2] if verified_right else ""
    distinct_requisitions = bool(req_left and req_right and req_left != req_right)
    if distinct_requisitions:
        basis.append("distinct verified requisition identities; related sibling records remain separate")
        confidence += 0.02
    return True, min(0.98, confidence), basis, distinct_requisitions


def probable_sibling_clusters(conn: Any, *, limit: int = 5000, output_limit: int = 200) -> dict[str, Any]:
    scan_limit = max(2, min(20000, int(limit)))
    bucket_limit = 200
    rows = conn.execute("""SELECT job_id,title,company,location_raw,remote_status,remote_gate,posted_at,description,
          ats_requisition_url,verified_application_url,source_verification,source_verification_state,
          evidence_readiness_state,qualification_readiness_state,recommendation,last_seen
        FROM jobs WHERE length(trim(COALESCE(title,'')))>0 AND length(trim(COALESCE(company,'')))>0
        ORDER BY last_seen DESC,job_id LIMIT ?""", (scan_limit + 1,)).fetchall()
    candidates = [dict(row) for row in rows[:scan_limit]]
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for job in candidates:
        key = (_norm(job.get("company")), _norm(job.get("title")), _location_key(job))
        if key[0] and key[1]:
            buckets.setdefault(key, []).append(job)

    clusters = []
    bucket_candidates_truncated = 0
    for key, bucket in sorted(buckets.items()):
        bucket_candidates_truncated += max(0, len(bucket) - bucket_limit)
        bucket = bucket[:bucket_limit]
        groups: list[list[dict[str, Any]]] = []
        for candidate in sorted(bucket, key=lambda job: str(job["job_id"])):
            target = next((group for group in groups if all(_compatible(candidate, member)[0] for member in group)), None)
            if target is None:
                groups.append([candidate])
            else:
                target.append(candidate)
        for group in groups:
            if len(group) < 2:
                continue
            pair_evidence = [_compatible(group[i], group[j]) for i in range(len(group)) for j in range(i + 1, len(group))]
            basis = sorted({item for evidence in pair_evidence for item in evidence[2]})
            confidence = round(min((evidence[1] for evidence in pair_evidence), default=0.0), 3)
            job_ids = sorted(str(job["job_id"]) for job in group)
            raw_key = f"{ALGORITHM_VERSION}|{'|'.join(job_ids)}"
            cluster_id = "S" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:18].upper()
            clusters.append({
                "cluster_id": cluster_id,
                "algorithm_version": ALGORITHM_VERSION,
                "relationship": "probable_siblings",
                "confidence": confidence,
                "basis": basis,
                "member_job_ids": job_ids,
                "distinct_verified_requisitions": any(item[3] for item in pair_evidence),
                "canonical_jobs_merged": False,
                "source_occurrences_changed": False,
                "members": [{
                    "job_id": str(job["job_id"]), "title": job.get("title"), "company": job.get("company"),
                    "location": job.get("location_raw"), "posted_at": job.get("posted_at"),
                    "source_verification_state": job.get("source_verification_state") or job.get("source_verification"),
                    "evidence_readiness_state": job.get("evidence_readiness_state"),
                    "qualification_readiness_state": job.get("qualification_readiness_state"),
                    "recommendation": job.get("recommendation"),
                } for job in sorted(group, key=lambda item: str(item["job_id"]))],
            })
    clusters.sort(key=lambda item: (-item["confidence"], item["cluster_id"]))
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "advisory_only": True,
        "destructive_merge_controls": False,
        "candidates_scanned": len(candidates),
        "scan_truncated": len(rows) > scan_limit,
        "bucket_limit": bucket_limit,
        "bucket_candidates_truncated": bucket_candidates_truncated,
        "cluster_count": len(clusters),
        "clusters": clusters[:max(1, min(1000, int(output_limit)))],
    }
