from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import browser_tasks, legacy_engine as j
from ..config import ConfigBundle
from ..db import Database
from ..search_plan import SearchTask, compile_plan, compile_staged_plan
from ..strategy_runtime import fallback_activation_enabled, with_fallback_activation
from .ingestion import persist_card, persist_detail
from .models import AcquisitionRecord, ProviderBatch, ProviderCompletionState, ProviderFailure, ProviderFailureClass
from .protocol import ProviderAdapter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _task_payload(task: SearchTask) -> dict[str, Any]:
    return {
        "task_key": task.task_key, "platform": task.platform, "query_text": task.query,
        "strategy_profile": task.strategy_profile, "strategy_profile_version": task.strategy_profile_version,
        "query_family": task.query_family, "query_kind": task.query_kind, "query_pass": task.query_pass,
        "initial_order": task.initial_order, "window_days": task.age_days, "search_profile": task.profile,
        "career_lane": task.career_lane, "resume_variant": task.resume_variant, "priority": task.priority,
        "search_url": task.search_url, "execution_rank": task.execution_rank, "phase": task.phase,
    }


def _provider_mode(provider: ProviderAdapter) -> str:
    return "file_import" if provider.name in {"jsonl-file", "json-file"} else "managed_provider"


def acquire(
    bundle: ConfigBundle, provider: ProviderAdapter, *, mode: str = "staged",
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    """Run the selected provider across the frozen CHG-170 tasks.

    Query tasks remain incomplete unless each adapter batch carries explicit,
    non-empty provider completion evidence. All committed sightings survive a
    later provider or detail failure.
    """
    chosen = platforms or list(browser_tasks.PLATFORMS)
    bad = sorted(set(chosen) - set(browser_tasks.PLATFORMS))
    if bad:
        raise ValueError(f"unsupported source surface(s): {', '.join(bad)}")
    db = Database(bundle)
    db.migrate()
    store = j.PrecisionStore(db.path)
    browser_tasks.init_browser_schema(store.conn)
    strategy = with_fallback_activation(
        bundle.strategy, fallback_activation_enabled(store.conn, bundle.runtime),
    )
    if mode == "staged":
        planned = compile_staged_plan(bundle, chosen, include_fallback=fallback_activation_enabled(store.conn, bundle.runtime))
    elif mode == "staged_recent":
        planned = compile_staged_plan(bundle, chosen, ("A_FASTEST_DOOR_RECENT", "B_REMAINING_CORE_RECENT"))
    elif mode == "staged_deep":
        planned = compile_staged_plan(bundle, chosen, ("C_DEEP_BACKFILL",))
    elif mode in {"fast", "deep"}:
        planned = compile_plan(bundle, mode, chosen)
    else:
        store.close()
        raise ValueError(f"unsupported acquisition mode: {mode}")

    payloads = [_task_payload(task) for task in planned]
    ordering_fields, ordering_metadata = browser_tasks._freeze_search_order(store.conn, payloads, bundle)
    provider_run_id = str(provider.run_id or uuid.uuid4())
    provider_name = str(provider.name or "unknown-provider")
    acquisition_mode = _provider_mode(provider)
    now = _now()
    conn = store.conn
    cursor = conn.execute(
        """INSERT INTO browser_runs(version,mode,platform,status,created_at,started_at,notes,
           acquisition_provider,acquisition_mode,provider_run_id,ordering_config_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (browser_tasks.V3_VERSION, "acquisition-v2", ",".join(chosen), "running", now, now,
         f"Provider-neutral acquisition over {len(planned)} frozen CHG-170 tasks.",
         provider_name, acquisition_mode, provider_run_id, json.dumps(ordering_metadata, sort_keys=True)),
    )
    run_id = int(cursor.lastrowid)
    for platform in chosen:
        count = sum(1 for task in planned if task.platform == platform)
        conn.execute("INSERT OR REPLACE INTO browser_platform_runs(browser_run_id,platform,tasks_total) VALUES(?,?,?)", (run_id, platform, count))
    for payload in payloads:
        task_key = (payload["task_key"], payload["phase"])
        conn.execute(
            """INSERT INTO browser_search_tasks(
              browser_run_id,platform,query_text,remote_required,window_days,sort_order,search_url,max_results,
              status,created_at,search_profile,career_lane,resume_variant,priority,execution_rank,skip_old_cards,
              task_key,phase,strategy_profile,strategy_profile_version,query_family,query_kind,query_pass,initial_order,
              baseline_execution_rank,effective_execution_rank,learned_order_reason,learned_order_sample_size,
              ordering_algorithm_version,acquisition_provider,acquisition_mode,provider_run_id,query_task_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, payload["platform"], payload["query_text"], 1, payload["window_days"], "date",
             payload["search_url"], None, "queued", now, payload["search_profile"], payload["career_lane"],
             payload["resume_variant"], payload["priority"], payload["execution_rank"], 1, payload["task_key"],
             payload["phase"], payload["strategy_profile"], payload["strategy_profile_version"],
             payload["query_family"], payload["query_kind"], payload["query_pass"], payload["initial_order"],
             *ordering_fields[task_key].values(), provider_name, acquisition_mode, provider_run_id, payload["task_key"]),
        )
    conn.commit()

    # Import lazily: RPC and offline providers intentionally converge on these
    # same shared functions without making the bridge an acquisition dependency.
    from ..bridge.rpc import refresh_result_reconciliation

    def event(connection: sqlite3.Connection, rid: int, tid: int, kind: str, message: str,
              event_payload: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)",
            (rid, tid or None, _now(), kind, message, json.dumps(event_payload, ensure_ascii=False, sort_keys=True)),
        )

    failures = 0
    for task, payload in zip(planned, payloads):
        task_row = conn.execute(
            "SELECT * FROM browser_search_tasks WHERE browser_run_id=? AND task_key=? AND phase=?",
            (run_id, payload["task_key"], payload["phase"]),
        ).fetchone()
        task_id = int(task_row["task_id"])
        started = _now()
        conn.execute(
            "UPDATE browser_search_tasks SET status='running',started_at=?,attempts=attempts+1,last_progress_at=? WHERE task_id=?",
            (started, started, task_id),
        )
        conn.execute("UPDATE browser_runs SET current_task_id=?,last_progress_at=? WHERE browser_run_id=?", (task_id, started, run_id))
        conn.commit()
        try:
            batch = provider.fetch(task)
        except ProviderFailure as exc:
            failures += 1
            _finish_task(conn, run_id, task_id, task.platform, ProviderCompletionState.RETRYABLE if exc.retryable else ProviderCompletionState.INCOMPLETE,
                         exc.classification, str(exc), {}, provider_task_id="",
                         provider_metadata=exc.provider_metadata, requests_submitted=exc.requests_submitted,
                         records_delivered=exc.records_delivered, reported_cost=exc.reported_cost)
            continue
        except Exception as exc:
            failures += 1
            _finish_task(conn, run_id, task_id, task.platform, ProviderCompletionState.RETRYABLE,
                         ProviderFailureClass.UNKNOWN, f"{type(exc).__name__}: {exc}", {}, provider_task_id="")
            continue
        if not isinstance(batch, ProviderBatch) or any(not isinstance(record, AcquisitionRecord) for record in batch.records):
            failures += 1
            _finish_task(conn, run_id, task_id, task.platform, ProviderCompletionState.INCOMPLETE,
                         ProviderFailureClass.INVALID_RESPONSE, "provider returned an invalid batch or record object", {},
                         provider_task_id="")
            continue

        cards: list[tuple[AcquisitionRecord, int]] = []
        failure: ProviderFailure | None = None
        for record in batch.records:
            if record.query_task_key and record.query_task_key != task.task_key:
                failure = ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, "provider record query_task_key does not match its task", retryable=False)
                break
            if record.source_surface != task.platform:
                failure = ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, "provider record source_surface does not match its CHG-170 task", retryable=False)
                break
            card_result = persist_card(
                conn, run_id=run_id, task=task_row, strategy=strategy, record=record,
                provider_name=provider_name, provider_run_id=provider_run_id,
                acquisition_mode=acquisition_mode, event=event,
                reconcile=refresh_result_reconciliation,
            )
            if not card_result.get("ok"):
                failure = ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, str(card_result.get("error") or "card persistence failed"), retryable=False)
                conn.commit()
                break
            result_id = int(card_result["result_id"])
            conn.commit()
            cards.append((record, result_id))

        # Identity receipts for the complete batch are durable before detail writes.
        if failure is None:
            for record, result_id in cards:
                if record.detail is None:
                    continue
                detail = dict(record.detail)
                urls = dict(record.source_urls)
                detail.setdefault("source_job_id", record.source_job_id)
                detail.setdefault("canonical_url", urls.get("board_detail_url") or urls.get("source_url") or record.discovery_url())
                detail.setdefault("apply_url", urls.get("observed_board_apply_url") or "")
                detail.setdefault("employer_job_url", urls.get("employer_job_url") or "")
                detail.setdefault("ats_requisition_url", urls.get("ats_requisition_url") or "")
                detail.setdefault("provider_observed_at", record.observed_at)
                detail.setdefault("search_card", record.card_fields())
                evidence = {
                    "detail_acquisition": {"mode": "managed_provider", "provider": provider_name},
                    "surface": detail.get("surface") or detail.get("page_type") or "",
                }
                persist_detail(
                    conn, store, run_id=run_id, task_id=task_id, result_id=result_id,
                    raw=detail, evidence=evidence, cfg=bundle.legacy_runtime(), strategy=strategy,
                    candidate=bundle.legacy_runtime().get("candidate", {}), provider_name=provider_name,
                    provider_run_id=provider_run_id, acquisition_mode=acquisition_mode,
                    provider_record_id=record.provider_record_id, provider_metadata=record.raw_metadata,
                    event=event, reconcile=refresh_result_reconciliation,
                )
                conn.commit()

        if failure is not None:
            failures += 1
            state = ProviderCompletionState.RETRYABLE if failure.retryable else ProviderCompletionState.INCOMPLETE
            _finish_task(conn, run_id, task_id, task.platform, state, failure.classification, str(failure), {},
                         provider_task_id=batch.provider_task_id, provider_metadata=batch.provider_metadata,
                         requests_submitted=batch.requests_submitted, records_delivered=batch.records_delivered,
                         reported_cost=batch.reported_cost)
        elif batch.proven_complete:
            _finish_task(conn, run_id, task_id, task.platform, ProviderCompletionState.COMPLETE, None, "",
                         dict(batch.completion_evidence), provider_task_id=batch.provider_task_id,
                         provider_metadata=batch.provider_metadata, requests_submitted=batch.requests_submitted,
                         records_delivered=batch.records_delivered, reported_cost=batch.reported_cost)
        else:
            failures += 1
            state = batch.completion_state
            if state == ProviderCompletionState.COMPLETE:
                state = ProviderCompletionState.INCOMPLETE
            _finish_task(conn, run_id, task_id, task.platform, state,
                         batch.failure_class or ProviderFailureClass.PARTIAL_BATCH,
                         "provider supplied no explicit completion evidence", {},
                         provider_task_id=batch.provider_task_id, provider_metadata=batch.provider_metadata,
                         requests_submitted=batch.requests_submitted, records_delivered=batch.records_delivered,
                         reported_cost=batch.reported_cost)

    now = _now()
    exhausted = int(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='exhausted'", (run_id,)).fetchone()[0])
    incomplete = int(conn.execute("SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='incomplete'", (run_id,)).fetchone()[0])
    run_status = "completed" if exhausted == len(planned) else "partial"
    conn.execute(
        """UPDATE browser_runs SET status=?,completed_at=?,current_task_id=NULL,tasks_completed=?,tasks_incomplete=?,
           last_progress_at=?,last_error=? WHERE browser_run_id=?""",
        (run_status, now, exhausted, incomplete, now, "" if not incomplete else "provider left one or more tasks incomplete/retryable", run_id),
    )
    for platform in chosen:
        counts = conn.execute(
            """SELECT COALESCE(SUM(status='exhausted'),0),COALESCE(SUM(status='incomplete'),0),
               COALESCE(SUM(status='failed'),0) FROM browser_search_tasks WHERE browser_run_id=? AND platform=?""",
            (run_id, platform),
        ).fetchone()
        conn.execute(
            "UPDATE browser_platform_runs SET tasks_completed=?,tasks_incomplete=?,tasks_failed=? WHERE browser_run_id=? AND platform=?",
            (int(counts[0]), int(counts[1]), int(counts[2]), run_id, platform),
        )
    conn.execute(
        "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
        (run_id, now, "acquisition_run_completed" if run_status == "completed" else "acquisition_run_incomplete",
         f"provider={provider_name} status={run_status}", json.dumps({"provider": provider_name, "provider_run_id": provider_run_id, "task_count": len(planned), "task_complete_count": exhausted, "failure_count": failures}, sort_keys=True)),
    )
    conn.commit()
    store.close()
    return {
        "browser_run_id": run_id, "provider": provider_name, "provider_run_id": provider_run_id,
        "acquisition_mode": acquisition_mode, "status": run_status, "task_count": len(planned),
        "task_complete_count": exhausted, "task_incomplete_count": incomplete, "failure_count": failures,
    }


def _finish_task(conn: sqlite3.Connection, run_id: int, task_id: int, platform: str,
                 state: ProviderCompletionState, failure_class: ProviderFailureClass | None,
                 message: str, evidence: dict[str, Any], *, provider_task_id: str,
                 provider_metadata: dict[str, Any] | None = None, requests_submitted: int = 0,
                 records_delivered: int = 0, reported_cost: dict[str, Any] | None = None) -> None:
    now = _now()
    complete = state == ProviderCompletionState.COMPLETE and bool(evidence)
    task_status = "exhausted" if complete else "incomplete"
    durable_state = "COMPLETE" if complete else state.value
    if state == ProviderCompletionState.COMPLETE and not evidence:
        durable_state = "INCOMPLETE"
    conn.execute(
        """UPDATE browser_search_tasks SET status=?,completed_at=?,exhausted=?,exhaustion_reason=?,
           safety_stop_reason=?,last_error=?,provider_completion_state=?,provider_failure_class=?,
           completion_evidence_json=?,provider_task_id=?,provider_metadata_json=?,provider_requests_submitted=?,
           provider_records_delivered=?,provider_reported_cost_json=?,lease_owner='',lease_until=?,last_progress_at=?
           WHERE task_id=? AND browser_run_id=?""",
        (task_status, now, int(complete), json.dumps(evidence, ensure_ascii=False, sort_keys=True) if complete else "{}",
         message if not complete else "", message if failure_class else "", durable_state,
         str(failure_class.value if failure_class else ""), json.dumps(evidence, ensure_ascii=False, sort_keys=True),
         provider_task_id, json.dumps(provider_metadata or {}, ensure_ascii=False, sort_keys=True),
         max(0, int(requests_submitted)), max(0, int(records_delivered)),
         json.dumps(reported_cost or {}, ensure_ascii=False, sort_keys=True), None, now, task_id, run_id),
    )
    conn.execute(
        "INSERT INTO browser_events(browser_run_id,task_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?,?)",
        (run_id, task_id, now, "provider_task_complete" if complete else "provider_task_incomplete",
         message or "provider supplied explicit completion evidence",
         json.dumps({"provider_failure_class": failure_class.value if failure_class else "",
                     "provider_completion_state": durable_state,
                     "completion_evidence": evidence}, ensure_ascii=False, sort_keys=True)),
    )
    if complete:
        conn.execute("UPDATE browser_platform_runs SET tasks_completed=tasks_completed+1 WHERE browser_run_id=? AND platform=?", (run_id, platform))
    else:
        conn.execute("UPDATE browser_platform_runs SET tasks_incomplete=tasks_incomplete+1 WHERE browser_run_id=? AND platform=?", (run_id, platform))
    conn.commit()
