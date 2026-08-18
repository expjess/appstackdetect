#!/usr/bin/env bash
# Start (or restart) the App Stack Detector web service.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8787}"
PIDFILE="$DIR/.server.pid"
LOG="${LOG:-$DIR/logs/server.log}"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"
  sleep 1
fi

mkdir -p "$(dirname "$LOG")"
cd "$DIR"

# Licence acquisition is allowed because the signed-in Apple ID is a dedicated
# account attached to no device, so nothing can auto-install anywhere.
# Set ALLOW_PURCHASE=0 to turn it off again.
export ALLOW_PURCHASE="${ALLOW_PURCHASE:-1}"
setsid nohup "$DIR/.venv/bin/python" -m uvicorn app.server:app \
  --host 127.0.0.1 --port "$PORT" >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 3
curl -sf "http://127.0.0.1:$PORT/api/health" && echo " <- listening on $PORT (log: $LOG)"
