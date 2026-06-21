#!/bin/bash
# Launch the local dashboard and open it in the browser. Stop with Ctrl-C.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8765}"

python3 "$ROOT/dashboard/server.py" "$PORT" &
SRV=$!
sleep 1
open "http://127.0.0.1:$PORT"
trap 'kill $SRV 2>/dev/null || true' INT TERM
wait $SRV
