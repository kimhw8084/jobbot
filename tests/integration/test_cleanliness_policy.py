from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobbot import validator
from jobbot.provenance import CLEANLINESS_POLICY_ID, ProvenanceError, source_identity
from tests.helpers import bundle_with_database


class CleanlinessPolicyTests(unittest.TestCase):
    """Exercise the production source-identity policy in isolated Git repos."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src" / "jobbot").mkdir(parents=True)
        (self.root / "extension").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        (self.root / "out").mkdir()
        (self.root / "logs").mkdir()
        (self.root / ".browser-profile").mkdir()
        (self.root / ".gitignore").write_text(
            "data/*.sqlite3\nout/*\nlogs/*\n.browser-profile/\ncache/\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text("tracked\n", encoding="utf-8")
        for relative in (
            "src/jobbot/module.py",
            "extension/service_worker.js",
            "scripts/tool.sh",
            "config/runtime.toml",
        ):
            self._write(relative, f"tracked {relative}\n")
        self._git("init", "-q")
        self._git("config", "user.name", "JobBot test")
        self._git("config", "user.email", "jobbot-test@example.invalid")
        self._git("add", ".")
        self._git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def _write(self, relative: str, content: str = "local\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_ignored_runtime_state_is_allowed(self) -> None:
        self._write("data/jobs.sqlite3", "sqlite bytes\n")
        self._write("out/dashboard.json")
        self._write("logs/run.log")
        self._write(".browser-profile/Preferences", "browser state\n")
        self._write("cache/search.json")

        state = source_identity(self.root)

        self.assertTrue(state["clean_worktree"])
        self.assertEqual(state["nonignored_untracked"], [])
        self.assertEqual(state["cleanliness_policy"]["id"], CLEANLINESS_POLICY_ID)

    def test_unexpected_source_like_paths_block(self) -> None:
        paths = (
            "unexpected.txt",
            "src/jobbot/new_module.py",
            "extension/new_worker.js",
            "scripts/NEW.command",
            "config/local.toml",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                self._write(relative)
                state = source_identity(self.root)
                self.assertFalse(state["clean_worktree"])
                self.assertTrue(any(relative in line for line in state["nonignored_untracked"]))
                (self.root / relative).unlink()

    def test_exact_duplicates_of_all_source_classes_block(self) -> None:
        pairs = (
            ("src/jobbot/module.py", "src/jobbot/module 2.py"),
            ("extension/service_worker.js", "extension/service_worker 2.js"),
            ("scripts/tool.sh", "scripts/tool 2.sh"),
            ("config/runtime.toml", "config/runtime 2.toml"),
            ("README.md", "README 2.md"),
        )
        for original, duplicate in pairs:
            with self.subTest(duplicate=duplicate):
                (self.root / duplicate).write_bytes((self.root / original).read_bytes())
                state = source_identity(self.root)
                self.assertFalse(state["clean_worktree"])
                self.assertIn(f"?? {duplicate}", state["nonignored_untracked"])
                (self.root / duplicate).unlink()

    def test_divergent_duplicate_remains_blocking(self) -> None:
        self._write("README 2.md", "different local work\n")

        state = source_identity(self.root)

        self.assertFalse(state["clean_worktree"])
        self.assertEqual(state["nonignored_untracked"], ["?? README 2.md"])

    def test_tracked_dirty_and_staged_changes_block(self) -> None:
        readme = self.root / "README.md"
        readme.write_text("modified\n", encoding="utf-8")
        self.assertFalse(source_identity(self.root)["clean_worktree"])

        self._git("restore", "README.md")
        readme.write_text("staged\n", encoding="utf-8")
        self._git("add", "README.md")
        state = source_identity(self.root)
        self.assertFalse(state["clean_worktree"])
        self.assertTrue(state["tracked_changes"])

    def test_policy_is_read_only_and_does_not_delete_local_files(self) -> None:
        marker = self.root / "src" / "jobbot" / "user-local.py"
        marker.write_bytes(b"user marker\n")

        source_identity(self.root)

        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_bytes(), b"user marker\n")

    def test_git_status_failure_is_not_clean(self) -> None:
        failure = subprocess.CalledProcessError(128, ["git", "status"])
        with patch("jobbot.provenance._git", return_value="value"), \
             patch("jobbot.provenance.subprocess.run", side_effect=failure):
            with self.assertRaises(ProvenanceError):
                source_identity(self.root)

    def test_untracked_query_failure_is_not_clean(self) -> None:
        status = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        failure = subprocess.CalledProcessError(128, ["git", "ls-files"])
        with patch("jobbot.provenance._git", return_value="value"), \
             patch("jobbot.provenance.subprocess.run", side_effect=[status, failure]):
            with self.assertRaises(ProvenanceError):
                source_identity(self.root)

    def test_required_upstream_failure_is_not_suppressed(self) -> None:
        with patch("jobbot.provenance._git", side_effect=ProvenanceError("upstream unavailable")):
            with self.assertRaises(ProvenanceError):
                source_identity(self.root, require_upstream=True)

    def test_validator_preflight_fails_closed_on_git_provenance_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = bundle_with_database(Path(td) / "acceptance.sqlite3", Path(td) / "out")
            with patch("jobbot.validator.source_identity", side_effect=ProvenanceError("status unavailable")):
                with self.assertRaises(ProvenanceError):
                    validator._preflight(bundle)


if __name__ == "__main__":
    unittest.main()
