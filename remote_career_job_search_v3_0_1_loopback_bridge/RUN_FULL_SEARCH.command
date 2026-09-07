#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"; cd "$BASE"; PY="$BASE/.venv/bin/python"; EXT_ID="jfdlmelgonjhgnabpbipjefgamedpgfb"
[[ -x "$PY" ]] || { echo "Run INSTALL_MAC.command first."; exit 2; }
"$PY" "$BASE/jobbot_v3.py" install-check >/dev/null || { echo "Run INSTALL_MAC.command first and reload the v3.1.0 extension."; exit 2; }
source "$BASE/.jobbot_bridge_lib.sh"
RUN_ID=$("$PY" "$BASE/jobbot_v3.py" enqueue-production --mode deep | tail -n1)
echo "JobBot v3.1.0 FULL SEARCH run #$RUN_ID"
echo "LinkedIn + Indeed + Glassdoor; every researched keyword; NO production result-count cap."
jobbot_start_bridge "$RUN_ID"; trap 'jobbot_stop_bridge' EXIT INT TERM
URL="chrome-extension://$EXT_ID/start.html?autorun=1&run_id=$RUN_ID&bridge_port=$JOBBOT_BRIDGE_PORT&bridge_token=$JOBBOT_BRIDGE_TOKEN"
open -a "Google Chrome" "$URL"
"$PY" "$BASE/jobbot_v3.py" wait --run-id "$RUN_ID" --timeout-minutes 1440 || true
"$PY" "$BASE/jobbot_v3.py" report --run-id "$RUN_ID"
jobbot_stop_bridge; trap - EXIT INT TERM
echo
echo "Primary Big-3 run complete/partial. Running supplemental feeds + canonical employer verification..."
"$PY" "$BASE/jobbot.py" run --mode deep || true
"$PY" "$BASE/jobbot.py" audit || true
"$PY" "$BASE/jobbot.py" open || true
read -r -p "Press ENTER to close..." _
