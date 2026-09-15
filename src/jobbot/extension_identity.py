"""Read the unpacked extension identity from its repository-owned manifest."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def manifest(root: Path) -> dict[str, Any]:
    value = json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("extension manifest must contain a JSON object")
    return value


def extension_build(root: Path) -> str:
    value = str(manifest(root).get("version_name") or "").strip()
    if not value:
        raise RuntimeError("extension manifest has no version_name build identity")
    return value
