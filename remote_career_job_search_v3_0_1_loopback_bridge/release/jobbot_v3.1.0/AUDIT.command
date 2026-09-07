#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"; PY="$BASE/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"; "$PY" "$BASE/jobbot.py" audit
read -r -p "Press ENTER to close..." _
