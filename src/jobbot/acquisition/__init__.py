"""Provider-neutral acquisition contracts and ingestion coordinator."""

from .models import AcquisitionRecord, ProviderBatch, ProviderCompletionState, ProviderFailure, ProviderFailureClass
from .protocol import ProviderAdapter

__all__ = [
    "AcquisitionRecord", "ProviderAdapter", "ProviderBatch", "ProviderCompletionState",
    "ProviderFailure", "ProviderFailureClass",
]
