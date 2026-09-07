from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .bridge.server import self_test as bridge_self_test
from .candidate import Candidate
from .config import ConfigBundle
from .db import Database, all_migrations
from .exports import export_all
from .ledger import open_ledger
from .orchestrator import chrome_path
from .scoring import Job, detect_required_credential, phrase_present, score_job, years_required
from .search_plan import compile_plan


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _score(bundle: ConfigBundle, title: str, description: str, *, location: str = "Remote — United States") -> Job:
    job = Job(
        source_site="greenhouse", source_job_id=title.replace(" ", "-"),
        canonical_url="https://boards.greenhouse.io/example/jobs/12345",
        apply_url="https://boards.greenhouse.io/example/jobs/12345", title=title,
        company="Example Health", location_raw=location, remote_status="remote",
        employment_type="Full-time permanent", description=description,
    )
    setattr(job, "_mode", "deep")
    return score_job(job, bundle.strategy, bundle.legacy_runtime()["candidate"])


def run(bundle: ConfigBundle) -> tuple[bool, list[Check]]:
    checks: list[Check] = []
    checks.append(Check("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    candidate = Candidate.from_bundle(bundle)
    checks.append(Check("Configuration", True, f"strategy {bundle.strategy['strategy']['version']}; candidate {candidate.name}"))
    migration = Database(bundle).migrate()
    checks.append(Check("Database migration", migration.integrity_after == "ok", f"applied={migration.applied}; backup={migration.backup_path or 'not needed'}"))
    expected_versions = tuple(m.VERSION for m in all_migrations())
    conn = Database(bundle).connect()
    try:
        current = tuple(int(x[0]) for x in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))
    finally:
        conn.close()
    checks.append(Check("Migrations current", current == expected_versions, str(current)))
    manifest_path = bundle.root / "extension" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_ok = manifest.get("manifest_version") == 3 and "nativeMessaging" not in manifest.get("permissions", []) and "http://127.0.0.1/*" in manifest.get("host_permissions", [])
        checks.append(Check("Extension manifest", manifest_ok, str(manifest.get("version"))))
    except Exception as exc:
        checks.append(Check("Extension manifest", False, str(exc)))
    extension_id = (bundle.root / "config" / "EXTENSION_ID.txt").read_text(encoding="utf-8").strip()
    checks.append(Check("Extension ID", len(extension_id) == 32, extension_id))
    try:
        bridge_ok = bridge_self_test() == 0
        checks.append(Check("Loopback bridge RPC", bridge_ok, "token auth and unauthorized rejection"))
    except Exception as exc:
        checks.append(Check("Loopback bridge RPC", False, str(exc)))
    for name in ("data", "out", "logs"):
        path = bundle.root / name
        path.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=path, prefix=".doctor-", delete=True):
                pass
            checks.append(Check(f"Writable {name}", True, str(path)))
        except Exception as exc:
            checks.append(Check(f"Writable {name}", False, str(exc)))
    chrome = chrome_path()
    checks.append(Check("Normal Google Chrome", bool(chrome), chrome or "not found"))
    deep = compile_plan(bundle, "deep")
    fast = compile_plan(bundle, "fast")
    plan_ok = bool(deep and fast) and all(task.max_results is None and task.remote_required for task in deep + fast)
    checks.append(Check("Search plan", plan_ok, f"fast={len(fast)} deep={len(deep)}; production caps=none"))
    regression_ok = (
        not phrase_present("SIS", "analysis") and not phrase_present("Lean", "clean")
        and not detect_required_credential("Requirements: do the work well.", "DO", bundle.strategy)
        and years_required("8+ years of program management") == 8
        and _score(bundle, "Senior AI Engineer", "Remote healthcare quality workflow.").recommendation == "OUT_OF_SCOPE"
        and _score(bundle, "Patient Enrollment Specialist", "Fully remote healthcare enrollment. Required Qualifications: 2 years of relevant experience.").recommendation == "APPLY_NOW"
        and _score(bundle, "Patient Access Specialist", "#LI-Remote. Mandatory hybrid schedule with three office days.").recommendation == "SKIP_HARD_GATE"
        and _score(bundle, "Patient Enrollment Specialist — Offshore Philippines", "Remote role.", location="United States").recommendation == "SKIP_HARD_GATE"
    )
    checks.append(Check("Scoring regressions", regression_ok, "boundaries, role family, remote precedence"))
    try:
        with open_ledger(bundle) as store:
            paths = export_all(store.conn, bundle.output_dir, batch_size=3)
        checks.append(Check("Exports", all(path.is_file() for path in paths.values()), f"{len(paths)} artifacts"))
    except Exception as exc:
        checks.append(Check("Exports", False, str(exc)))
    resumes = candidate.existing_resumes()
    configured = len(candidate.resume_files)
    # Resume documents are intentionally private and excluded from release archives.
    # Routing configuration must exist, but a fresh installation remains healthy
    # until the user places their own files at those paths.
    resume_registry_ok = configured == 4 and all(path.is_relative_to(bundle.root) for path in candidate.resume_files.values())
    checks.append(Check(
        "Resume registry",
        resume_registry_ok,
        f"{len(resumes)}/{configured} private files present; missing files are optional and never packaged",
    ))
    return all(check.ok for check in checks), checks
