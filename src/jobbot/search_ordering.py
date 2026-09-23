from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .qualified_yield import trusted_qualified_yield

ALGORITHM_VERSION = "chg114-yield-v2"


@dataclass(frozen=True)
class OrderingConfig:
    min_evaluated_jobs: int = 8
    prior_strength: float = 8.0
    confidence_z: float = 1.96
    minimum_effect_per_cost: float = 0.02
    max_rank_displacement: int = 20

    @classmethod
    def from_mapping(cls, value: Any = None) -> "OrderingConfig":
        if not isinstance(value, dict):
            return cls()
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown search_quality_ordering parameter(s): {', '.join(sorted(unknown))}")
        config = cls(**value)
        if config.min_evaluated_jobs < 1 or config.prior_strength < 0 or config.confidence_z <= 0 or config.minimum_effect_per_cost < 0 or config.max_rank_displacement < 0:
            raise ValueError("invalid search_quality_ordering guardrail values")
        return config

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def config_for_bundle(bundle: Any) -> OrderingConfig:
    value = bundle.runtime.get("search_quality_ordering")
    if value is None:
        value = bundle.runtime.get("runtime", {}).get("search_quality_ordering")
    return OrderingConfig.from_mapping(value)


def _history(conn: Any, task: dict[str, Any], config: OrderingConfig) -> dict[str, Any]:
    params = (
        str(task.get("strategy_profile") or ""), str(task.get("strategy_profile_version") or ""),
        str(task.get("platform") or ""), str(task.get("query_family") or ""),
        str(task.get("query_kind") or ""), str(task.get("query_pass") or ""),
        str(task.get("task_key") or ""), int(task.get("window_days") or 0), str(task.get("phase") or ""),
        str(task.get("query_text") or ""),
    )
    executions = conn.execute("""SELECT COUNT(*) n,COALESCE(SUM(pages_visited+detail_count_read),0) cost
        FROM browser_search_tasks
        WHERE strategy_profile=? AND strategy_profile_version=? AND platform=? AND query_family=?
          AND query_kind=? AND query_pass=? AND task_key=? AND window_days=? AND phase=? AND query_text=?
          AND status NOT IN ('queued','running')""", params).fetchone()
    rows = conn.execute("""SELECT r.canonical_job_id,r.detail_status,
          j.evidence_readiness_state,j.qualification_readiness_state,j.recommendation,j.application_status,
          j.identity_evidence_state,j.source_verification_state,j.source_verification,
          j.application_destination_verification_state,j.remote_gate,j.employment_class,j.posting_status,j.is_active,
          j.hard_reject_reasons_json,j.qualification_gates_json,j.evidence_readiness_json
        FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id
        JOIN jobs j ON j.job_id=r.canonical_job_id
        WHERE t.strategy_profile=? AND t.strategy_profile_version=? AND t.platform=? AND t.query_family=?
          AND t.query_kind=? AND t.query_pass=? AND t.task_key=? AND t.window_days=? AND t.phase=? AND t.query_text=?
          AND t.status NOT IN ('queued','running')""", params).fetchall()
    evaluated_jobs: set[str] = set()
    qualified_jobs: set[str] = set()
    for raw in rows:
        row = dict(raw)
        job_id = str(row.get("canonical_job_id") or "")
        if not job_id:
            continue
        if (
            str(row.get("detail_status") or "").upper() in {"COMPLETE", "PARTIAL"}
            and str(row.get("evidence_readiness_state") or "").upper() in {"READY", "REVIEW", "BLOCKED"}
            and str(row.get("qualification_readiness_state") or "").upper() in {"READY", "REVIEW", "BLOCKED"}
        ):
            evaluated_jobs.add(job_id)
        if trusted_qualified_yield(row):
            qualified_jobs.add(job_id)
    evaluated = len(evaluated_jobs)
    qualified = len(qualified_jobs)
    cost = max(1, int(executions["cost"] or 0))
    cost_per_observation = cost / max(1, evaluated)
    return {
        "evaluated_jobs": evaluated, "qualified_jobs": qualified,
        "trusted_qualified_yield_jobs": qualified,
        "execution_instances": int(executions["n"] or 0), "cost_units": cost,
        "cost_per_evaluated_job": cost_per_observation,
        "observed_yield_per_cost": qualified / cost,
    }


def _wilson(successes: int, total: int, z: float) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def order_tasks(conn: Any, tasks: Iterable[dict[str, Any]], config: OrderingConfig | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze a statistical order inside each phase/platform peer group."""
    active = config or OrderingConfig()
    rows = [dict(task) for task in tasks]
    peer_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for task in rows:
        peer_groups.setdefault((str(task.get("phase") or ""), str(task.get("platform") or "")), []).append(task)
    configurations: dict[str, Any] = {}
    for (phase, platform), peers in sorted(peer_groups.items()):
        baseline = sorted(peers, key=lambda task: (int(task.get("execution_rank") or 0), int(task.get("priority") or 0), str(task.get("task_key") or task.get("query_text") or "")))
        histories = [_history(conn, task, active) for task in baseline]
        sample_floor = min((item["evaluated_jobs"] for item in histories), default=0)
        reason = "baseline: one task in phase/platform peer group" if len(baseline) < 2 else ""
        learned = False
        ordered = baseline
        scores: list[dict[str, Any]] = []
        if len(baseline) >= 2 and any(item["evaluated_jobs"] < active.min_evaluated_jobs for item in histories):
            reason = f"baseline: peer group below sample floor {active.min_evaluated_jobs}; minimum observed={sample_floor}"
        elif len(baseline) >= 2:
            prior_rate = sum(item["qualified_jobs"] for item in histories) / max(1, sum(item["evaluated_jobs"] for item in histories))
            for task, history in zip(baseline, histories):
                n = history["evaluated_jobs"]
                positives = history["qualified_jobs"]
                shrunk = (positives + active.prior_strength * prior_rate) / (n + active.prior_strength)
                cost_per_job = history["cost_per_evaluated_job"]
                low, high = _wilson(positives, n, active.confidence_z)
                scores.append({
                    "task": task, "history": history,
                    "score": shrunk / max(cost_per_job, 1e-9),
                    "lower": low / max(cost_per_job, 1e-9),
                    "upper": high / max(cost_per_job, 1e-9),
                })
            candidate = sorted(scores, key=lambda item: (-item["score"], int(item["task"].get("execution_rank") or 0), int(item["task"].get("priority") or 0), str(item["task"].get("task_key") or item["task"].get("query_text") or "")))
            separated = all(
                candidate[index]["lower"] > candidate[index + 1]["upper"] + active.minimum_effect_per_cost
                for index in range(len(candidate) - 1)
            )
            if not separated:
                reason = "baseline: trusted outcome intervals overlap or contradict; learned separation guardrail held"
            else:
                max_shift = max(abs(baseline.index(item["task"]) - index) for index, item in enumerate(candidate))
                if max_shift > active.max_rank_displacement:
                    reason = f"baseline: learned displacement {max_shift} exceeds guardrail {active.max_rank_displacement}"
                else:
                    learned = candidate != scores
                    if learned:
                        ordered = [item["task"] for item in candidate]
                        reason = "learned: shrunk trusted qualified yield per search/detail cost; disjoint Wilson intervals"
                    else:
                        reason = "baseline: trusted history supports current execution order"
        rank_slots = sorted(int(task.get("execution_rank") or 0) for task in baseline)
        for position, task in enumerate(ordered):
            history = next((item for peer, item in zip(baseline, histories) if peer is task), {})
            task["baseline_execution_rank"] = int(task.get("execution_rank") or 0)
            task["effective_execution_rank"] = rank_slots[position] if learned else int(task.get("execution_rank") or 0)
            task["learned_order_reason"] = reason if not learned else (
                f"{reason}; yield/cost={next((item['score'] for item in scores if item['task'] is task), 0):.6f}; "
                f"sample={history.get('evaluated_jobs', 0)}, trusted_qualified={history.get('trusted_qualified_yield_jobs', 0)}"
            )
            task["learned_order_sample_size"] = int(history.get("evaluated_jobs", 0))
            task["ordering_algorithm_version"] = ALGORITHM_VERSION
        configurations[f"{phase}/{platform}"] = {
            "learned": learned,
            "reason": reason,
            "sample_floor": sample_floor,
            "task_samples": {str(task.get("task_key") or task.get("query_text") or ""): history["evaluated_jobs"] for task, history in zip(baseline, histories)},
        }
    return rows, {"algorithm_version": ALGORITHM_VERSION, "parameters": asdict(active), "peer_groups": configurations}
