from __future__ import annotations

from typing import Protocol

from ..search_plan import SearchTask
from .models import ProviderBatch


class ProviderAdapter(Protocol):
    """Named provider boundary; adapters receive a frozen CHG-170 task."""

    name: str
    run_id: str

    def fetch(self, task: SearchTask) -> ProviderBatch: ...
