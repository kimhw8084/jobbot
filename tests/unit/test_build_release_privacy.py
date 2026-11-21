from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import build_release


class ReleaseCandidatePrivacyTests(unittest.TestCase):
    def test_archive_omits_local_profiles_and_keeps_example_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            (config / "candidate.toml").write_text("PRIVATE_PROFILE_SENTINEL", encoding="utf-8")
            (config / "candidate.local.toml").write_text("LOCAL_OVERRIDE_SENTINEL", encoding="utf-8")
            (config / "candidate.example.toml").write_text("[candidate]\n", encoding="utf-8")
            (root / "README.md").write_text("safe", encoding="utf-8")
            paths = "\n".join((
                "config/candidate.toml",
                "config/candidate.local.toml",
                "config/candidate.example.toml",
                "README.md",
            ))
            with (
                patch.object(build_release, "ROOT", root),
                patch.object(build_release, "DIST", root / "dist"),
                patch.object(build_release, "TARGET", root / "dist" / "jobbot-test.zip"),
                patch.object(build_release.subprocess, "run", return_value=SimpleNamespace(stdout=paths)),
            ):
                target, _, _ = build_release.build()

            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                payload = b"\n".join(archive.read(name) for name in archive.namelist())
            self.assertIn("jobbot-3.2.0/config/candidate.example.toml", names)
            self.assertIn("jobbot-3.2.0/README.md", names)
            self.assertFalse(any(name.endswith("candidate.toml") for name in names))
            self.assertNotIn(b"PRIVATE_PROFILE_SENTINEL", payload)
            self.assertNotIn(b"LOCAL_OVERRIDE_SENTINEL", payload)


if __name__ == "__main__":
    unittest.main()
