#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$BASE/.venv/bin/python"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3 || true)"; fi
if [[ -z "$PY" ]]; then echo "JobBot native host: python3 not found" >&2; exit 127; fi
exec "$PY" "$BASE/native_host/jobbot_native_host.py" "$@"
