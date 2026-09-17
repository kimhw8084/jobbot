"""Stable extension deployment and ordinary-Chrome profile targeting.

The unpacked extension is loaded once from a machine-local directory.  Git
worktrees are source inputs only; Chrome never receives a worktree path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extension_identity import extension_build, manifest


DEPLOYMENT_MARKER = "deployment_identity.json"
CHROME_PROFILE_BINDING = "chrome_profile_binding.json"
_PROFILE_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


@dataclass(frozen=True)
class DeploymentInfo:
    extension_root: Path
    marker_path: Path
    extension_id: str
    version_name: str
    source_identity: str
    source_head: str
    source_root: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "extension_root": str(self.extension_root),
            "marker_path": str(self.marker_path),
            "extension_id": self.extension_id,
            "version_name": self.version_name,
            "source_identity": self.source_identity,
            "source_head": self.source_head,
            "source_root": str(self.source_root),
        }


def machine_local_root() -> Path:
    override = os.environ.get("JOBBOT_EXTENSION_DEPLOY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "JobBot").resolve()
    if sys.platform == "win32":
        return (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "JobBot").resolve()
    return (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "jobbot").resolve()


def stable_extension_root() -> Path:
    return machine_local_root() / "extension"


def deployment_marker_path() -> Path:
    return stable_extension_root() / DEPLOYMENT_MARKER


def chrome_profile_binding_path() -> Path:
    return machine_local_root() / CHROME_PROFILE_BINDING


def _extension_tree_identity(extension_root: Path) -> str:
    if not extension_root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(path for path in extension_root.rglob("*") if path.is_file()):
        relative = path.relative_to(extension_root).as_posix()
        if relative == DEPLOYMENT_MARKER:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def extension_source_identity(root: Path) -> str:
    """Return a content identity for the repository-owned extension source."""
    return _extension_tree_identity((root / "extension").resolve())


def _source_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root.resolve()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _expected_deployment(root: Path) -> DeploymentInfo:
    root = root.resolve()
    source_extension = root / "extension"
    value = manifest(root)
    extension_id = (root / "config" / "EXTENSION_ID.txt").read_text(encoding="utf-8").strip()
    if not extension_id or len(extension_id) != 32:
        raise RuntimeError("config/EXTENSION_ID.txt must contain the stable JobBot extension ID")
    if value.get("manifest_version") != 3:
        raise RuntimeError("JobBot extension must remain Manifest V3")
    if not source_extension.is_dir():
        raise RuntimeError(f"extension source directory is missing: {source_extension}")
    return DeploymentInfo(
        extension_root=stable_extension_root(),
        marker_path=deployment_marker_path(),
        extension_id=extension_id,
        version_name=extension_build(root),
        source_identity=_extension_tree_identity(source_extension),
        source_head=_source_head(root),
        source_root=root,
    )


def _marker_payload(info: DeploymentInfo) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "extension_id": info.extension_id,
        "version_name": info.version_name,
        "source_identity": info.source_identity,
        "source_head": info.source_head,
        "source_root": str(info.source_root),
        "deployed_extension_root": str(info.extension_root),
    }


def _read_marker() -> dict[str, Any] | None:
    path = deployment_marker_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def deployment_diagnostics(root: Path) -> dict[str, Any]:
    expected = _expected_deployment(root)
    observed = _read_marker()
    diagnostics: dict[str, Any] = {
        "classification": "bootstrap_or_deployment_source_mismatch",
        "expected": expected.as_dict(),
        "observed": observed or {},
        "stable_extension_root": str(expected.extension_root),
    }
    if observed is None or not expected.extension_root.is_dir():
        diagnostics.update({"ok": False, "state": "not_deployed"})
        return diagnostics
    fields_match = all(
        str(observed.get(field, "")) == str(value)
        for field, value in {
            "extension_id": expected.extension_id,
            "version_name": expected.version_name,
            "source_identity": expected.source_identity,
            "deployed_extension_root": str(expected.extension_root),
        }.items()
    )
    deployed_identity = _extension_tree_identity(expected.extension_root)
    if not fields_match or deployed_identity != expected.source_identity:
        diagnostics.update({"ok": False, "state": "mismatch", "deployed_identity": deployed_identity})
        return diagnostics
    diagnostics.update({
        "ok": True,
        "state": "current",
        "classification": "deployed_source_current",
        "deployed_identity": deployed_identity,
    })
    return diagnostics


def sync_extension(root: Path) -> DeploymentInfo:
    """Copy exact integrated source to the stable machine-local extension path."""
    expected = _expected_deployment(root)
    current = deployment_diagnostics(root)
    if current.get("ok"):
        return expected

    destination = expected.extension_root
    if destination.is_symlink():
        raise RuntimeError(f"stable extension path must be a real directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".extension-sync-{secrets.token_hex(8)}"
    try:
        shutil.copytree(root.resolve() / "extension", staging)
        (staging / DEPLOYMENT_MARKER).write_text(
            json.dumps(_marker_payload(expected), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            if not destination.is_dir():
                raise RuntimeError(f"stable extension path is not a directory: {destination}")
            shutil.rmtree(destination)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return expected


def _valid_profile_directory(value: object) -> bool:
    return bool(isinstance(value, str) and _PROFILE_DIRECTORY.fullmatch(value) and value not in {".", ".."})


def bind_chrome_profile(profile_directory: str) -> dict[str, Any]:
    if not _valid_profile_directory(profile_directory):
        raise ValueError("Chrome profile directory must be a simple directory name such as Default or Profile 1")
    path = chrome_profile_binding_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract_version": 1,
        "browser": "Google Chrome",
        "profile_directory": profile_directory,
        "extension_root": str(stable_extension_root()),
    }
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def chrome_target_diagnostics() -> dict[str, Any]:
    path = chrome_profile_binding_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "ok": False,
            "classification": "wrong_or_untargeted_chrome_profile_or_instance",
            "state": "unbound",
            "binding_path": str(path),
            "message": "Chrome profile is not bound; run the one-time bootstrap with --profile-directory",
        }
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "classification": "wrong_or_untargeted_chrome_profile_or_instance",
            "state": "invalid_binding",
            "binding_path": str(path),
            "message": "Chrome profile binding is unreadable or invalid; rerun the one-time bootstrap",
        }
    valid = (
        isinstance(value, dict)
        and value.get("contract_version") == 1
        and value.get("browser") == "Google Chrome"
        and _valid_profile_directory(value.get("profile_directory"))
        and str(value.get("extension_root")) == str(stable_extension_root())
    )
    if not valid:
        return {
            "ok": False,
            "classification": "wrong_or_untargeted_chrome_profile_or_instance",
            "state": "invalid_binding",
            "binding_path": str(path),
            "observed": value if isinstance(value, dict) else {},
            "message": "Chrome profile binding does not target the stable JobBot extension contract",
        }
    return {
        "ok": True,
        "classification": "chrome_profile_targeted",
        "state": "bound",
        "binding_path": str(path),
        "browser": "Google Chrome",
        "profile_directory": value["profile_directory"],
        "extension_root": str(stable_extension_root()),
    }


def chrome_open_command(url: str, executable: str | None = None) -> list[str]:
    target = chrome_target_diagnostics()
    if not target.get("ok"):
        raise RuntimeError(json.dumps(target, ensure_ascii=False, sort_keys=True))
    profile_arg = f"--profile-directory={target['profile_directory']}"
    if sys.platform == "darwin":
        # `open --args` is only delivered to a newly launched application.
        # Force that normal-Chrome instance so an already-running Chrome cannot
        # silently consume the URL while dropping the profile arguments.
        return ["open", "-n", "-g", "-a", "Google Chrome", "--args", profile_arg, url]
    if not executable:
        raise RuntimeError("normal installed Google Chrome was not found")
    return [executable, profile_arg, url]


def runtime_binding_diagnostics(root: Path) -> dict[str, Any]:
    deployment = deployment_diagnostics(root)
    target = chrome_target_diagnostics()
    if not deployment.get("ok"):
        classification = "bootstrap_or_deployment_source_mismatch"
    elif not target.get("ok"):
        classification = "wrong_or_untargeted_chrome_profile_or_instance"
    else:
        classification = "runtime_binding_ready"
    return {
        "ok": bool(deployment.get("ok") and target.get("ok")),
        "classification": classification,
        "deployment": deployment,
        "chrome_target": target,
    }
