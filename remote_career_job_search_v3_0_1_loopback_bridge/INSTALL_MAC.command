#!/bin/bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"; cd "$BASE"
EXT_ID="jfdlmelgonjhgnabpbipjefgamedpgfb"; CHROME_APP="/Applications/Google Chrome.app"
[[ -d "$CHROME_APP" ]] || { echo "Google Chrome is required at $CHROME_APP"; exit 2; }
PY_SYS="$(command -v python3 || true)"; [[ -n "$PY_SYS" ]] || { echo "python3 is required."; exit 2; }
"$PY_SYS" - <<'PY'
import sys
if sys.version_info < (3,11): raise SystemExit(f"Python 3.11+ required; found {sys.version.split()[0]}")
PY
if [[ ! -x "$BASE/.venv/bin/python" ]]; then echo "Creating local Python environment..."; "$PY_SYS" -m venv "$BASE/.venv"; fi
PY="$BASE/.venv/bin/python"
chmod +x "$BASE/jobbot_bridge.py" "$BASE/jobbot_v3.py" "$BASE"/*.command 2>/dev/null || true
# v3.0 used Native Messaging. v3.1.0 no longer needs it; remove only JobBot's old manifest.
rm -f "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.jobbot.local.json" 2>/dev/null || true
"$PY" "$BASE/jobbot_v3.py" self-test
"$PY" "$BASE/jobbot_bridge.py" --self-test
"$PY" "$BASE/jobbot_v3.py" install-check
cat <<EOF

LOCAL INSTALL COMPLETE — v3.1.0 LOOPBACK BRIDGE.

ONE-TIME CHROME SETUP (same normal Chrome profile where LinkedIn/Indeed/Glassdoor work):
1. Open chrome://extensions and turn on Developer mode.
2. REMOVE the older JobBot v3 extension that points to another folder.
3. Click Load unpacked and choose:
   $BASE/extension
4. Confirm extension ID: $EXT_ID
5. Sign into LinkedIn, Indeed and Glassdoor normally in THIS SAME Chrome profile.

Then run RUN_ACCEPTANCE_INDEED.command.
No Chrome Native Messaging host is used in this build.
EOF
open -a "Google Chrome" "chrome://extensions"
open "$BASE/extension"
