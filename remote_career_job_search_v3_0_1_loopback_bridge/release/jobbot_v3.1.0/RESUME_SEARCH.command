#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"; cd "$BASE"; PY="$BASE/.venv/bin/python"; EXT_ID="jfdlmelgonjhgnabpbipjefgamedpgfb"
[[ -x "$PY" ]] || { echo "Run INSTALL_MAC.command first."; exit 2; }
source "$BASE/.jobbot_bridge_lib.sh"
RUN_ID=$("$PY" "$BASE/jobbot_v3.py" resume-run | tail -n1)
echo "Resuming JobBot run #$RUN_ID from persisted checkpoints."
jobbot_start_bridge "$RUN_ID"; trap 'jobbot_stop_bridge' EXIT INT TERM
URL="chrome-extension://$EXT_ID/start.html?autorun=1&run_id=$RUN_ID&bridge_port=$JOBBOT_BRIDGE_PORT&bridge_token=$JOBBOT_BRIDGE_TOKEN"
open -a "Google Chrome" "$URL"
"$PY" "$BASE/jobbot_v3.py" wait --run-id "$RUN_ID" --timeout-minutes 1440 || true
"$PY" "$BASE/jobbot_v3.py" report --run-id "$RUN_ID"
read -r -p "Press ENTER to close..." _
