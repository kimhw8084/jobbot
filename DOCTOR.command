#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
source "$BASE/scripts/mac_python.sh"
source "$BASE/scripts/production_env.sh"
cd "$BASE"
exec "$JOBBOT_PYTHON" -m jobbot doctor
