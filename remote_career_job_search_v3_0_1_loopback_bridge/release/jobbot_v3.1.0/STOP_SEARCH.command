#!/bin/bash
BASE="$(cd "$(dirname "$0")" && pwd)"; PY="$BASE/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"; "$PY" "$BASE/jobbot_v3.py" emergency-stop
