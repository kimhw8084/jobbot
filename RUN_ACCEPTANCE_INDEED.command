#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
source "$BASE/scripts/mac_python.sh"
export JOBBOT_DATABASE_PATH="$BASE/data/acceptance.sqlite3"
export JOBBOT_OUTPUT_DIR="$BASE/out/acceptance"
export JOBBOT_DASHBOARD_PORT="8766"
cd "$BASE"
exec "$JOBBOT_PYTHON" -m jobbot acceptance --platform indeed --days 7 --max-results 20
