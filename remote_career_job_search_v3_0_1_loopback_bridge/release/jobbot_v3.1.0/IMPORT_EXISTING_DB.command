#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$BASE/data/jobs.sqlite3"
echo "Paste the full path to your existing jobs.sqlite3, then press ENTER:"
read -r OLD
PY="$BASE/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
OLD="$($PY - "$OLD" <<'PY'
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
)"
if [[ ! -f "$OLD" ]]; then
  echo "File not found: $OLD"
  exit 2
fi
"$PY" "$BASE/jobbot_v3.py" import-db "$OLD"
"$PY" "$BASE/jobbot_v3.py" self-test >/dev/null
echo "Import complete. Existing history was backed up and migrated with SQLite integrity checks."
read -r -p "Press ENTER..." _
