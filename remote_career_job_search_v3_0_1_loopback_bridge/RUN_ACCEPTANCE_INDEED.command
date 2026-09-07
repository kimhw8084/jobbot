#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"; cd "$BASE"; PY="$BASE/.venv/bin/python"; EXT_ID="jfdlmelgonjhgnabpbipjefgamedpgfb"
[[ -x "$PY" ]] || { echo "Run INSTALL_MAC.command first."; exit 2; }
source "$BASE/.jobbot_bridge_lib.sh"
RUN_ID=$("$PY" "$BASE/jobbot_v3.py" enqueue-acceptance --platform indeed --days 7 --max-results 20 | tail -n1)
echo "JobBot v3.1.0 Indeed acceptance run #$RUN_ID — three queries, up to 20 job details each (test only)."
jobbot_start_bridge "$RUN_ID"
trap 'jobbot_stop_bridge' EXIT INT TERM
URL="chrome-extension://$EXT_ID/start.html?autorun=1&run_id=$RUN_ID&bridge_port=$JOBBOT_BRIDGE_PORT&bridge_token=$JOBBOT_BRIDGE_TOKEN"
open -a "Google Chrome" "$URL"
"$PY" "$BASE/jobbot_v3.py" wait --run-id "$RUN_ID" --timeout-minutes 180 || true
"$PY" "$BASE/jobbot_v3.py" report --run-id "$RUN_ID"
echo "Bridge log: $JOBBOT_BRIDGE_LOG"
echo "Run: $PY $BASE/jobbot_v3.py status --run-id $RUN_ID --verbose"
read -r -p "Press ENTER to close..." _
