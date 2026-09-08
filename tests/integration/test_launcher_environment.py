from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from jobbot.config import PROJECT_ROOT
from jobbot.validator import _isolated_bundle


PRODUCTION_LAUNCHERS = (
    "RUN_NOW.command",
    "RUN_CONTINUOUS.command",
    "STOP_SEARCH.command",
    "RESUME_SEARCH.command",
    "OPEN_DASHBOARD.command",
    "AUDIT.command",
    "DOCTOR.command",
    "IMPORT_EXISTING_DB.command",
    "RUN_FULL_SEARCH.command",
    "RUN_FAST_SEARCH.command",
    "RUN_PLATFORM_LINKEDIN.command",
    "RUN_PLATFORM_INDEED.command",
    "RUN_PLATFORM_GLASSDOOR.command",
)


class LauncherEnvironmentTests(unittest.TestCase):
    def test_production_environment_helper_overrides_stale_parent_values(self) -> None:
        env = os.environ.copy()
        env.update({
            "JOBBOT_DATABASE_PATH": "/tmp/wrong.sqlite3",
            "JOBBOT_OUTPUT_DIR": "/tmp/wrong-output",
            "JOBBOT_DASHBOARD_PORT": "9999",
        })
        command = (
            "BASE=\"$PWD\"; export BASE; source scripts/production_env.sh; "
            "if command -v cygpath >/dev/null 2>&1; then "
            "cygpath -w \"$JOBBOT_DATABASE_PATH\"; cygpath -w \"$JOBBOT_OUTPUT_DIR\"; "
            "else printf '%s\\n' \"$JOBBOT_DATABASE_PATH\" \"$JOBBOT_OUTPUT_DIR\"; fi; "
            "printf '%s\\n' \"$JOBBOT_DASHBOARD_PORT\""
        )
        values = subprocess.check_output(
            ["bash", "-c", command], cwd=PROJECT_ROOT, env=env, text=True
        ).splitlines()
        self.assertEqual(values, [
            str((PROJECT_ROOT / "data" / "jobs.sqlite3").resolve()),
            str((PROJECT_ROOT / "out").resolve()),
            "8765",
        ])

    def test_all_production_ledger_launchers_source_the_same_binding(self) -> None:
        source_line = 'source "$BASE/scripts/production_env.sh"'
        for name in PRODUCTION_LAUNCHERS:
            with self.subTest(name=name):
                self.assertIn(source_line, (PROJECT_ROOT / name).read_text(encoding="utf-8"))

        self.assertNotIn(source_line, (PROJECT_ROOT / "VALIDATE_PRODUCTION.command").read_text(encoding="utf-8"))
        acceptance = (PROJECT_ROOT / "RUN_ACCEPTANCE_INDEED.command").read_text(encoding="utf-8")
        self.assertNotIn(source_line, acceptance)
        self.assertIn('JOBBOT_DATABASE_PATH="$BASE/data/acceptance.sqlite3"', acceptance)
        self.assertIn('JOBBOT_OUTPUT_DIR="$BASE/out/acceptance"', acceptance)

    def test_validator_bundle_remains_isolated_from_stale_parent_values(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            expected_database = root / "validation.sqlite3"
            expected_output = root / "validation-output"
            stale = {
                "JOBBOT_DATABASE_PATH": "/tmp/wrong.sqlite3",
                "JOBBOT_OUTPUT_DIR": "/tmp/wrong-output",
                "JOBBOT_DASHBOARD_PORT": "9999",
            }
            previous = {name: os.environ.get(name) for name in stale}
            try:
                os.environ.update(stale)
                bundle = _isolated_bundle(expected_database, expected_output, 18765)
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            self.assertEqual(bundle.database_path, expected_database.resolve())
            self.assertEqual(bundle.output_dir, expected_output.resolve())
            self.assertEqual(bundle.runtime["runtime"]["dashboard_port"], 18765)


if __name__ == "__main__":
    unittest.main()
