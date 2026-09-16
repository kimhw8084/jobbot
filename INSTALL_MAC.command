#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE"
PYTHON="$(command -v python3)"
"$PYTHON" -m venv .venv
"$BASE/.venv/bin/python" -m pip install --upgrade pip setuptools
"$BASE/.venv/bin/python" -m pip install --editable "$BASE"
mkdir -p data out logs cache resumes
"$BASE/.venv/bin/python" -m jobbot doctor
echo
echo "One-time setup: load the unpacked extension from: $BASE/extension"
echo "After it is loaded, routine refreshes use ./REFRESH_EXTENSION.command or the run launchers."
open -a "Google Chrome" "chrome://extensions/"
