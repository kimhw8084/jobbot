from __future__ import annotations

"""Build and verify deterministic releases from immutable Git objects."""

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", "cache", "out", "logs", "data", "resumes", "artifacts", "dist", ".browser-profile", "release"}
FORBIDDEN_SUFFIXES = {".sqlite3", ".pyc", ".zip"}
PRIVATE_PATHS = {"config/candidate.toml"}
RELEASE_ROOT = "jobbot-3.2.3"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()


def _object_bytes(commit: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT)


def _tree_entries(commit: str) -> list[tuple[str, str]]:
    raw = _git("ls-tree", "-r", commit)
    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        mode_type, path = line.split("\t", 1)
        entries.append((mode_type.split()[0], path))
    return entries


def _allowed(path: str) -> bool:
    value = Path(path)
    if path in PRIVATE_PATHS or any(part in FORBIDDEN_PARTS for part in value.parts):
        return False
    if value.suffix.lower() in FORBIDDEN_SUFFIXES or path.startswith("tests/"):
        return False
    if path.startswith(".github/"):
        return False
    return (
        path.startswith("src/") or path.startswith("extension/") or path.startswith("scripts/")
        or path.startswith("config/") or path.startswith("docs/")
        or path in {"README.md", "CHANGELOG.md", "pyproject.toml"} or value.suffix == ".command"
    )


def release_files(commit: str | None = None) -> list[str]:
    selected = commit or _git("rev-parse", "HEAD")
    return sorted(path for _mode, path in _tree_entries(selected) if _allowed(path))


def _extension_identity(commit: str) -> dict[str, Any]:
    files = [path for path in release_files(commit) if path.startswith("extension/") and Path(path).name != "build_meta.json"]
    digest = hashlib.sha256()
    for path in files:
        raw = _object_bytes(commit, path)
        relative = path.removeprefix("extension/")
        digest.update(relative.encode("utf-8")); digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big")); digest.update(raw)
    manifest = json.loads(_object_bytes(commit, "extension/manifest.json").decode("utf-8"))
    return {
        "extension_version": str(manifest.get("version") or ""),
        "extension_build": str(manifest.get("version_name") or ""),
        "extension_runtime_digest": digest.hexdigest(),
    }


def _product_version(commit: str) -> str:
    source = _object_bytes(commit, "src/jobbot/version.py").decode("utf-8")
    match = re.search(r"PRODUCT_VERSION\s*=\s*['\"]([^'\"]+)", source)
    if not match:
        raise RuntimeError("authoritative product version is missing from the selected commit")
    return match.group(1)


def _highest_migration(commit: str) -> int:
    values = [int(match.group(1)) for path in release_files(commit) if (match := re.match(r"src/jobbot/migrations/m(\d+)_", path))]
    return max(values, default=0)


def _file_record(commit: str, path: str) -> dict[str, Any]:
    raw = _object_bytes(commit, path)
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def _provenance(commit: str, files: list[str]) -> dict[str, Any]:
    source_tree = _git("rev-parse", f"{commit}^{{tree}}")
    return {
        "product_version": _product_version(commit),
        "source_commit": commit,
        "source_tree": source_tree,
        "highest_schema_migration": _highest_migration(commit),
        **_extension_identity(commit),
        "private_configuration": "config/candidate.toml is intentionally excluded; this is a private single-user snapshot release",
        "files": [_file_record(commit, path) for path in files],
    }


def build(*, commit: str | None = None, target: Path | None = None) -> tuple[Path, str, int]:
    selected = _git("rev-parse", commit or "HEAD")
    files = release_files(selected)
    provenance = _provenance(selected, files)
    destination = (target or DIST / f"jobbot-{provenance['product_version']}.zip").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(f"{RELEASE_ROOT}/{path}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _object_bytes(selected, path), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        manifest_info = zipfile.ZipInfo(f"{RELEASE_ROOT}/RELEASE_PROVENANCE.json")
        manifest_info.date_time = (1980, 1, 1, 0, 0, 0)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o100644 << 16
        archive.writestr(manifest_info, json.dumps(provenance, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    sidecar = destination.with_suffix(".provenance.json")
    sidecar.write_text(json.dumps({**provenance, "archive_sha256": digest, "archive": destination.name}, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination, digest, len(files)


def verify(archive_path: Path, provenance_path: Path | None = None) -> dict[str, Any]:
    archive_path = archive_path.resolve()
    sidecar = provenance_path or archive_path.with_suffix(".provenance.json")
    value = json.loads(sidecar.read_text(encoding="utf-8"))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if digest != value.get("archive_sha256"):
        raise RuntimeError("release archive SHA-256 does not match provenance")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        prefix = f"{RELEASE_ROOT}/"
        embedded = json.loads(archive.read(f"{prefix}RELEASE_PROVENANCE.json").decode("utf-8"))
        expected = {key: item for key, item in value.items() if key not in {"archive_sha256", "archive"}}
        if embedded != expected:
            raise RuntimeError("embedded release provenance differs from sidecar")
        for record in value["files"]:
            name = prefix + record["path"]
            if name not in names:
                raise RuntimeError(f"release file missing: {record['path']}")
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise RuntimeError(f"release file hash mismatch: {record['path']}")
    return {"ok": True, "archive_sha256": digest, "source_commit": value["source_commit"], "files": len(value["files"])}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify an immutable JobBot release")
    parser.add_argument("--commit", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--verify", type=Path, default=None)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.verify), sort_keys=True))
        return 0
    target, digest, count = build(commit=args.commit, target=args.output)
    print(target); print(f"files={count}"); print(f"sha256={digest}")
    print(json.dumps(verify(target), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
