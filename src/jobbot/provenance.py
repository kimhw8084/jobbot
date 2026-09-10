"""Immutable source and runtime provenance used by validation and launch guards."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .extension_identity import expected_identity
from .version import PRODUCT_VERSION


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT).strip()


def source_identity(root: Path) -> dict[str, Any]:
    try:
        upstream_ref = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        upstream_sha = _git(root, "rev-parse", "@{upstream}")
    except (subprocess.CalledProcessError, OSError):
        upstream_ref = ""
        upstream_sha = ""
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root, text=True, capture_output=True, check=False,
    ).stdout.splitlines()
    return {
        "branch": _git(root, "branch", "--show-current"),
        "head": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "upstream_ref": upstream_ref,
        "upstream_sha": upstream_sha,
        "head_equals_upstream": bool(upstream_sha) and upstream_sha == _git(root, "rev-parse", "HEAD"),
        "status_lines": status,
        "clean_worktree": not status,
        "nonignored_untracked": [line for line in status if line.startswith("?? ")],
        "product_version": PRODUCT_VERSION,
    }


def release_identity(root: Path) -> dict[str, Any]:
    value = source_identity(root)
    extension = expected_identity(root)
    value.update({
        "extension_version": extension["extension_version"],
        "extension_build": extension["extension_build"],
        "extension_runtime_digest": extension["runtime_digest"],
    })
    return value


def identity_unchanged(root: Path, snapshot: dict[str, Any]) -> bool:
    current = release_identity(root)
    for key in ("head", "tree", "extension_build", "extension_runtime_digest"):
        if current.get(key) != snapshot.get(key):
            return False
    return True

