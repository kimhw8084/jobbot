from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from jobbot.config import PROJECT_ROOT
from jobbot.provenance import identity_unchanged, release_identity


def release_module():
    path = PROJECT_ROOT / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("jobbot_build_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class ReleaseProvenanceTests(unittest.TestCase):
    def test_two_immutable_builds_are_identical_and_private_files_are_excluded(self) -> None:
        builder = release_module()
        with tempfile.TemporaryDirectory() as td:
            first, first_hash, _ = builder.build(commit="HEAD", target=Path(td) / "one.zip")
            second, second_hash, _ = builder.build(commit="HEAD", target=Path(td) / "two.zip")
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(builder.verify(first)["ok"], True)
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertTrue(any(name.endswith("RELEASE_PROVENANCE.json") for name in names))
                self.assertFalse(any("candidate.toml" in name for name in names))
                self.assertFalse(any("jobs.sqlite3" in name or "/data/" in name for name in names))
                provenance = json.loads(archive.read(f"jobbot-{release_identity(PROJECT_ROOT)['product_version']}/RELEASE_PROVENANCE.json"))
            self.assertRegex(provenance["source_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(provenance["source_tree"], r"^[0-9a-f]{40}$")
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), first_hash)

    def test_release_files_come_from_git_not_nonignored_worktree_files(self) -> None:
        builder = release_module()
        paths = builder.release_files("HEAD")
        self.assertNotIn("config/candidate.toml", paths)
        self.assertTrue(all(not path.startswith("tests/") for path in paths))
            self.assertNotIn("src/jobbot/orchestrator 2.py", paths)

    def test_validation_provenance_detects_worktree_status_change(self) -> None:
        snapshot = release_identity(PROJECT_ROOT)
        changed = dict(snapshot)
        changed["status_lines"] = ["?? unexpected-release-input.txt"]
        changed["clean_worktree"] = False
        with patch("jobbot.provenance.release_identity", return_value=changed):
            self.assertFalse(identity_unchanged(PROJECT_ROOT, snapshot))


if __name__ == "__main__":
    unittest.main()
