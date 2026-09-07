from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .legacy_engine import (
    detect_required_credential,
    extract_preferred_block,
    extract_required_block,
    requirement_analysis,
    years_required,
)


@dataclass(frozen=True)
class RequirementSections:
    required: str
    preferred: str


def extract_sections(description: str, strategy: dict[str, Any]) -> RequirementSections:
    return RequirementSections(
        required=extract_required_block(description, strategy),
        preferred=extract_preferred_block(description, strategy),
    )


__all__ = [
    "RequirementSections", "detect_required_credential", "extract_sections",
    "requirement_analysis", "years_required",
]
