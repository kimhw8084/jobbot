#!/usr/bin/env sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$BASE/.venv/bin/python" -m jobbot "$@"
