from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ProviderCompletionState(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    RETRYABLE = "RETRYABLE"


class ProviderFailureClass(StrEnum):
    TRANSPORT = "TRANSPORT"
    TIMEOUT = "TIMEOUT"
    AMBIGUOUS_CURSOR = "AMBIGUOUS_CURSOR"
    PARTIAL_BATCH = "PARTIAL_BATCH"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    AUTHORIZATION = "AUTHORIZATION"
    CONFIGURATION = "CONFIGURATION"
    SCHEMA = "SCHEMA"
    UNKNOWN = "UNKNOWN"


class ProviderFailure(RuntimeError):
    def __init__(self, classification: ProviderFailureClass, message: str, *, retryable: bool = True,
                 provider_metadata: Mapping[str, Any] | None = None, requests_submitted: int = 0,
                 records_delivered: int = 0, reported_cost: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.classification = classification
        self.retryable = retryable
        self.provider_metadata = dict(provider_metadata or {})
        self.requests_submitted = max(0, int(requests_submitted))
        self.records_delivered = max(0, int(records_delivered))
        self.reported_cost = dict(reported_cost or {})


@dataclass(frozen=True)
class AcquisitionRecord:
    """One provider observation. Values are observations, never verification claims."""

    source_surface: str
    source_job_id: str = ""
    source_urls: Mapping[str, str] = field(default_factory=dict)
    query_task_key: str = ""
    provider_record_id: str = ""
    observed_at: str = ""
    card: Mapping[str, Any] = field(default_factory=dict)
    detail: Mapping[str, Any] | None = None
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AcquisitionRecord":
        urls = value.get("source_urls") or value.get("url_roles") or {}
        if not isinstance(urls, Mapping):
            raise ValueError("source_urls must be an object keyed by URL role")
        card = value.get("card") or {}
        detail = value.get("detail")
        metadata = value.get("raw_metadata") or value.get("provider_metadata") or {}
        if not isinstance(card, Mapping) or (detail is not None and not isinstance(detail, Mapping)):
            raise ValueError("card and detail must be objects")
        if not isinstance(metadata, Mapping):
            raise ValueError("raw_metadata must be an object")
        source_surface = str(value.get("source_surface") or value.get("platform") or "").strip()
        if not source_surface:
            raise ValueError("source_surface is required")
        return cls(
            source_surface=source_surface,
            source_job_id=str(value.get("source_job_id") or "").strip(),
            source_urls={str(key): str(url).strip() for key, url in urls.items() if str(url or "").strip()},
            query_task_key=str(value.get("query_task_key") or value.get("task_key") or "").strip(),
            provider_record_id=str(value.get("provider_record_id") or "").strip(),
            observed_at=str(value.get("observed_at") or "").strip(),
            card=dict(card), detail=dict(detail) if detail is not None else None,
            raw_metadata=dict(metadata),
        )

    def card_fields(self) -> dict[str, Any]:
        return {
            "title": self.card.get("title") or (self.detail or {}).get("title") or "",
            "company": self.card.get("company") or (self.detail or {}).get("company") or "",
            "location": self.card.get("location") or (self.detail or {}).get("location") or "",
            **dict(self.card),
        }

    def discovery_url(self) -> str:
        return str(self.source_urls.get("discovery_url") or self.source_urls.get("source_url") or self.source_urls.get("board_detail_url") or "")


@dataclass(frozen=True)
class ProviderBatch:
    records: tuple[AcquisitionRecord, ...] = ()
    completion_state: ProviderCompletionState = ProviderCompletionState.INCOMPLETE
    completion_evidence: Mapping[str, Any] = field(default_factory=dict)
    failure_class: ProviderFailureClass | None = None
    provider_task_id: str = ""
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    requests_submitted: int = 0
    records_delivered: int = 0
    reported_cost: Mapping[str, Any] = field(default_factory=dict)

    @property
    def proven_complete(self) -> bool:
        return self.completion_state == ProviderCompletionState.COMPLETE and bool(self.completion_evidence)
