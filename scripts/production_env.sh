#!/usr/bin/env bash
set -euo pipefail

JOBBOT_PRODUCTION_BASE="${BASE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
export JOBBOT_DATABASE_PATH="$JOBBOT_PRODUCTION_BASE/data/jobs.sqlite3"
export JOBBOT_OUTPUT_DIR="$JOBBOT_PRODUCTION_BASE/out"
export JOBBOT_DASHBOARD_PORT="8765"
unset JOBBOT_PRODUCTION_BASE
