from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExhaustionTracker:
    max_identical_fingerprints: int = 3
    max_zero_new_batches: int = 3
    fingerprints: dict[str, int] = field(default_factory=dict)
    zero_new_batches: int = 0

    def observe(self, fingerprint: str, new_results: int, *, explicit_end: bool = False, age_boundary: bool = False) -> tuple[str, str]:
        if explicit_end:
            return "exhausted", "platform explicit end state"
        if age_boundary:
            return "exhausted", "newest-sorted age boundary"
        if new_results:
            self.zero_new_batches = 0
        else:
            self.zero_new_batches += 1
        if fingerprint:
            self.fingerprints[fingerprint] = self.fingerprints.get(fingerprint, 0) + 1
            if self.fingerprints[fingerprint] >= self.max_identical_fingerprints:
                return "incomplete", "SAFETY_STOP: repeated stable fingerprint without verified end"
        if self.zero_new_batches >= self.max_zero_new_batches:
            return "incomplete", "SAFETY_STOP: repeated batches with zero new results"
        return "running", ""
