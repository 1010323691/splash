#!/bin/sh
set -eu

# Stops the Splash server on SPLASH_PORT, however it was started.

. "$(dirname "$0")/window.sh"

PORT=${SPLASH_PORT:-8000}
PID_FILE=$HOME/Library/Logs/Splash/serve-$PORT.pid

is_splash() {
    ps -o command= -p "$1" 2>/dev/null \
        | grep -Eq 'server\.server|install/launcher\.py'
}

wait_exit() {
    remaining=$1
    while [ "$remaining" -gt 0 ]; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
        remaining=$((remaining - 1))
    done
    return 1
}

# The PID file also covers a server still downloading or loading, which does
# not listen yet; the port covers one started by hand.
pid=$(cat "$PID_FILE" 2>/dev/null || true)
if [ -z "$pid" ] || ! is_splash "$pid"; then
    pid=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
fi
if [ -z "$pid" ]; then
    rm -f "$PID_FILE"
    echo "Splash is not running on port $PORT."
    exit 0
fi
if ! is_splash "$pid"; then
    echo "error: port $PORT is held by pid $pid, which is not Splash" >&2
    exit 1
fi

echo "Stopping Splash (pid $pid)..."
kill -TERM "$pid"
if ! wait_exit 30; then
    # A signal during shutdown stops the engine at once.
    echo "Still stopping after 30 s; stopping the engine now..."
    kill -INT "$pid" 2>/dev/null || true
    if ! wait_exit 15; then
        echo "error: Splash (pid $pid) did not exit" >&2
        exit 1
    fi
fi

rm -f "$PID_FILE"
echo "Stopped."
