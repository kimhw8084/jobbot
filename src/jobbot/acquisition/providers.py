from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import (
    AcquisitionRecord, ProviderBatch, ProviderCompletionState, ProviderFailure,
    ProviderFailureClass,
)


class JSONLProvider:
    """Deterministic offline JSONL adapter with explicit per-task completion rows."""

    name = "jsonl-file"

    def __init__(self, path: Path, *, run_id: str = ""):
        self.path = Path(path)
        self.run_id = run_id or f"jsonl:{self.path.name}"
        self._records: dict[str, list[AcquisitionRecord]] = {}
        self._completions: dict[str, dict[str, Any]] = {}
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"invalid JSONL line {line_number}: {exc}", retryable=False) from exc
                    if not isinstance(value, dict):
                        raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"JSONL line {line_number} must be an object", retryable=False)
                    key = str(value.get("query_task_key") or value.get("task_key") or "").strip()
                    if value.get("type") == "completion":
                        evidence = value.get("completion_evidence")
                        if key and isinstance(evidence, dict) and evidence:
                            self._completions[key] = dict(evidence)
                        continue
                    if value.get("type", "record") != "record":
                        raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"unsupported JSONL row type on line {line_number}", retryable=False)
                    try:
                        record = AcquisitionRecord.from_mapping(value)
                    except ValueError as exc:
                        raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"invalid JSONL record on line {line_number}: {exc}", retryable=False) from exc
                    if not key:
                        key = record.query_task_key
                    if not key:
                        raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"JSONL line {line_number} is missing query_task_key", retryable=False)
                    self._records.setdefault(key, []).append(record)
        except OSError as exc:
            raise ProviderFailure(ProviderFailureClass.TRANSPORT, f"could not read provider file: {exc}") from exc

    def fetch(self, task) -> ProviderBatch:
        key = task.task_key
        evidence = self._completions.get(key, {})
        return ProviderBatch(
            records=tuple(self._records.get(key, ())),
            completion_state=ProviderCompletionState.COMPLETE if evidence else ProviderCompletionState.INCOMPLETE,
            completion_evidence=evidence,
            provider_task_id=key,
        )


class JSONFileProvider:
    """Deterministic JSON fixture/import adapter using the JSONL row contract."""

    name = "json-file"

    def __init__(self, path: Path, *, run_id: str = ""):
        self.path = Path(path)
        self.run_id = run_id or f"json:{self.path.name}"
        self._records: dict[str, list[AcquisitionRecord]] = {}
        self._completions: dict[str, dict[str, Any]] = {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderFailure(ProviderFailureClass.TRANSPORT, f"could not read provider file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"invalid JSON provider file: {exc}", retryable=False) from exc
        if isinstance(value, list):
            rows = value
        elif isinstance(value, Mapping):
            rows = list(value.get("records") or []) + list(value.get("completions") or [])
        else:
            raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, "JSON provider root must be an object or array", retryable=False)
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"JSON row {index} must be an object", retryable=False)
            key = str(row.get("query_task_key") or row.get("task_key") or "").strip()
            if row.get("type") == "completion" or "completion_evidence" in row:
                evidence = row.get("completion_evidence")
                if key and isinstance(evidence, Mapping) and evidence:
                    self._completions[key] = dict(evidence)
                continue
            try:
                record = AcquisitionRecord.from_mapping(row)
            except ValueError as exc:
                raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"invalid JSON record {index}: {exc}", retryable=False) from exc
            key = key or record.query_task_key
            if not key:
                raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, f"JSON record {index} is missing query_task_key", retryable=False)
            self._records.setdefault(key, []).append(record)

    def fetch(self, task) -> ProviderBatch:
        evidence = self._completions.get(task.task_key, {})
        return ProviderBatch(
            records=tuple(self._records.get(task.task_key, ())),
            completion_state=ProviderCompletionState.COMPLETE if evidence else ProviderCompletionState.INCOMPLETE,
            completion_evidence=evidence, provider_task_id=task.task_key,
        )


class ManagedHTTPProvider:
    """HTTP adapter shell; transport is injected so tests and callers control I/O."""

    name = "managed-http"

    def __init__(self, endpoint: str, transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]], *, run_id: str):
        self.endpoint = endpoint
        self.transport = transport
        self.run_id = run_id

    def fetch(self, task) -> ProviderBatch:
        try:
            response = self.transport(self.endpoint, task.__dict__)
        except TimeoutError as exc:
            raise ProviderFailure(ProviderFailureClass.TIMEOUT, str(exc)) from exc
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(ProviderFailureClass.TRANSPORT, f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(response, Mapping):
            raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, "managed HTTP response must be an object", retryable=False)
        raw_records = response.get("records", ())
        if not isinstance(raw_records, (list, tuple)):
            raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, "managed HTTP records must be a list", retryable=False)
        try:
            records = tuple(AcquisitionRecord.from_mapping(record) for record in raw_records)
        except (TypeError, ValueError) as exc:
            raise ProviderFailure(ProviderFailureClass.INVALID_RESPONSE, str(exc), retryable=False) from exc
        evidence = response.get("completion_evidence")
        complete = response.get("complete") is True and isinstance(evidence, Mapping) and bool(evidence)
        state = ProviderCompletionState.COMPLETE if complete else ProviderCompletionState.INCOMPLETE
        if not response.get("complete") and response.get("has_more"):
            failure = ProviderFailureClass.AMBIGUOUS_CURSOR if response.get("next_cursor") is None else ProviderFailureClass.PARTIAL_BATCH
        elif not complete:
            failure = ProviderFailureClass.PARTIAL_BATCH
        else:
            failure = None
        return ProviderBatch(
            records=records, completion_state=state,
            completion_evidence=dict(evidence) if isinstance(evidence, Mapping) else {},
            failure_class=failure, provider_task_id=str(response.get("provider_task_id") or ""),
        )
