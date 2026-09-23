from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from typing import Any, Callable, Mapping

from .. import crawl_observations
from .. import legacy_engine as j
from ..discoveries import block_detail, fail_detail, finish_detail, upsert_card
from .models import AcquisitionRecord


EventWriter = Callable[[Any, int, int, str, str, Mapping[str, Any]], None]
Reconciler = Callable[[Any, int, int], None]


def _recall_decision(strategy: dict[str, Any], platform: str, source_job_id: str, source_url: str,
                     title: str, query_family: str) -> tuple[bool, bool, str, int]:
    configured_families = {
        str(family.get("id")) for family in strategy.get("_live_search_profile", {}).get("families", [])
        if family.get("enabled", True) and family.get("minimum_deep_recall", False)
    }
    probe = j.Job(source_site=platform, source_job_id=source_job_id, canonical_url=source_url,
                  title=title, remote_status="unknown")
    selected, reason = j.recall_prefilter(probe, strategy)
    sample_key = f"{platform}|{source_job_id}|{source_url}".encode("utf-8", errors="ignore")
    qa_sample = int(hashlib.sha256(sample_key).hexdigest()[:8], 16) % 20 == 0
    if query_family and query_family in configured_families:
        if reason in {"excluded occupation family", "explicit out-of-scope title"}:
            return False, bool(qa_sample), j.clean_text(reason), 1 if qa_sample else 2
        return True, False, f"active live-search family recall: {query_family}", 0
    return bool(selected), bool(qa_sample), j.clean_text(reason), 0 if selected else (1 if qa_sample else 2)


def persist_card(
    conn: sqlite3.Connection, *, run_id: int, task: Any, strategy: dict[str, Any], record: AcquisitionRecord,
    provider_name: str, provider_run_id: str, acquisition_mode: str,
    event: EventWriter, reconcile: Reconciler,
) -> dict[str, Any]:
    """Persist identity and card metadata through the same RPC/provider boundary."""
    source = j.clean_text(record.source_surface)
    source_id = j.clean_text(record.source_job_id)
    source_url = j.canonical_url(j.clean_text(record.discovery_url()))
    if not source or not (source_id or source_url):
        return {"ok": False, "error": "insufficient_result_identity"}
    if source != j.clean_text(task["platform"]):
        return {"ok": False, "error": "source_surface_task_mismatch"}
    card = record.card_fields()
    selected, qa_sample, recall_reason, enrichment_priority = _recall_decision(
        strategy, source, source_id, source_url,
        j.clean_text(card.get("title")), j.clean_text(task["query_family"]),
    )
    discovery, duplicate = upsert_card(
        conn, run_id=run_id, task_id=int(task["task_id"]), platform=source,
        source_job_id=source_id, source_url=source_url,
        title_hint=j.clean_text(card.get("title")), company_hint=j.clean_text(card.get("company")),
        location_hint=j.clean_text(card.get("location")), posted_text=j.clean_text(card.get("posted_text")),
        posted_age_days=card.get("posted_age_days"), card=card,
        eligible_for_detail=bool(card.get("eligible_for_detail", True)),
        recall_selected=selected, recall_qa_sample=qa_sample, recall_reason=recall_reason,
        enrichment_priority=enrichment_priority,
        strategy_profile=j.clean_text(task["strategy_profile"]),
        strategy_profile_version=j.clean_text(task["strategy_profile_version"]),
        query_family=j.clean_text(task["query_family"]), query_kind=j.clean_text(task["query_kind"]),
        query_pass=j.clean_text(task["query_pass"]), initial_order=int(task["initial_order"] or 0),
    )
    result_id = int(discovery.result_id)
    url_roles = record.source_urls
    conn.execute("""UPDATE search_task_results SET acquisition_provider=?,acquisition_mode=?,provider_run_id=?,
        provider_record_id=?,provider_observed_at=?,provider_metadata_json=?,query_task_key=?,phase=?,
        board_detail_url=CASE WHEN ?<>'' THEN ? ELSE board_detail_url END,
        observed_board_apply_url=CASE WHEN ?<>'' THEN ? ELSE observed_board_apply_url END,
        employer_job_url=CASE WHEN ?<>'' THEN ? ELSE employer_job_url END,
        ats_requisition_url=CASE WHEN ?<>'' THEN ? ELSE ats_requisition_url END
        WHERE result_id=?""", (
        provider_name, acquisition_mode, provider_run_id, record.provider_record_id,
        record.observed_at, json.dumps(dict(record.raw_metadata), ensure_ascii=False, sort_keys=True),
        str(task["task_key"] or ""), str(task["phase"] or ""),
        j.canonical_url(j.clean_text(url_roles.get("board_detail_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("board_detail_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("observed_board_apply_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("observed_board_apply_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("employer_job_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("employer_job_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("ats_requisition_url") or "")),
        j.canonical_url(j.clean_text(url_roles.get("ats_requisition_url") or "")),
        result_id,
    ))
    conn.execute("""UPDATE browser_search_tasks SET acquisition_provider=?,acquisition_mode=?,provider_run_id=?
        WHERE task_id=?""", (provider_name, acquisition_mode, provider_run_id, int(task["task_id"])))
    if duplicate:
        conn.execute("UPDATE browser_search_tasks SET duplicate_sightings=duplicate_sightings+1 WHERE task_id=?", (int(task["task_id"]),))
    pending_count = int(conn.execute(
        "SELECT COUNT(*) FROM search_task_results WHERE task_id=? "
        "AND detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED','DEFERRED_RECALL','PARTIAL')",
        (int(task["task_id"]),),
    ).fetchone()[0] or 0)
    event(conn, run_id, int(task["task_id"]), "result_discovered", f"{source}: {source_id or source_url}",
          {"acquisition_provider": provider_name, "provider_record_id": record.provider_record_id})
    reconcile(conn, run_id, int(task["task_id"]))
    return {"ok": True, "duplicate": duplicate, "result_id": result_id,
            "detail_status": discovery.detail_status, "pending_count": pending_count}


def _unsafe_detail_reason(detail: dict[str, Any], title: str, page_url: str) -> str:
    surface = j.clean_text(detail.get("page_type") or detail.get("surface") or "").lower()
    haystack = " ".join((surface, title, page_url, j.clean_text(detail.get("challenge_reason") or ""))).lower()
    markers = (
        "tunnel connection failed", "could not establish connection", "receiving end does not exist",
        "page load timed out", "sign in", "log in", "login", "checkpoint", "authwall",
        "captcha", "challenge", "access denied", "security check", "temporarily unavailable",
    )
    if surface in {"error", "login", "challenge", "interstitial"}:
        return surface
    return next((marker for marker in markers if marker in haystack), "")


def persist_detail(
    conn: sqlite3.Connection, store: Any, *, run_id: int, task_id: int, result_id: int,
    raw: Mapping[str, Any], evidence: Mapping[str, Any], cfg: Mapping[str, Any],
    strategy: dict[str, Any], candidate: dict[str, Any], provider_name: str,
    provider_run_id: str, acquisition_mode: str, provider_record_id: str = "",
    provider_metadata: Mapping[str, Any] | None = None, event: EventWriter,
    reconcile: Reconciler, cache_bundle: Any = None, source_build: str = "",
) -> dict[str, Any]:
    """Normalize and persist detail using the existing scorer and canonical ledger."""
    task = conn.execute("SELECT * FROM browser_search_tasks WHERE task_id=? AND browser_run_id=?", (task_id, run_id)).fetchone()
    if task is None:
        return {"ok": False, "error": "task_not_found"}
    title = j.clean_text(raw.get("title"))
    company = j.clean_text(raw.get("company"))
    desc = j.strip_html(raw.get("description") or "")[:180000]
    url = j.canonical_url(j.clean_text(raw.get("canonical_url") or raw.get("url") or ""))
    sid = j.clean_text(raw.get("source_job_id") or "")
    if not title or not url:
        return {"ok": False, "error": "insufficient_job_identity"}
    discovery_url = url
    if result_id:
        observed = conn.execute(
            "SELECT source_job_id,source_url FROM search_task_results WHERE result_id=? AND task_id=?",
            (result_id, task_id),
        ).fetchone()
        if observed is None:
            return {"ok": False, "error": "result_not_found"}
        if observed["source_job_id"] and sid and str(observed["source_job_id"]) != sid:
            return {"ok": False, "error": "detail_identity_mismatch"}
        discovery_url = j.canonical_url(j.clean_text(observed["source_url"] or "")) or url
        detail_acquisition = evidence.get("detail_acquisition") if isinstance(evidence.get("detail_acquisition"), dict) else {}
        mode = j.clean_text(detail_acquisition.get("mode") or evidence.get("acquisition_mode") or "")
        if mode and mode not in {"search_pane", "cache", "user_reenrichment", "managed_provider"}:
            return {"ok": False, "error": "detail_acquisition_mode_not_allowed", "mode": mode}
    unsafe = _unsafe_detail_reason(dict(evidence), title, j.clean_text(raw.get("page_url") or url))
    if unsafe:
        if result_id:
            block_detail(conn, result_id, f"detail surface rejected as {unsafe}")
        event(conn, run_id, task_id, "unsafe_detail_rejected", f"{unsafe}: {title}", {})
        reconcile(conn, run_id, task_id)
        return {"ok": False, "error": "unsafe_detail_surface", "surface": unsafe}
    if not desc:
        detail_status = fail_detail(
            conn, result_id, "detail identity had no substantive description",
            max_attempts=int(cfg.get("runtime", {}).get("watchdog_retries", 3) or 3),
        ) if result_id else "RETRYABLE"
        event(conn, run_id, task_id, "detail_content_missing", f"{title}: substantive description missing", {})
        reconcile(conn, run_id, task_id)
        return {"ok": False, "error": "content_incomplete", "detail_status": detail_status}

    source_site = j.clean_text(task["platform"])
    location = j.clean_text(raw.get("location") or "")
    if location.casefold() in {"[object object]", "undefined", "null"}:
        location = ""
    remote_status = j.clean_text(raw.get("remote_status") or "unknown").lower()
    if remote_status in {"remote", "fully remote", "100 remote", "us remote"} and not (
        location or re.search(r"\bremote\b|work[ -]?from[ -]?home|\bwfh\b", desc, re.I)
    ):
        remote_status = "unknown"
    apply_candidate = j.canonical_url(j.clean_text(raw.get("apply_url") or ""))
    apply_url = apply_candidate if apply_candidate and apply_candidate != url else ""
    provenance = {
        "source_type": "board_detail", "detail_source_type": "board_detail",
        "identity": "observed_detail_identity",
        "card_metadata": "search_card" if raw.get("search_card") else "detail_surface",
        "description": "observed_substantive_detail",
        "location": "observed" if location else "unknown", "location_source_type": "board_detail",
        "remote": "observed_detail_text" if remote_status in {"remote", "fully remote", "100 remote", "us remote"} else "unknown",
        "remote_source_type": "board_detail",
        "salary": "observed_detail_text" if j.clean_text(raw.get("salary_text") or "") else "unknown",
        "salary_source_type": "board_detail",
        "employment_type": "observed_detail_text" if j.clean_text(raw.get("employment_type") or "") else "unknown",
        "employment_type_source_type": "board_detail",
        "posted_at": "observed_search_card_or_detail" if j.clean_text(raw.get("posted_at") or "") else "unknown",
        "posted_at_source_type": "search_card" if raw.get("search_card") else "board_detail",
        "requirements": "employer_description_requirement_extraction",
        "requirements_source_type": "board_detail",
        "application_destination": "observed_distinct_destination" if apply_url else "unknown_board_destination",
        "application_destination_source_type": "board_detail",
        "remote_filter_intent": bool(task["remote_required"]),
    }
    metadata = dict(provider_metadata or {})
    job_raw = {
        "browser_v3": True, "browser_run_id": run_id, "browser_task_id": task_id,
        "platform": source_site, "query_text": task["query_text"], "search_profile": task["search_profile"],
        "career_lane": task["career_lane"], "strategy_profile": task["strategy_profile"],
        "strategy_profile_version": task["strategy_profile_version"], "query_family": task["query_family"],
        "query_kind": task["query_kind"], "query_pass": task["query_pass"],
        "initial_order": task["initial_order"], "phase": task["phase"],
        "query_task_key": task["task_key"], "page_url": j.clean_text(raw.get("page_url") or url),
        "valid_through": j.clean_text(raw.get("valid_through") or ""),
        "remote_filter_intent": bool(task["remote_required"]), "source_payload": dict(raw),
        "_discovery_company": j.clean_text((raw.get("search_card") or {}).get("company") if isinstance(raw.get("search_card"), dict) else ""),
        "discovery_url": discovery_url, "board_detail_url": url,
        "observed_board_apply_url": apply_candidate,
        "employer_job_url": j.canonical_url(j.clean_text(raw.get("employer_job_url") or "")),
        "ats_requisition_url": j.canonical_url(j.clean_text(raw.get("ats_requisition_url") or "")),
        # Provider URL roles are observations. This service never accepts a
        # provider-supplied verified_application_url or verification state.
        "verified_application_url": "",
        "acquisition_provider": provider_name, "acquisition_mode": acquisition_mode,
        "provider_run_id": provider_run_id, "provider_record_id": provider_record_id,
        "provider_observed_at": j.clean_text(raw.get("provider_observed_at") or ""),
        "provider_metadata": metadata,
    }
    job = j.Job(
        source_site=source_site, source_job_id=sid, canonical_url=url, apply_url=apply_url,
        title=title, company=company, location_raw=location, remote_status=remote_status,
        employment_type=j.clean_text(raw.get("employment_type") or ""),
        salary_text=j.clean_text(raw.get("salary_text") or ""), posted_at=j.clean_text(raw.get("posted_at") or ""),
        description=desc, category=j.clean_text(raw.get("category") or ""),
        tags=[j.clean_text(item) for item in (raw.get("tags") or []) if j.clean_text(item)],
        raw=job_raw,
    )
    setattr(job, "_mode", "deep")
    j.score_job(job, strategy, candidate)
    ledger_status = store.upsert(job, run_id=run_id, commit=False)
    fields = {"new": "jobs_new", "updated": "jobs_updated", "unchanged": "jobs_unchanged"}
    if ledger_status in fields:
        field = fields[ledger_status]
        conn.execute(f"UPDATE browser_search_tasks SET jobs_recorded=jobs_recorded+1,{field}={field}+1 WHERE task_id=?", (task_id,))
        conn.execute(f"UPDATE browser_runs SET jobs_recorded=jobs_recorded+1,{field}={field}+1 WHERE browser_run_id=?", (run_id,))
        conn.execute("UPDATE browser_platform_runs SET jobs_recorded=jobs_recorded+1 WHERE browser_run_id=? AND platform=?", (run_id, source_site))
    if ledger_status == "new":
        conn.execute("UPDATE browser_search_tasks SET unique_jobs_recorded=unique_jobs_recorded+1 WHERE task_id=?", (task_id,))
    else:
        conn.execute("UPDATE browser_search_tasks SET duplicate_sightings=duplicate_sightings+1 WHERE task_id=?", (task_id,))
    job_id = store.resolve_job_id(job)
    description_state = "COMPLETE" if len(desc) >= 250 else "PARTIAL_TOO_SHORT"
    content_state = "COMPLETE" if description_state == "COMPLETE" else "PARTIAL"
    enrichment_status = "ENRICHED" if content_state == "COMPLETE" else "PARTIAL"
    location_state = "OBSERVED" if location else "UNKNOWN"
    remote_state = "OBSERVED" if provenance["remote"] != "unknown" else "UNKNOWN"
    apply_state = "OBSERVED" if apply_url else "UNKNOWN"
    conn.execute("""UPDATE jobs SET description_state=?,content_state=?,enrichment_status=?,enrichment_last_error='',
        location_evidence_state=?,remote_evidence_state=?,apply_destination_state=?,evidence_provenance_json=?
        WHERE job_id=?""", (description_state, content_state, enrichment_status, location_state, remote_state,
                            apply_state, json.dumps(provenance, ensure_ascii=False), job_id))
    evidence_values = (
        job.identity_evidence_state, job.detail_evidence_state, job.requirements_evidence_state,
        job.source_verification, job.application_destination_verification_state, job.evidence_readiness_state,
        json.dumps(job.evidence_missing, ensure_ascii=False), json.dumps(job.evidence_blocking, ensure_ascii=False),
    )
    if result_id:
        finish_detail(conn, result_id, job_id, content_state=content_state)
        conn.execute("""UPDATE search_task_results SET discovery_url=?,board_detail_url=?,observed_board_apply_url=?,
            employer_job_url=?,ats_requisition_url=?,verified_application_url=?,identity_evidence_state=?,detail_evidence_state=?,
            requirements_evidence_state=?,source_verification_state=?,application_destination_verification_state=?,
            evidence_readiness_state=?,evidence_missing_json=?,evidence_blocking_json=?,acquisition_provider=?,
            acquisition_mode=?,provider_run_id=?,provider_record_id=?,provider_observed_at=?,provider_metadata_json=?,
            query_task_key=?,phase=? WHERE result_id=?""", (
            discovery_url, url, apply_candidate, job.employer_job_url, job.ats_requisition_url, job.verified_application_url,
            *evidence_values, provider_name, acquisition_mode, provider_run_id, provider_record_id,
            j.clean_text(raw.get("provider_observed_at") or ""), json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            str(task["task_key"] or ""), str(task["phase"] or ""), result_id,
        ))
    else:
        conn.execute("""UPDATE search_task_results SET canonical_job_id=?,detail_read=1,detail_status=?,content_state=?,
            detail_completed_at=?,discovery_url=?,board_detail_url=?,observed_board_apply_url=?,employer_job_url=?,ats_requisition_url=?,
            verified_application_url=?,identity_evidence_state=?,detail_evidence_state=?,requirements_evidence_state=?,
            source_verification_state=?,application_destination_verification_state=?,evidence_readiness_state=?,
            evidence_missing_json=?,evidence_blocking_json=?,acquisition_provider=?,acquisition_mode=?,provider_run_id=?,
            provider_record_id=?,provider_observed_at=?,provider_metadata_json=?,query_task_key=?,phase=?
            WHERE task_id=? AND source_site=? AND source_job_id=? AND source_url=?""", (
            job_id, "COMPLETE" if content_state == "COMPLETE" else "PARTIAL", content_state, j.now_iso(),
            discovery_url, url, apply_candidate, job.employer_job_url, job.ats_requisition_url, job.verified_application_url,
            *evidence_values, provider_name, acquisition_mode, provider_run_id, provider_record_id,
            j.clean_text(raw.get("provider_observed_at") or ""), json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            str(task["task_key"] or ""), str(task["phase"] or ""), task_id, source_site, sid, url,
        ))
    detail_acquisition = evidence.get("detail_acquisition") if isinstance(evidence.get("detail_acquisition"), dict) else {}
    conn.execute("UPDATE browser_search_tasks SET detail_acquisition_mode=? WHERE task_id=?",
                 (j.clean_text(detail_acquisition.get("mode") or evidence.get("acquisition_mode") or acquisition_mode), task_id))
    cache_published = False
    if content_state == "COMPLETE" and cache_bundle is not None:
        try:
            card_row = conn.execute("SELECT card_json FROM search_task_results WHERE result_id=?", (result_id,)).fetchone() if result_id else None
            cache_published = crawl_observations.publish(
                cache_bundle, platform=source_site, source_job_id=sid, source_url=url,
                card=json.loads(card_row["card_json"] or "{}") if card_row else {},
                title=title, company=company, location=location,
                job={"source_job_id": sid, "canonical_url": url, "title": title, "company": company,
                     "location": location, "remote_status": remote_status,
                     "employment_type": job.employment_type, "salary_text": job.salary_text,
                     "posted_at": job.posted_at, "description": desc,
                     "valid_through": j.clean_text(raw.get("valid_through") or "")},
                evidence={"detail_acquisition": evidence.get("detail_acquisition", {})},
                source_build=source_build,
            )
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
            cache_published = False
    event(conn, run_id, task_id, "job_recorded", f"{ledger_status}: {job.title} — {job.company}", {
        "job_id": job_id, "ledger_status": ledger_status, "recommendation": job.recommendation,
        "content_state": content_state, "apply_destination_state": apply_state,
        "cache_published": cache_published, "acquisition_provider": provider_name,
    })
    reconcile(conn, run_id, task_id)
    return {"ok": True, "ledger_status": ledger_status, "job_id": job_id,
            "recommendation": job.recommendation, "title": job.title, "company": job.company,
            "content_state": content_state, "enrichment_status": enrichment_status,
            "cache_published": cache_published}
