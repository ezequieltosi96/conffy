#!/usr/bin/env bash
# conffy · setup-host.sh
# Prepares a macOS (Apple Silicon) host to run the AI models natively with Metal.
# Docker on macOS cannot use the GPU, so the models live on the host and the
# containers reach them through host.docker.internal.
#
# Idempotent: safe to run as many times as you want.
# Written for macOS's default bash 3.2 (no bash-4 features).
#
# Env overrides:
#   WHISPER_MODEL   ggml model name     (default: large-v3-turbo-q5_0)
#   MT_MODEL        Ollama model tag    (default: gemma4:e4b)
#   MODELS_DIR      where to store ggml (default: ./models)
#   SKIP_SMOKE=1    skip the smoke tests

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${MODELS_DIR:-$ROOT/models}"
SAMPLES_DIR="$ROOT/samples"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo-q5_0}"
WHISPER_FILE="$MODELS_DIR/ggml-$WHISPER_MODEL.bin"
WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$WHISPER_MODEL.bin"
MT_MODEL="${MT_MODEL:-gemma4:e4b}"
SMOKE_WHISPER_PORT=18081
OLLAMA_URL="http://127.0.0.1:11434"

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok  ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail ]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- cleanup
TMP_PIDS=""
cleanup() {
  for pid in $TMP_PIDS; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

wait_http() { # url, seconds
  local i=0
  while [ "$i" -lt "$2" ]; do
    if curl -fsS -o /dev/null "$1" 2>/dev/null; then return 0; fi
    sleep 1; i=$((i + 1))
  done
  return 1
}

# ---------------------------------------------------------------- checks
[ "$(uname -s)" = "Darwin" ] || die "This script targets macOS. On Linux, run the models as containers (see README)."
[ "$(uname -m)" = "arm64" ] || warn "Not Apple Silicon: whisper.cpp will run without Metal and be slow."
command -v brew >/dev/null 2>&1 || die "Homebrew is required: https://brew.sh"

# ---------------------------------------------------------------- packages
# Detect by command in PATH, not by brew: tools installed another way
# (e.g. the Ollama desktop app from ollama.com) are respected, not reinstalled.
# Format: "<brew formula>:<command it provides>"
log "Checking host tools (ffmpeg, whisper-cpp, ollama)"
for entry in ffmpeg:ffmpeg whisper-cpp:whisper-server ollama:ollama; do
  pkg="${entry%%:*}"; cmd="${entry##*:}"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd found at $(command -v "$cmd")"
  else
    log "Installing $pkg with Homebrew"
    brew install "$pkg"
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' still not in PATH after 'brew install $pkg'"
  fi
done

# Gemma 4 needs Ollama >= 0.20.0
OLLAMA_VERSION="$(ollama --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
if [ -z "$OLLAMA_VERSION" ]; then
  warn "Could not read Ollama's version; continuing"
else
  OV_MAJOR="${OLLAMA_VERSION%%.*}"; OV_REST="${OLLAMA_VERSION#*.}"; OV_MINOR="${OV_REST%%.*}"
  if [ "$OV_MAJOR" -eq 0 ] && [ "$OV_MINOR" -lt 20 ]; then
    die "Ollama $OLLAMA_VERSION is too old for Gemma 4 (needs >= 0.20.0). Update the app, or: brew upgrade ollama"
  fi
  ok "Ollama version $OLLAMA_VERSION"
fi

# ---------------------------------------------------------------- whisper model
mkdir -p "$MODELS_DIR" "$SAMPLES_DIR"
if [ -s "$WHISPER_FILE" ]; then
  ok "Whisper model present: $(basename "$WHISPER_FILE")"
else
  log "Downloading Whisper model $WHISPER_MODEL (~500 MB for turbo-q5)"
  curl -fL --retry 3 --progress-bar -o "$WHISPER_FILE.part" "$WHISPER_URL" \
    || die "Download failed: $WHISPER_URL"
  mv "$WHISPER_FILE.part" "$WHISPER_FILE"
  ok "Saved $WHISPER_FILE"
fi

# ---------------------------------------------------------------- ollama model
if curl -fsS -o /dev/null "$OLLAMA_URL/api/version" 2>/dev/null; then
  ok "Ollama already running"
else
  log "Starting a temporary Ollama server to pull the model"
  ollama serve >"/tmp/conffy-ollama-setup.log" 2>&1 &
  TMP_PIDS="$TMP_PIDS $!"
  wait_http "$OLLAMA_URL/api/version" 30 || die "Ollama did not start. See /tmp/conffy-ollama-setup.log"
fi
log "Pulling $MT_MODEL (several GB the first time)"
ollama pull "$MT_MODEL"
ok "Ollama model ready: $MT_MODEL"

# ---------------------------------------------------------------- smoke tests
if [ "${SKIP_SMOKE:-0}" = "1" ]; then
  warn "Skipping smoke tests"; exit 0
fi

SAMPLE="$SAMPLES_DIR/jfk.wav"
if [ ! -s "$SAMPLE" ]; then
  curl -fsSL -o "$SAMPLE" "https://github.com/ggml-org/whisper.cpp/raw/refs/heads/master/samples/jfk.wav" \
    || die "Could not download the test sample"
fi

log "Smoke test 1/2: whisper-server transcription"
whisper-server --host 127.0.0.1 --port "$SMOKE_WHISPER_PORT" -m "$WHISPER_FILE" \
  >"/tmp/conffy-whisper-smoke.log" 2>&1 &
TMP_PIDS="$TMP_PIDS $!"
wait_http "http://127.0.0.1:$SMOKE_WHISPER_PORT/" 60 \
  || die "whisper-server did not start. See /tmp/conffy-whisper-smoke.log"
ASR_OUT="$(curl -fsS -w '\n%{time_total}' "http://127.0.0.1:$SMOKE_WHISPER_PORT/inference" \
  -F "file=@$SAMPLE" -F "response_format=json" -F "language=en")" \
  || die "Transcription request failed"
ASR_TIME="$(printf '%s' "$ASR_OUT" | tail -n 1)"
ASR_TEXT="$(printf '%s' "$ASR_OUT" | sed '$d')"
printf '%s' "$ASR_TEXT" | grep -qi "country" || die "Unexpected transcription: $ASR_TEXT"
ok "Whisper answered in ${ASR_TIME}s (11s of audio): $ASR_TEXT"

log "Smoke test 2/2: translation through Ollama's OpenAI-compatible API"
MT_OUT="$(curl -fsS -w '\n%{time_total}' "$OLLAMA_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MT_MODEL\",\"temperature\":0,\"messages\":[
        {\"role\":\"system\",\"content\":\"Translate the user's text from English to Spanish. Reply with the translation only.\"},
        {\"role\":\"user\",\"content\":\"Ask not what your country can do for you.\"}]}")" \
  || die "Translation request failed"
MT_TIME="$(printf '%s' "$MT_OUT" | tail -n 1)"
MT_TEXT="$(printf '%s' "$MT_OUT" | sed '$d' | grep -o '"content":"[^"]*"' | head -n 1)"
[ -n "$MT_TEXT" ] || die "Unexpected translation response: $(printf '%s' "$MT_OUT" | sed '$d')"
ok "Gemma answered in ${MT_TIME}s (includes model load on first call): $MT_TEXT"

echo
ok "Host ready. Next: scripts/run-host.sh"
