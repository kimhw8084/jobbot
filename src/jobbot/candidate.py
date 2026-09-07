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
