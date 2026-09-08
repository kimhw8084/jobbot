#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
source "$BASE/scripts/mac_python.sh"
source "$BASE/scripts/production_env.sh"
if [[ $# -ne 1 ]]; then
  echo "Usage: drag an existing jobs.sqlite3 onto this launcher, or run: IMPORT_EXISTING_DB.command /path/to/jobs.sqlite3" >&2
  exit 2
fi
cd "$BASE"
exec "$JOBBOT_PYTHON" -m jobbot import-db "$1"
