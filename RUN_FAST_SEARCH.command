#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
source "$BASE/scripts/mac_python.sh"
cd "$BASE"
exec caffeinate -dimsu "$JOBBOT_PYTHON" -m jobbot run --mode fast
