#!/bin/bash
set -euo pipefail
JOBBOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBBOT_PYTHON="$JOBBOT_ROOT/.venv/bin/python"
if [[ ! -x "$JOBBOT_PYTHON" ]]; then
  echo "JobBot is not installed. Double-click INSTALL_MAC.command first." >&2
  exit 2
fi
export JOBBOT_ROOT JOBBOT_PYTHON
