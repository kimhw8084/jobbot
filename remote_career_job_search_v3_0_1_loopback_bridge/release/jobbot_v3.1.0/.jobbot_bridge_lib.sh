#!/bin/bash
# Internal helper sourced by JobBot .command launchers.
jobbot_start_bridge() {
  local run_id="$1"
  mkdir -p "$BASE/out" "$BASE/data"
  JOBBOT_BRIDGE_READY="$(mktemp -t jobbot-bridge-ready.XXXXXX)"
  rm -f "$JOBBOT_BRIDGE_READY"
  JOBBOT_BRIDGE_LOG="$BASE/out/v3_bridge_run_${run_id}.log"
  "$PY" "$BASE/jobbot_bridge.py" --ready-file "$JOBBOT_BRIDGE_READY" >>"$JOBBOT_BRIDGE_LOG" 2>&1 &
  JOBBOT_BRIDGE_PID=$!
  export JOBBOT_BRIDGE_PID JOBBOT_BRIDGE_READY JOBBOT_BRIDGE_LOG
  local i
  for i in $(seq 1 100); do
    if [[ -s "$JOBBOT_BRIDGE_READY" ]]; then break; fi
    if ! kill -0 "$JOBBOT_BRIDGE_PID" 2>/dev/null; then
      echo "Local JobBot bridge exited during startup."
      echo "Log: $JOBBOT_BRIDGE_LOG"
      tail -n 80 "$JOBBOT_BRIDGE_LOG" 2>/dev/null || true
      return 1
    fi
    sleep 0.1
  done
  if [[ ! -s "$JOBBOT_BRIDGE_READY" ]]; then
    echo "Local JobBot bridge did not become ready. Log: $JOBBOT_BRIDGE_LOG"
    return 1
  fi
  JOBBOT_BRIDGE_PORT=$("$PY" - "$JOBBOT_BRIDGE_READY" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['port'])
PY
)
  JOBBOT_BRIDGE_TOKEN=$("$PY" - "$JOBBOT_BRIDGE_READY" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['token'])
PY
)
  export JOBBOT_BRIDGE_PORT JOBBOT_BRIDGE_TOKEN
  "$PY" - "$JOBBOT_BRIDGE_PORT" "$JOBBOT_BRIDGE_TOKEN" <<'PY'
import json,sys,urllib.request
port,token=int(sys.argv[1]),sys.argv[2]
req=urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'X-JobBot-Token':token})
with urllib.request.urlopen(req,timeout=5) as r:
    d=json.loads(r.read().decode())
assert d.get('ok'),d
PY
  echo "Local bridge ready on 127.0.0.1:$JOBBOT_BRIDGE_PORT"
}

jobbot_stop_bridge() {
  if [[ -n "${JOBBOT_BRIDGE_PID:-}" ]] && kill -0 "$JOBBOT_BRIDGE_PID" 2>/dev/null; then
    kill "$JOBBOT_BRIDGE_PID" 2>/dev/null || true
    for _i in $(seq 1 30); do kill -0 "$JOBBOT_BRIDGE_PID" 2>/dev/null || break; sleep 0.1; done
    kill -9 "$JOBBOT_BRIDGE_PID" 2>/dev/null || true
  fi
  [[ -n "${JOBBOT_BRIDGE_READY:-}" ]] && rm -f "$JOBBOT_BRIDGE_READY" 2>/dev/null || true
}
