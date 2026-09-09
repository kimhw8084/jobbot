#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
source "$BASE/scripts/mac_python.sh"
cd "$BASE"
echo "JobBot bounded live micro-validator: isolated database only (maximum 5 minutes)"
echo "Production database is protected: $BASE/data/jobs.sqlite3"
exec caffeinate -dimsu "$JOBBOT_PYTHON" -m jobbot validate-production --stage micro "$@"
