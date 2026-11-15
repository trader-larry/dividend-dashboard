#!/usr/bin/env bash
# Stops the dividend dashboard. Optional: pass the port if you started it with --port.
PORT="${1:-8765}"
if curl -s -f -X POST -H "X-Dashboard-Stop: 1" "http://127.0.0.1:${PORT}/api/shutdown" >/dev/null; then
  echo "Dashboard stopped."
  exit 0
fi
PIDS="$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null)"
if [ -n "$PIDS" ]; then
  kill $PIDS && echo "Dashboard stopped."
else
  echo "The dashboard is not running on port ${PORT}."
fi
