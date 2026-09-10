"""Immutable source and runtime provenance used by validation and launch guards."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .extension_identity import expected_identity
from .version import PRODUCT_VERSION


CLEANLINESS_POLICY_ID = "git-v1"
class ProvenanceError(RuntimeError):
    """Raised when Git source identity cannot be established safely."""


CLEANLINESS_POLICY = {
    "id": CLEANLINESS_POLICY_ID,
    "status_command": "git status --porcelain=v1 --untracked-files=all",
    "tracked_changes_block": True,
    "nonignored_untracked_block": True,
    "ignored_local_state_allowed": True,
    "source_like_untracked_block": True,
}


def _git(root: Path, *args: str) -> str:
    operation = "git " + " ".join(args)
    try:
        value = subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"required Git operation failed: {operation}") from exc
    if not isinstance(value, str):
        raise ProvenanceError(f"required Git operation returned invalid text: {operation}")
    return value.strip()


def _nonignored_untracked_paths(root: Path) -> list[str]:
    operation = "git ls-files --others --exclude-standard -z"
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root, text=False, capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"required Git operation failed: {operation}") from exc
    raw = result.stdout
    if not isinstance(raw, bytes):
        raise ProvenanceError(f"required Git operation returned invalid bytes: {operation}")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError(f"required Git operation returned undecodable paths: {operation}") from exc
    return [item for item in decoded.split("\0") if item]


def _status_lines(root: Path) -> list[str]:
    operation = "git status --porcelain=v1 --untracked-files=all"
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root, text=True, capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ProvenanceError(f"required Git operation failed: {operation}") from exc
    if not isinstance(result.stdout, str):
        raise ProvenanceError(f"required Git operation returned invalid text: {operation}")
    return result.stdout.splitlines()


def _worktree_status(root: Path) -> dict[str, Any]:
    """Return the one source-of-truth cleanliness decision used by all guards.

    Git's porcelain status already excludes entries matched by the repository's
    narrowly scoped ignore rules.  Everything that remains is source identity:
    tracked modifications and *all* nonignored untracked paths are blocking.
    In particular, filename suffixes, directory names, and historical local
    overlays are never treated as implicit exemptions.
    """
    raw_status = _status_lines(root)
    untracked_paths = _nonignored_untracked_paths(root)
    tracked_changes = [line for line in raw_status if not line.startswith("?? ")]
    status = tracked_changes + [f"?? {path}" for path in untracked_paths]
    return {
        "raw_status_lines": raw_status,
        "status_lines": status,
        "tracked_changes": tracked_changes,
        "nonignored_untracked": [f"?? {path}" for path in untracked_paths],
        "clean_worktree": not status,
        "cleanliness_policy": dict(CLEANLINESS_POLICY),
    }


def source_identity(root: Path, *, require_upstream: bool = False) -> dict[str, Any]:
    try:
        upstream_ref = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        upstream_sha = _git(root, "rev-parse", "@{upstream}")
    except ProvenanceError as exc:
        if require_upstream:
            raise ProvenanceError(f"required Git upstream ref/SHA could not be established: {exc}") from exc
        upstream_ref = ""
        upstream_sha = ""
    worktree = _worktree_status(root)
    return {
        "branch": _git(root, "branch", "--show-current"),
        "head": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "upstream_ref": upstream_ref,
        "upstream_sha": upstream_sha,
        "head_equals_upstream": bool(upstream_sha) and upstream_sha == _git(root, "rev-parse", "HEAD"),
        **worktree,
        "product_version": PRODUCT_VERSION,
    }


def release_identity(root: Path, *, require_upstream: bool = False) -> dict[str, Any]:
    value = source_identity(root, require_upstream=require_upstream)
    extension = expected_identity(root)
    value.update({
        "extension_version": extension["extension_version"],
        "extension_build": extension["extension_build"],
        "extension_runtime_digest": extension["runtime_digest"],
    })
    return value


def identity_unchanged(root: Path, snapshot: dict[str, Any]) -> bool:
    current = release_identity(root, require_upstream=True)
    for key in ("head", "tree", "extension_build", "extension_runtime_digest", "status_lines"):
        if current.get(key) != snapshot.get(key):
            return False
    return True
