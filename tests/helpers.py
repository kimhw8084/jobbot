from __future__ import annotations

import copy
from pathlib import Path

from jobbot.config import ConfigBundle, PROJECT_ROOT, load_bundle
from jobbot.scoring import Job, score_job


def bundle_with_database(database: Path, output: Path | None = None) -> ConfigBundle:
    original = load_bundle(PROJECT_ROOT)
    runtime = copy.deepcopy(original.runtime)
    runtime["runtime"]["database_path"] = str(database)
    runtime["runtime"]["output_dir"] = str(output or database.parent / "out")
    runtime["ledger"]["backup_dir"] = str(database.parent / "backups")
    return ConfigBundle(original.root, copy.deepcopy(original.strategy), copy.deepcopy(original.candidate), runtime)


def scored(title: str, description: str, *, location: str = "Remote — United States", employment: str = "Full-time permanent", source: str = "greenhouse", remote_status: str = "remote") -> Job:
    bundle = load_bundle(PROJECT_ROOT)
    job = Job(
        source_site=source, source_job_id=title.replace(" ", "-") + "-100",
        canonical_url="https://boards.greenhouse.io/example/jobs/100",
        apply_url="https://boards.greenhouse.io/example/jobs/100",
        title=title, company="Example Health", location_raw=location,
        remote_status=remote_status, employment_type=employment,
        description=description, posted_at="2026-09-07T12:00:00+00:00",
    )
    setattr(job, "_mode", "deep")
    return score_job(job, bundle.strategy, bundle.legacy_runtime()["candidate"])
