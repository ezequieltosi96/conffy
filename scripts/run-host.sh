#!/usr/bin/env bash
# conffy · run-host.sh
# Starts the model servers on the macOS host, in the foreground. Ctrl+C stops both.
# Containers reach them at http://host.docker.internal:<port>.
#
# Env overrides:
#   WHISPER_MODEL        ggml model name              (default: large-v3-turbo-q5_0)
#   WHISPER_PORT         whisper-server port          (default: 8081)
#   WHISPER_THREADS      CPU threads for whisper      (default: 4)
#   MT_MODEL             Ollama model to warm up      (default: gemma4:e4b)
#   OLLAMA_NUM_PARALLEL  concurrent Ollama requests   (default: 4)
#   BIND_HOST            listen address               (default: 127.0.0.1)
#     Docker Desktop and OrbStack forward host.docker.internal to the host's
#     loopback, so 127.0.0.1 works and keeps the models off the LAN. Use 0.0.0.0
#     only if your Docker runtime cannot reach them.
#
# Note: whisper-server handles one request at a time. That is fine for a few
# sessions; to scale on one machine, run more instances on other ports.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo-q5_0}"
WHISPER_FILE="${MODELS_DIR:-$ROOT/models}/ggml-$WHISPER_MODEL.bin"
WHISPER_PORT="${WHISPER_PORT:-8081}"
WHISPER_THREADS="${WHISPER_THREADS:-4}"
MT_MODEL="${MT_MODEL:-gemma4:e4b}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"
OLLAMA_URL="http://127.0.0.1:11434"

log()  { printf '\033[1;34m[host ]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok  ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail ]\033[0m %s\n' "$*" >&2; exit 1; }

PIDS=""
cleanup() {
  log "Stopping model servers"
  for pid in $PIDS; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

wait_http() { # url, seconds
  local i=0
  while [ "$i" -lt "$2" ]; do
    if curl -fsS -o /dev/null "$1" 2>/dev/null; then return 0; fi
    sleep 1; i=$((i + 1))
  done
  return 1
}

[ -s "$WHISPER_FILE" ] || die "Missing $WHISPER_FILE. Run scripts/setup-host.sh first."
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------- whisper
log "Starting whisper-server on $BIND_HOST:$WHISPER_PORT"
whisper-server --host "$BIND_HOST" --port "$WHISPER_PORT" \
  -m "$WHISPER_FILE" -t "$WHISPER_THREADS" \
  >"$LOG_DIR/whisper.log" 2>&1 &
PIDS="$PIDS $!"
wait_http "http://127.0.0.1:$WHISPER_PORT/" 60 || die "whisper-server did not start. See logs/whisper.log"
ok "whisper-server ready"

# ---------------------------------------------------------------- ollama
if curl -fsS -o /dev/null "$OLLAMA_URL/api/version" 2>/dev/null; then
  warn "Ollama is already running (desktop app or brew service)."
  warn "OLLAMA_NUM_PARALLEL / OLLAMA_KEEP_ALIVE from this script will NOT apply to it."
else
  log "Starting Ollama on $BIND_HOST:11434"
  OLLAMA_HOST="$BIND_HOST:11434" \
  OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-4}" \
  OLLAMA_KEEP_ALIVE="-1" \
    ollama serve >"$LOG_DIR/ollama.log" 2>&1 &
  PIDS="$PIDS $!"
  wait_http "$OLLAMA_URL/api/version" 30 || die "Ollama did not start. See logs/ollama.log"
fi

log "Warming up $MT_MODEL (loads it into memory so the first caption is not slow)"
curl -fsS -o /dev/null "$OLLAMA_URL/api/generate" \
  -d "{\"model\":\"$MT_MODEL\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1}" \
  || warn "Warm-up failed; the first translation will be slower"
ok "Ollama ready"

echo
ok "Models running. From containers use:"
echo "     ASR_URL=http://host.docker.internal:$WHISPER_PORT"
echo "     MT_URL=http://host.docker.internal:11434/v1"
echo "     Logs: $LOG_DIR/  ·  Ctrl+C to stop"
wait
