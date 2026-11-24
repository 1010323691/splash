#!/bin/sh
set -eu

# Stops the Splash monitor on SPLASH_MONITOR_PORT. The server keeps running.

. "$(dirname "$0")/window.sh"

PORT=${SPLASH_MONITOR_PORT:-18081}
PID_FILE=$HOME/Library/Logs/Splash/monitor-$PORT.pid

pid=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
if [ -z "$pid" ]; then
    rm -f "$PID_FILE"
    echo "The monitor is not running on port $PORT."
    exit 0
fi
if ! ps -o command= -p "$pid" | grep -q 'monitor\.py'; then
    echo "error: port $PORT is held by pid $pid, which is not the monitor" >&2
    exit 1
fi

kill -TERM "$pid"
remaining=10
while kill -0 "$pid" 2>/dev/null; do
    if [ "$remaining" -le 0 ]; then
        echo "error: the monitor (pid $pid) did not exit" >&2
        exit 1
    fi
    sleep 1
    remaining=$((remaining - 1))
done

rm -f "$PID_FILE"
echo "Stopped the monitor."
