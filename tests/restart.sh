#!/usr/bin/env bash
# Starts a mock Google Sheet (port $MOCK_PORT, default 8090, running apps-script/Code.gs on now.csv) and a test
# board (port $BOARD_PORT, default 8099) that syncs with it. Nothing here touches a real sheet or the live board.
#   restart.sh                fresh board data, restart both
#   restart.sh keep           keep the board's data
#   restart.sh keep boardonly restart only the board (the mock keeps running)
#   restart.sh stopboard      stop the board      restart.sh stopall   stop both
T="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$T")"
MP="${MOCK_PORT:-8090}"; BP="${BOARD_PORT:-8099}"  # set both to run beside another test run
RUN="$T/.run/$BP"
mkdir -p "$RUN"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in "$ROOT/.venv/Scripts/python.exe" "$ROOT/.venv/bin/python" python3 python; do
    if "$c" -c "import flask" >/dev/null 2>&1; then PY="$c"; break; fi  # skips the Windows Store "python3" stub
  done
fi

stop() { # $1 = board | mock
  local port; [ "$1" = board ] && port=$BP || port=$MP
  if [ -f "$RUN/$1.pid" ]; then
    local p; p=$(cat "$RUN/$1.pid")
    # Windows: a venv's python.exe is a launcher that starts the real server as a child; end the whole tree.
    if [ -r "/proc/$p/winpid" ] && command -v taskkill >/dev/null 2>&1; then
      taskkill //F //T //PID "$(cat "/proc/$p/winpid")" >/dev/null 2>&1
    else
      kill "$p" 2>/dev/null
    fi
  fi
  rm -f "$RUN/$1.pid"
  if command -v taskkill >/dev/null 2>&1; then # Windows: make sure the port is free
    local pid; pid=$(netstat -ano | grep -E "(127.0.0.1|0.0.0.0):$port .*LISTENING" | awk '{print $5}' | head -1)
    [ -n "$pid" ] && taskkill //F //PID "$pid" >/dev/null 2>&1
  fi
  for i in $(seq 1 20); do curl -s -m 1 "localhost:$port" >/dev/null || return 0; sleep 0.5; done # gone for real
}

case "$1" in
  stopboard) stop board; exit 0 ;;
  stopall) stop board; stop mock; exit 0 ;;
esac

stop board
[ "$2" = boardonly ] || stop mock
sleep 1
if [ "$1" != keep ]; then # a server that just ended may hold its database files for a moment
  for i in $(seq 1 20); do rm -rf "$RUN/data" 2>/dev/null && break; sleep 0.5; done
  mkdir -p "$RUN/data"
fi
if [ "$2" != boardonly ]; then
  (cd "$T" && exec node mock.js "$ROOT/apps-script/Code.gs" now.csv $MP http://127.0.0.1:$BP) > "$RUN/mock.log" 2>&1 < /dev/null &
  echo $! > "$RUN/mock.pid"
fi
# The whole subshell is redirected, so a caller reading this script's output isn't held open by the servers.
(cd "$ROOT" && DATA_DIR="$RUN/data" SHEET_SCRIPT_URL=http://127.0.0.1:$MP/exec SYNC_SECRET=s3cret SYNC_INTERVAL=10 PORT=$BP \
  exec "$PY" app.py) >> "$RUN/board.log" 2>&1 < /dev/null &
echo $! > "$RUN/board.pid"
for i in $(seq 1 30); do curl -s localhost:$BP/api/sync/status | grep -q '"lastPull":[0-9]' && break; sleep 1; done
