from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from jobbot.provenance import CLEANLINESS_POLICY_ID, source_identity


class CleanlinessPolicyTests(unittest.TestCase):
    """Exercise the production source-identity policy in isolated Git repos."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src" / "jobbot").mkdir(parents=True)
        (self.root / "extension").mkdir()
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

    def test_historical_duplicate_suffix_is_not_an_exemption(self) -> None:
        self._write("src/jobbot/orchestrator 2.py")

        state = source_identity(self.root)

        self.assertFalse(state["clean_worktree"])
        self.assertEqual(len(state["nonignored_untracked"]), 1)

    def test_exact_cloud_sync_duplicate_overlay_is_permitted(self) -> None:
        tracked = self.root / "README.md"
        duplicate = self.root / "README 2.md"
        duplicate.write_bytes(tracked.read_bytes())

        state = source_identity(self.root)

        self.assertTrue(state["clean_worktree"])
        self.assertEqual(state["nonignored_untracked"], [])
        self.assertEqual(state["permitted_local_overlays"], ["README 2.md"])

    def test_divergent_cloud_sync_duplicate_remains_blocking(self) -> None:
        self._write("README 2.md", "different local work\n")

        state = source_identity(self.root)

        self.assertFalse(state["clean_worktree"])
        self.assertEqual(state["permitted_local_overlays"], [])
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


if __name__ == "__main__":
    unittest.main()
