#!/bin/sh
set -eu

# Starts the Splash monitor in the background and opens it in the browser.
# Arguments go to monitor/monitor.py: ./monitor.command --port 18082
# Environment: SPLASH_MONITOR_PORT.

. "$(dirname "$0")/window.sh"

PORT=${SPLASH_MONITOR_PORT:-18081}
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
STATE_DIR=$HOME/Library/Logs/Splash
LOG=$STATE_DIR/monitor-$PORT.log
PID_FILE=$STATE_DIR/monitor-$PORT.pid

listener=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
if [ -n "$listener" ]; then
    if ps -o command= -p "$listener" | grep -q 'monitor\.py'; then
        echo "The monitor is already running on port $PORT (pid $listener)."
        open "http://127.0.0.1:$PORT/"
        exit 0
    fi
    echo "error: port $PORT is in use by pid $listener" >&2
    exit 1
fi

# The packaged Python, else any Python 3: the monitor needs only the standard library.
python=$(ls -d "$HOME"/.local/opt/splash-*/python/bin/python3 2>/dev/null | tail -n 1 || true)
python=${python:-$(command -v python3 || true)}
if [ -z "$python" ]; then
    echo "error: Python 3 not found" >&2
    exit 1
fi

mkdir -p "$STATE_DIR"
: > "$LOG"
nohup "$python" -u "$ROOT/monitor/monitor.py" --port "$PORT" "$@" >> "$LOG" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$PID_FILE"

attempts=0
until curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$PORT/metrics" 2>/dev/null; do
    attempts=$((attempts + 1))
    if ! kill -0 "$pid" 2>/dev/null || [ "$attempts" -ge 30 ]; then
        rm -f "$PID_FILE"
        echo "error: the monitor did not start. Last lines of $LOG:" >&2
        tail -n 20 "$LOG" >&2
        exit 1
    fi
    sleep 0.5
done

echo "Monitor: http://127.0.0.1:$PORT/"
open "http://127.0.0.1:$PORT/"
