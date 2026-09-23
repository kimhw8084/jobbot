from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


METRIC_DEFINITION_VERSION = "chg114-search-quality-v1"
ACTIONABLE = {"APPLY_NOW", "APPLY_VOLUME", "HIGH_VALUE_STRETCH"}
VERIFIED_SOURCES = {"verified_direct_ats", "verified_canonical_ats", "verified_jsonld"}
VERIFIED_DESTINATIONS = {"VERIFIED_ATS", "VERIFIED_EMPLOYER"}
APPLICATION_STAGES = {"APPLIED", "SCREEN", "INTERVIEW", "FINAL", "OFFER", "REJECTED"}


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


def _json_object(value: Any) -> dict[str, Any]:
    try:
        result = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _elapsed_seconds(start: Any, end: Any) -> float | None:
    if not start or not end:
        return None
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0.0, (last - first).total_seconds())
    except (TypeError, ValueError):
        return None


def _timestamp_order(value: Any) -> tuple[int, float | str]:
    if not value:
        return (1, "")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (0, parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return (1, str(value))


def _task_dimensions(row: dict[str, Any]) -> tuple[Any, ...]:
    query = str(row.get("query_text") or "")
    task_key = str(row.get("task_key") or "")
    identity = task_key or hashlib.sha256(query.encode("utf-8")).hexdigest()[:18]
    return (
        str(row.get("strategy_profile") or ""),
        str(row.get("strategy_profile_version") or ""),
        str(row.get("platform") or ""),
        str(row.get("query_family") or ""),
        str(row.get("query_kind") or ""),
        str(row.get("query_pass") or ""),
        identity,
        query,
        _number(row.get("window_days")),
        str(row.get("phase") or ""),
    )


def _family_dimensions(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("strategy_profile") or ""),
        str(row.get("strategy_profile_version") or ""),
        str(row.get("platform") or ""),
        str(row.get("query_family") or ""),
        _number(row.get("window_days")),
        str(row.get("phase") or ""),
    )


def _accumulator() -> dict[str, Any]:
    return {
        "instances": 0, "task_ids": [], "cards_discoveries_persisted": 0,
        "results_seen": 0, "cards_extracted": 0, "cards_persistence_succeeded": 0,
        "source_ids": set(), "canonical_jobs": set(), "sightings": 0,
        "duplicate_sightings": 0, "reported_duplicate_cards": 0,
        "recall_selected_count": 0, "recall_qa_sample_count": 0,
        "detail_attempts": 0, "detail_completions": 0, "detail_partial": 0,
        "detail_failures": 0, "detail_external_blocks": 0, "detail_outcomes": 0,
        "detail_reads": 0, "pages_visited": 0, "scroll_generations": 0,
        "elapsed_seconds": 0.0, "elapsed_observations": 0,
        "source_verified_jobs": set(), "application_destination_verified_jobs": set(),
        "evidence_ready_jobs": set(), "qualification_ready_jobs": set(),
        "actionable_jobs": set(), "hard_reject_jobs": set(), "review_jobs": set(),
        "no_repeat_leakage_jobs": set(), "actionable_hard_reject_leakage_jobs": set(),
        "application_jobs": set(), "screen_jobs": set(), "interview_jobs": set(), "offer_jobs": set(),
        "challenge_task_ids": set(), "auth_task_ids": set(), "external_block_task_ids": set(),
        "interruption_task_ids": set(), "task_failure_ids": set(), "task_completions": 0,
        "_jobs_by_query": {},
    }


def _record_job(acc: dict[str, Any], job: dict[str, Any], result: dict[str, Any]) -> None:
    job_id = str(result.get("canonical_job_id") or "")
    if not job_id:
        return
    acc["canonical_jobs"].add(job_id)
    evidence_state = str(job.get("evidence_readiness_state") or result.get("evidence_readiness_state") or "").upper()
    qualification_state = str(job.get("qualification_readiness_state") or "").upper()
    recommendation = str(job.get("recommendation") or "").upper()
    if evidence_state == "READY":
        acc["evidence_ready_jobs"].add(job_id)
    if qualification_state == "READY":
        acc["qualification_ready_jobs"].add(job_id)
    actionable = evidence_state == "READY" and qualification_state == "READY" and recommendation in ACTIONABLE
    if actionable:
        acc["actionable_jobs"].add(job_id)
    try:
        hard_rejects = json.loads(str(job.get("hard_reject_reasons_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        hard_rejects = []
    if recommendation == "SKIP_HARD_GATE" or (isinstance(hard_rejects, list) and bool(hard_rejects)):
        acc["hard_reject_jobs"].add(job_id)
        if actionable:
            acc["actionable_hard_reject_leakage_jobs"].add(job_id)
    if recommendation.startswith("REVIEW") or evidence_state == "REVIEW" or qualification_state == "REVIEW":
        acc["review_jobs"].add(job_id)
    gates = _json_object(job.get("qualification_gates_json"))
    no_repeat = gates.get("no_repeat") if isinstance(gates.get("no_repeat"), dict) else {}
    if (str(no_repeat.get("status") or "").lower() == "fail" or str(job.get("application_status") or "NEW").upper() != "NEW"):
        if actionable:
            acc["no_repeat_leakage_jobs"].add(job_id)
    source_state = str(job.get("source_verification_state") or job.get("source_verification") or "").lower()
    if source_state in VERIFIED_SOURCES:
        acc["source_verified_jobs"].add(job_id)
    if str(job.get("application_destination_verification_state") or "").upper() in VERIFIED_DESTINATIONS:
        acc["application_destination_verified_jobs"].add(job_id)


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _public_metrics(acc: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in acc.items() if not key.startswith("_") and not isinstance(value, set)}
    for key, value in acc.items():
        if isinstance(value, set):
            public_key = key.removesuffix("_jobs") + "_count" if key.endswith("_jobs") else key
            result[public_key] = len(value)
    for internal, public in (
        ("challenge_task_ids", "challenges"), ("auth_task_ids", "auth_blocks"),
        ("external_block_task_ids", "external_blocks"), ("interruption_task_ids", "interruptions"),
        ("task_failure_ids", "task_failures"),
    ):
        result[public] = len(acc[internal])
    persisted = int(result.get("cards_discoveries_persisted", 0))
    sightings = int(result.get("sightings", 0))
    duplicate = int(result.get("duplicate_sightings", 0))
    result["duplicate_sighting_ratio"] = _ratio(duplicate, sightings)
    result["detail_completion_rate"] = _ratio(result.get("detail_completions", 0), result.get("detail_outcomes", 0))
    ready = int(result.get("evidence_ready_count", 0))
    qualified = int(result.get("actionable_count", 0))
    reads = int(result.get("detail_reads", 0))
    result["detail_reads_per_evidence_ready"] = _ratio(reads, ready)
    result["detail_reads_per_actionable"] = _ratio(reads, qualified)
    result["cards_persisted"] = persisted
    result["unique_source_ids"] = int(result.get("source_ids", 0))
    result["unique_canonical_jobs"] = int(result.get("canonical_count", 0))
    result["recall_selected"] = int(result.get("recall_selected_count", 0))
    result["recall_qa_samples"] = int(result.get("recall_qa_sample_count", 0))
    result["source_verification_conversion"] = _ratio(result.get("source_verified_count", 0), result.get("canonical_count", 0))
    result["verified_application_destination_conversion"] = _ratio(result.get("application_destination_verified_count", 0), result.get("canonical_count", 0))
    result["cards_discovered_total_sightings"] = sightings
    result["task_elapsed_seconds"] = round(float(result.get("elapsed_seconds", 0.0)), 3)
    result["mean_task_elapsed_seconds"] = _ratio(result.get("elapsed_seconds", 0.0), result.get("elapsed_observations", 0))
    result["search_detail_cost_units"] = int(result.get("pages_visited", 0)) + reads
    result["cost_per_evidence_ready"] = _ratio(result["search_detail_cost_units"], ready)
    result["cost_per_actionable"] = _ratio(result["search_detail_cost_units"], qualified)
    return result


def search_quality_metrics(conn: Any, *, limit: int = 500) -> dict[str, Any]:
    """Reproducible aggregation over task, discovery, job-readiness and funnel ledgers."""
    task_rows = conn.execute("""SELECT task_id,browser_run_id,task_key,platform,query_text,window_days,phase,
          status,started_at,completed_at,execution_rank,priority,baseline_execution_rank,effective_execution_rank,
          learned_order_reason,learned_order_sample_size,ordering_algorithm_version,results_seen,jobs_recorded,
          cards_extracted,cards_persistence_succeeded,duplicate_cards,pages_visited,scroll_generation,detail_count_read,
          challenge_reason,last_error,safety_stop_reason,search_profile,strategy_profile,strategy_profile_version,
          query_family,query_kind,query_pass,initial_order
        FROM browser_search_tasks ORDER BY task_id""").fetchall()
    tasks: dict[int, dict[str, Any]] = {}
    task_accs: dict[tuple[Any, ...], dict[str, Any]] = {}
    family_accs: dict[tuple[Any, ...], dict[str, Any]] = {}
    task_meta: dict[tuple[Any, ...], dict[str, Any]] = {}
    family_meta: dict[tuple[Any, ...], dict[str, Any]] = {}
    task_family: dict[int, tuple[Any, ...]] = {}
    for raw in task_rows:
        row = dict(raw)
        task_id = int(row["task_id"])
        tasks[task_id] = row
        tdim = _task_dimensions(row)
        fdim = _family_dimensions(row)
        task_family[task_id] = fdim
        tacc = task_accs.setdefault(tdim, _accumulator())
        facc = family_accs.setdefault(fdim, _accumulator())
        for acc in (tacc, facc):
            acc["instances"] += 1
            acc["task_ids"].append(task_id)
            acc["pages_visited"] += _number(row.get("pages_visited"))
            acc["scroll_generations"] += _number(row.get("scroll_generation"))
            acc["detail_reads"] += _number(row.get("detail_count_read"))
            acc["results_seen"] += _number(row.get("results_seen"))
            acc["cards_extracted"] += _number(row.get("cards_extracted"))
            acc["cards_persistence_succeeded"] += _number(row.get("cards_persistence_succeeded"))
            acc["reported_duplicate_cards"] += _number(row.get("duplicate_cards"))
            if row.get("status") in {"challenged", "deferred_by_platform"} or row.get("challenge_reason"):
                acc["challenge_task_ids"].add(task_id)
            if row.get("status") in {"auth_required", "deferred_by_platform"} or "auth" in str(row.get("last_error") or "").lower():
                acc["auth_task_ids"].add(task_id)
            if row.get("status") == "external_blocked" or "external block" in str(row.get("last_error") or "").lower():
                acc["external_block_task_ids"].add(task_id)
            if row.get("status") in {"incomplete", "stopped"} or row.get("safety_stop_reason"):
                acc["interruption_task_ids"].add(task_id)
            if row.get("status") in {"failed", "auth_required"}:
                acc["task_failure_ids"].add(task_id)
            if row.get("status") in {"exhausted", "completed", "complete"}:
                acc["task_completions"] += 1
            elapsed = _elapsed_seconds(row.get("started_at"), row.get("completed_at"))
            if elapsed is not None:
                acc["elapsed_seconds"] += elapsed
                acc["elapsed_observations"] += 1
        task_meta[tdim] = row
        family_meta[fdim] = {"strategy_profile": row["strategy_profile"], "strategy_profile_version": row["strategy_profile_version"],
                             "platform": row["platform"], "query_family": row["query_family"],
                             "age_window_days": row["window_days"], "phase": row["phase"]}

    result_rows = conn.execute("""SELECT r.result_id,r.task_id,r.source_site,r.source_job_id,r.source_url,r.canonical_job_id,
          r.first_seen_at,r.sighting_count,r.detail_read,r.detail_status,r.detail_attempts,r.content_state,
          r.recall_selected,r.recall_qa_sample,r.evidence_readiness_state result_evidence_readiness_state,
          j.evidence_readiness_state,j.qualification_readiness_state,j.recommendation,j.application_status,
          j.hard_reject_reasons_json,j.qualification_gates_json,j.source_verification_state,j.source_verification,
          j.application_destination_verification_state
        FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id
        LEFT JOIN jobs j ON j.job_id=r.canonical_job_id ORDER BY r.result_id""").fetchall()
    first_touch: dict[tuple[Any, ...], tuple[tuple[Any, ...], tuple[Any, ...], tuple[tuple[int, float | str], int, int]]] = {}
    family_job_tasks: dict[tuple[Any, ...], dict[str, set[tuple[Any, ...]]]] = {}
    canonical_jobs_by_task: dict[tuple[Any, ...], set[str]] = {}
    for raw in result_rows:
        row = dict(raw)
        task = tasks.get(int(row["task_id"]), {})
        tdim = _task_dimensions(task)
        fdim = _family_dimensions(task)
        tacc = task_accs.setdefault(tdim, _accumulator())
        facc = family_accs.setdefault(fdim, _accumulator())
        for acc in (tacc, facc):
            acc["cards_discoveries_persisted"] += 1
            count = max(1, _number(row.get("sighting_count")))
            acc["sightings"] += count
            acc["duplicate_sightings"] += max(0, count - 1)
            acc["detail_attempts"] += _number(row.get("detail_attempts"))
            acc["recall_selected_count"] += int(bool(row.get("recall_selected")))
            acc["recall_qa_sample_count"] += int(bool(row.get("recall_qa_sample")))
            status = str(row.get("detail_status") or "").upper()
            content = str(row.get("content_state") or "").upper()
            if status in {"COMPLETE", "PARTIAL", "FAILED", "RETRYABLE", "EXTERNAL_BLOCKED"}:
                acc["detail_outcomes"] += 1
            if status == "COMPLETE" and content == "COMPLETE":
                acc["detail_completions"] += 1
            if status == "PARTIAL" or content == "PARTIAL":
                acc["detail_partial"] += 1
            if status in {"FAILED", "RETRYABLE"}:
                acc["detail_failures"] += 1
            if status == "EXTERNAL_BLOCKED":
                acc["detail_external_blocks"] += 1
                acc["external_block_task_ids"].add(int(row["task_id"]))
            site = str(row.get("source_site") or "")
            source_id = str(row.get("source_job_id") or row.get("source_url") or "")
            if source_id:
                acc["source_ids"].add((site, source_id))
        job_id = str(row.get("canonical_job_id") or "")
        if job_id:
            canonical_jobs_by_task.setdefault(tdim, set()).add(job_id)
            family_job_tasks.setdefault(fdim, {}).setdefault(job_id, set()).add(tdim)
            job = row
            for acc in (tacc, facc):
                _record_job(acc, job, row)
            touch = (_timestamp_order(row.get("first_seen_at")), int(row["task_id"]), int(row["result_id"]))
            logical_query_dimension = fdim[:4]
            key = (logical_query_dimension, job_id)
            previous = first_touch.get(key)
            if previous is None or touch < previous[2]:
                first_touch[key] = (tdim, fdim, touch)

    for fdim, jobs in family_job_tasks.items():
        family_accs[fdim]["_jobs_by_query"] = jobs

    for raw in conn.execute("SELECT task_id,event_type FROM browser_events WHERE task_id IS NOT NULL"):
        task_id, event_type = int(raw[0]), str(raw[1] or "").lower()
        tdim = _task_dimensions(tasks[task_id]) if task_id in tasks else None
        fdim = task_family.get(task_id)
        if tdim is None or fdim is None:
            continue
        targets = (task_accs[tdim], family_accs[fdim])
        if "challenge" in event_type:
            for acc in targets: acc["challenge_task_ids"].add(task_id)
        if "auth" in event_type or "sign_in" in event_type:
            for acc in targets: acc["auth_task_ids"].add(task_id)
        if "external_block" in event_type:
            for acc in targets: acc["external_block_task_ids"].add(task_id)
        if "interrupt" in event_type or event_type in {"run_stopped", "task_stopped"}:
            for acc in targets: acc["interruption_task_ids"].add(task_id)
        if event_type in {"job_error", "task_failed", "result_persistence_failed"}:
            for acc in targets: acc["task_failure_ids"].add(task_id)

    if first_touch:
        attribution_by_job: dict[str, list[tuple[tuple[Any, ...], tuple[Any, ...]]]] = {}
        for (_, job_id), (tdim, fdim, _) in first_touch.items():
            attribution_by_job.setdefault(job_id, []).append((tdim, fdim))
        events = conn.execute(
            "SELECT job_id,event_type FROM application_events WHERE upper(event_type) IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED')",
        ).fetchall()
        for event in events:
            job_id, stage = str(event[0]), str(event[1]).upper()
            for tdim, fdim in attribution_by_job.get(job_id, ()):
                targets = (task_accs[tdim], family_accs[fdim])
                if stage in APPLICATION_STAGES:
                    for acc in targets: acc["application_jobs"].add(job_id)
                if stage == "SCREEN":
                    for acc in targets: acc["screen_jobs"].add(job_id)
                if stage in {"INTERVIEW", "FINAL"}:
                    for acc in targets: acc["interview_jobs"].add(job_id)
                if stage == "OFFER":
                    for acc in targets: acc["offer_jobs"].add(job_id)

    def task_document(dim: tuple[Any, ...], acc: dict[str, Any]) -> dict[str, Any]:
        meta = task_meta[dim]
        metric = _public_metrics(acc)
        metric.update({
            "strategy_profile": dim[0], "profile_version": dim[1], "platform": dim[2],
            "query_family": dim[3], "query_kind": dim[4], "query_pass": dim[5],
            "task_key": dim[6], "query": dim[7], "age_window_days": dim[8], "phase": dim[9],
            "task_ids": sorted(set(acc["task_ids"])),
        })
        return metric

    def family_document(dim: tuple[Any, ...], acc: dict[str, Any]) -> dict[str, Any]:
        metric = _public_metrics(acc)
        metric.update(family_meta[dim])
        metric["multi_query_canonical_jobs"] = sum(1 for values in family_job_tasks.get(dim, {}).values() if len(values) > 1)
        return metric

    task_metrics = [task_document(key, value) for key, value in task_accs.items()]
    family_metrics = [family_document(key, value) for key, value in family_accs.items()]
    for values in (task_metrics, family_metrics):
        values.sort(key=lambda item: (item["platform"], item["phase"], item["query_family"], item.get("query_kind", ""), item.get("query_pass", ""), item.get("query", "")))
    run = conn.execute("SELECT browser_run_id FROM browser_runs ORDER BY browser_run_id DESC LIMIT 1").fetchone()
    ordering: list[dict[str, Any]] = []
    if run:
        for row in conn.execute("""SELECT task_id,platform,phase,query_family,query_kind,query_pass,task_key,query_text,
              baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,ordering_algorithm_version
            FROM browser_search_tasks WHERE browser_run_id=? ORDER BY platform,phase,effective_execution_rank,priority,task_id""", (run[0],)):
            ordering.append({key: row[key] for key in row.keys()})
    source_occurrences = [dict(row) for row in conn.execute("""SELECT strategy_profile,strategy_profile_version profile_version,
          source_site platform,query_family,query_kind,query_pass,COUNT(*) source_occurrence_rows,
          COUNT(DISTINCT COALESCE(NULLIF(source_job_id,''),source_url)) unique_source_ids,
          COUNT(DISTINCT job_id) unique_canonical_jobs,
          COUNT(DISTINCT CASE WHEN source_verification_state IN ('verified_direct_ats','verified_canonical_ats','verified_jsonld') THEN job_id END) source_verified_jobs,
          COUNT(DISTINCT CASE WHEN application_destination_verification_state IN ('VERIFIED_ATS','VERIFIED_EMPLOYER') THEN job_id END) verified_application_destinations
        FROM source_occurrences WHERE strategy_profile<>''
        GROUP BY strategy_profile,strategy_profile_version,source_site,query_family,query_kind,query_pass
        ORDER BY platform,query_family,query_kind,query_pass""")]
    maximum = max(1, min(2000, int(limit)))
    return {
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "metric_definitions": {
            "cards_persisted": "Count of durable search_task_results rows; repeat card sightings are separately reported.",
            "source_occurrence_rows": "Durable source_occurrences identities, grouped by their recorded CHG-170 profile/version/platform/family/kind/pass provenance; the table has no task age-window or phase field.",
            "unique_source_ids": "Distinct (source_site, source_job_id), falling back to source_url, within the displayed grouping.",
            "unique_canonical_jobs": "Distinct linked canonical job_id within the displayed grouping.",
            "duplicate_sighting_ratio": "Sum(max(sighting_count-1, 0)) divided by total durable sightings.",
            "qualified_yield": "Distinct jobs with evidence_readiness_state=READY, qualification_readiness_state=READY, and an actionable recommendation.",
            "funnel_attribution": "Each downstream canonical job-stage is credited once per strategy profile/version + platform + query family, to its earliest durable discovery in that group; duplicate sightings and later query touches do not multiply events.",
            "detail_cost": "Pages visited plus detail reads; cost per outcome is null when the outcome denominator is zero.",
        },
        "attribution_rule": "Occurrence metrics include every durable task result. Canonical yield is distinct job_id per query/family grouping. Funnel events use earliest first_seen_at, then task_id, then result_id within profile/version/platform/family.",
        "task_metrics": task_metrics[:maximum],
        "family_metrics": family_metrics[:maximum],
        "source_occurrence_metrics": source_occurrences[:maximum],
        "current_order": ordering[:maximum],
    }
