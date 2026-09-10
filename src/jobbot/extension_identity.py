"""Cryptographic identity for the unpacked runtime extension bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


METADATA_NAME = "build_meta.json"


def runtime_paths(root: Path) -> tuple[str, ...]:
    extension = root / "extension"
    return tuple(sorted(
        path.relative_to(extension).as_posix()
        for path in extension.rglob("*")
        if path.is_file() and path.name != METADATA_NAME
        and path.suffix.lower() in {".js", ".html", ".json", ".css"}
    ))


def runtime_digest(root: Path) -> str:
    extension = root / "extension"
    digest = hashlib.sha256()
    for relative in runtime_paths(root):
        raw = (extension / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))


def expected_identity(root: Path) -> dict[str, Any]:
    value = manifest(root)
    return {
        "extension_version": str(value.get("version") or ""),
        "extension_build": str(value.get("version_name") or ""),
        "runtime_digest": runtime_digest(root),
        "runtime_paths": list(runtime_paths(root)),
    }


def metadata(root: Path) -> dict[str, Any]:
    return json.loads((root / "extension" / METADATA_NAME).read_text(encoding="utf-8"))

