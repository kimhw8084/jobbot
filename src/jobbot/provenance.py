"""Immutable source and runtime provenance used by validation and launch guards."""
from __future__ import annotations

import subprocess
import re
from pathlib import Path
from typing import Any

from .extension_identity import expected_identity
from .version import PRODUCT_VERSION


CLEANLINESS_POLICY_ID = "git-v1"
CLEANLINESS_POLICY = {
    "id": CLEANLINESS_POLICY_ID,
    "status_command": "git status --porcelain=v1 --untracked-files=all",
    "tracked_changes_block": True,
    "nonignored_untracked_block": True,
    "ignored_local_state_allowed": True,
    "source_like_untracked_block": True,
    "exact_cloud_sync_duplicate_overlay_allowed": True,
}


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT).strip()


def _nonignored_untracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root, text=False, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return []
    raw = result.stdout
    if isinstance(raw, str):
        return [item for item in raw.split("\0") if item]
    return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]


def _is_exact_cloud_sync_duplicate(root: Path, relative: str) -> bool:
    """Recognize only harmless byte-identical ``name 2.ext`` sync copies.

    Cloud sync tools can leave a second copy beside a tracked file.  A copy is
    tolerated only when its normalized path is already tracked, it is a regular
    file (not a link), and its Git blob is byte-identical to that tracked file.
    Divergent copies remain blocking source-like untracked state.
    """
    path = Path(relative)
    match = re.match(r"^(.*) 2(\.[^./]+)$", path.name)
    if not match:
        return False
    normalized = path.with_name(f"{match.group(1)}{match.group(2)}")
    local = root / path
    if not local.is_file() or local.is_symlink():
        return False
    try:
        tracked_blob = _git(root, "rev-parse", f"HEAD:{normalized.as_posix()}")
        local_blob = _git(root, "hash-object", "--", relative)
    except (subprocess.CalledProcessError, OSError):
        return False
    return bool(tracked_blob) and tracked_blob == local_blob


def _worktree_status(root: Path) -> dict[str, Any]:
    """Return the one source-of-truth cleanliness decision used by all guards.

    Git's porcelain status already excludes entries matched by the repository's
    narrowly scoped ignore rules.  Everything that remains is source identity:
    tracked modifications and *all* nonignored untracked paths are blocking.
    In particular, filename suffixes, directory names, and historical local
    overlays are never treated as implicit exemptions.
    """
    raw_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root, text=True, capture_output=True, check=False,
    ).stdout.splitlines()
    untracked_paths = _nonignored_untracked_paths(root)
    permitted_overlays = [path for path in untracked_paths if _is_exact_cloud_sync_duplicate(root, path)]
    permitted_set = set(permitted_overlays)
    blocking_untracked = [path for path in untracked_paths if path not in permitted_set]
    tracked_changes = [line for line in raw_status if not line.startswith("?? ")]
    status = tracked_changes + [f"?? {path}" for path in blocking_untracked]
    return {
        "raw_status_lines": raw_status,
        "status_lines": status,
        "tracked_changes": tracked_changes,
        "nonignored_untracked": [f"?? {path}" for path in blocking_untracked],
        "permitted_local_overlays": permitted_overlays,
        "clean_worktree": not status,
        "cleanliness_policy": dict(CLEANLINESS_POLICY),
    }


def source_identity(root: Path) -> dict[str, Any]:
    try:
        upstream_ref = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        upstream_sha = _git(root, "rev-parse", "@{upstream}")
    except (subprocess.CalledProcessError, OSError):
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
    for key in ("head", "tree", "extension_build", "extension_runtime_digest", "status_lines"):
        if current.get(key) != snapshot.get(key):
            return False
    return True
