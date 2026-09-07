from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.2.0"
DIST = ROOT / "dist"
TARGET = DIST / f"jobbot-{VERSION}.zip"
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", "cache", "out", "logs", "data", "resumes", "artifacts", "dist", ".browser-profile", "release"}
FORBIDDEN_SUFFIXES = {".sqlite3", ".pyc", ".zip"}


def release_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    )
    files: list[Path] = []
    for raw in result.stdout.splitlines():
        path = Path(raw)
        if not (ROOT / path).is_file():
            continue
        if any(part in FORBIDDEN_PARTS for part in path.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            continue
        files.append(path)
    return sorted(set(files))


def build() -> tuple[Path, str, int]:
    DIST.mkdir(parents=True, exist_ok=True)
    files = release_files()
    with zipfile.ZipFile(TARGET, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            source = ROOT / relative
            info = zipfile.ZipInfo(f"jobbot-{VERSION}/{relative.as_posix()}")
            info.date_time = (2026, 9, 7, 0, 0, 0)
            mode = source.stat().st_mode
            info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(TARGET.read_bytes()).hexdigest()
    return TARGET, digest, len(files)


if __name__ == "__main__":
    target, digest, count = build()
    print(target)
    print(f"files={count}")
    print(f"sha256={digest}")
