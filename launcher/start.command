#!/bin/sh
set -eu

# Starts Splash in the background and returns once it is ready.
# Arguments go to splash serve: ./start.command --max-memory 28G --idle-release off
# Environment: SPLASH_MODEL, SPLASH_PORT, SPLASH_BIN, HF_HUB_CACHE (default: the
# checkout's models/).

. "$(dirname "$0")/window.sh"

MODEL=${SPLASH_MODEL:-unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS}
PORT=${SPLASH_PORT:-8000}
HF_HUB_CACHE=${HF_HUB_CACHE:-$(cd -- "$(dirname -- "$0")/.." && pwd -P)/models}
export HF_HUB_CACHE
STATE_DIR=$HOME/Library/Logs/Splash
LOG=$STATE_DIR/serve-$PORT.log
PID_FILE=$STATE_DIR/serve-$PORT.pid

splash=${SPLASH_BIN:-$(command -v splash || echo "$HOME/.local/bin/splash")}
if [ ! -x "$splash" ]; then
    echo "error: splash not found at $splash; set SPLASH_BIN" >&2
    exit 1
fi

listener=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
if [ -n "$listener" ]; then
    if ps -o command= -p "$listener" | grep -q 'server\.server'; then
        echo "Splash is already running on port $PORT (pid $listener)."
        exit 0
    fi
    echo "error: port $PORT is in use by pid $listener" >&2
    exit 1
fi

mkdir -p "$STATE_DIR"
: > "$LOG"
nohup "$splash" serve --model "$MODEL" --port "$PORT" "$@" >> "$LOG" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$PID_FILE"
echo "Starting $MODEL on port $PORT (pid $pid). Log: $LOG"

# The first start also downloads the model, so only the process's end stops
# this wait.
while kill -0 "$pid" 2>/dev/null; do
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/status" 2>/dev/null \
        | grep -q '"ready":true'; then
        echo "Ready: http://127.0.0.1:$PORT"
        exit 0
    fi
    sleep 2
done

rm -f "$PID_FILE"
echo "error: Splash exited during startup. Last lines of $LOG:" >&2
tail -n 20 "$LOG" >&2
exit 1
