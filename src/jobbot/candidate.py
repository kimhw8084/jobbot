from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigBundle


@dataclass(frozen=True)
class Capability:
    name: str
    level: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    name: str
    country: str
    state: str
    metro: str
    professional_positioning: str
    capabilities: tuple[Capability, ...]
    resume_files: dict[str, Path]

    @classmethod
    def from_bundle(cls, bundle: ConfigBundle) -> "Candidate":
        raw = bundle.candidate["candidate"]
        caps = tuple(
            Capability(str(x["name"]), str(x["level"]), tuple(str(v) for v in x.get("aliases", [])))
            for x in bundle.candidate.get("capabilities", [])
        )
        files = {
            str(key): (bundle.root / str(value)).resolve()
            for key, value in bundle.candidate.get("resume_files", {}).items()
        }
        return cls(
            name=str(raw["name"]), country=str(raw["country"]), state=str(raw["state"]),
            metro=str(raw["metro"]), professional_positioning=str(raw["professional_positioning"]),
            capabilities=caps, resume_files=files,
        )

    def existing_resumes(self) -> dict[str, Path]:
        return {key: value for key, value in self.resume_files.items() if value.is_file()}


def readiness_warnings(bundle: ConfigBundle) -> list[str]:
    """Surface unresolved candidate constraints without inventing values."""
    candidate = bundle.candidate.get("candidate", {})
    warnings: list[str] = []
    if str(candidate.get("work_authorization", "unknown")).strip().lower() in {"", "unknown"}:
        warnings.append("work authorization is unresolved; employer-specific authorization gates may remain REVIEW")
    if float(candidate.get("max_travel_percent", -1) or -1) < 0:
        warnings.append("maximum travel tolerance is unresolved; required in-person training/onboarding remains logistics review")
    if float(candidate.get("minimum_salary_annual", 0) or 0) <= 0:
        warnings.append("minimum salary floor is unset; compensation filtering is not an application-readiness guarantee")
    missing = sorted(key for key, path in Candidate.from_bundle(bundle).resume_files.items() if not path.is_file())
    if missing:
        warnings.append("resume files unavailable: " + ", ".join(missing))
    return warnings
