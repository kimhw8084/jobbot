#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"; cd "$BASE"; PY="$BASE/.venv/bin/python"; EXT_ID="jfdlmelgonjhgnabpbipjefgamedpgfb"
[[ -x "$PY" ]] || { echo "Run INSTALL_MAC.command first."; exit 2; }
source "$BASE/.jobbot_bridge_lib.sh"
RUN_ID=$("$PY" "$BASE/jobbot_v3.py" enqueue-production --mode deep --platform indeed | tail -n1)
echo "JobBot Indeed platform run #$RUN_ID — all enabled strategy queries, no production result cap."
jobbot_start_bridge "$RUN_ID"; trap 'jobbot_stop_bridge' EXIT INT TERM
open -a "Google Chrome" "chrome-extension://$EXT_ID/start.html?autorun=1&run_id=$RUN_ID&bridge_port=$JOBBOT_BRIDGE_PORT&bridge_token=$JOBBOT_BRIDGE_TOKEN"
"$PY" "$BASE/jobbot_v3.py" wait --run-id "$RUN_ID" --timeout-minutes 1440 || true
"$PY" "$BASE/jobbot_v3.py" report --run-id "$RUN_ID"
read -r -p "Press ENTER to close..." _
