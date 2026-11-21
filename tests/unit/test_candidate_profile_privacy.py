from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobbot.config import ConfigError, load_bundle


ROOT = Path(__file__).resolve().parents[2]


class CandidateProfilePrivacyTests(unittest.TestCase):
    def test_missing_local_profile_has_actionable_setup_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            for filename in ("strategy.toml", "live_search.toml", "runtime.toml"):
                shutil.copy2(ROOT / "config" / filename, config_dir / filename)
            missing_profile = config_dir / "candidate.toml"
            with patch.dict(os.environ, {"JOBBOT_CANDIDATE_CONFIG": str(missing_profile)}):
                with self.assertRaisesRegex(ConfigError, "candidate.example.toml"):
                    load_bundle(root)

    def test_test_profile_is_loaded_from_private_path_override(self) -> None:
        bundle = load_bundle(ROOT)
        self.assertEqual(bundle.candidate["candidate"]["name"], "Synthetic test candidate")
        self.assertEqual(bundle.candidate["candidate"]["country"], "US")
