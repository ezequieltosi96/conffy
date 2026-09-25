# conffy command line

`conffy.worker.cli` runs the whole caption pipeline in your terminal: audio →
voice detection → transcription → translation → subtitles printed live. It
needs no Docker and no Valkey, only the models running on the host
(`./scripts/run-host.sh`).

Use it to:

- check that the models work
- try a new audio source or language pair
- tune a glossary
- test segmentation settings for a particular speaker

```bash
uv run python -m conffy.worker.cli <source> [options]
```

## Reading the output

```
[00:49.5 → 00:55.2] #12 Goodhart's Law states that when a measure becomes a target (asr 631 ms · lag 1.2 s)
                  ↳ es: La ley de Goodhart establece que cuando una medida se convierte en un objetivo (mt 1460 ms · backlog 0)
```

- **Grey text** that rewrites itself in place is the *partial*: the sentence
  in progress.
- **`[start → end] #seq text`** is the *final* sentence, with its position in
  the audio.
  - `asr`: how long Whisper took.
  - `lag`: how far behind the audio the pipeline is. ~1 s is normal. If it
    keeps growing, the models can't keep up.
- **`↳ <lang>:`** is the translation.
  - `mt`: how long it took.
  - `backlog`: sentences waiting to be translated.

When the source ends (or you press Ctrl+C), a summary follows:

```
sentences: n=25 avg=3.9 s max=9.3 s
asr: finals=25 partials=78 dropped=0 errors=0 avg=697 ms max_lag=1.6 s
mt:  done=25 errors=0 avg=1266 ms
  error: ...            (only if something failed: the last error of each stage)
```

- `dropped` counts sentences filtered out because they were empty, noise, or
  a typical Whisper hallucination on silence.

## Options

### Source and languages

| Option | Default | Description |
|---|---|---|
| `source` (positional) | – | A file path, or a URL ffmpeg can read: `rtmp://`, `srt://`, `rtsp://`, HLS `.m3u8`, `http(s)://`. Video files work too; only the audio is used. |
| `--lang CODE` | `en` | Spoken language (ISO 639-1). **Set it for non-English audio**: Whisper forced to the wrong language produces nonsense. |
| `--to CODE [CODE ...]` | `es` | Translation targets; several at once are fine (`--to es pt`). `--to none` shows the transcription only. The source language is ignored if listed. |
| `--glossary NAME` | none | A glossary from `config/glossary.yml`. Its terms help Whisper recognize names and keep translations consistent. |
| `--glossary-file PATH` | `config/glossary.yml` (or `$GLOSSARY_FILE`) | Another glossary file. |

### Speed and display

| Option | Default | Description |
|---|---|---|
| `--fast` | off | Files: read as fast as possible instead of at 1× speed. Good for quick checks. `lag` numbers are meaningless in this mode. |
| `--no-partials` | off | Only final sentences. Halves the ASR load; useful when models are slow or remote. |

### Models

| Option | Default | Description |
|---|---|---|
| `--asr-url URL` | `$ASR_URL` or `http://127.0.0.1:8081` | whisper-server base URL |
| `--mt-url URL` | `$MT_URL` or `http://127.0.0.1:11434/v1` | OpenAI-compatible translation API (Ollama, vLLM, Gemini, …) |
| `--mt-model NAME` | `$MT_MODEL` or `gemma4:e4b` | translation model |

Other environment variables read by the CLI:

| Variable | Description |
|---|---|
| `MT_API_KEY` | key for hosted translation providers |
| `MT_REASONING_EFFORT` | `none` by default; `minimal` for Gemini 3.x; empty to not send it (see `docs/MODELS.md`) |

### Segmentation (how audio is cut into sentences)

The defaults suit most speakers. Change them only after comparing runs on a
real recording of that speaker.

| Option | Default | Description |
|---|---|---|
| `--min-silence S` | `0.6` | Silence that ends a sentence. |
| `--soft-max S` | `8.0` | Once a sentence is this long, cut at the first short pause. |
| `--soft-pause S` | `0.2` | What counts as a short pause after `--soft-max`. |
| `--hard-max S` | `15.0` | Cut no matter what. |

What we measured:

- Shorter cuts (e.g. `--soft-max 6 --soft-pause 0.15 --hard-max 12`) give
  shorter subtitles for speakers who barely pause.
- The cost is occasional recognition errors at forced cuts, because Whisper
  loses the context around the cut.
- Forced cuts show up as sentences of exactly `--hard-max` seconds in the
  summary.

## Examples

```bash
# Quickest check: the bundled 11-second sample
uv run python -m conffy.worker.cli samples/jfk.wav

# English talk, Spanish subtitles, with the event glossary
uv run python -m conffy.worker.cli samples/charla.mp3 --glossary nerdearla

# Spanish talk, English subtitles
uv run python -m conffy.worker.cli samples/charla_es.mp3 --lang es --to en --glossary nerdearla

# Several languages at once
uv run python -m conffy.worker.cli samples/charla.mp3 --to es pt

# Transcription only, as fast as possible
uv run python -m conffy.worker.cli samples/charla.mp3 --to none --fast

# A live stream received by MediaMTX (make up-obs; OBS or `make mic` publishing to it)
uv run python -m conffy.worker.cli rtmp://localhost:1935/sala-yt

# A YouTube *live* stream: yt-dlp resolves the real HLS URL
uv run python -m conffy.worker.cli "$(yt-dlp -g -f best 'https://www.youtube.com/watch?v=LIVE_ID')"

# Translation through Gemini instead of the local model
MT_URL=https://generativelanguage.googleapis.com/v1beta/openai MT_API_KEY=... \
MT_MODEL=<gemini flash model> MT_REASONING_EFFORT=minimal \
  uv run python -m conffy.worker.cli samples/charla.mp3

# A speaker who barely pauses: shorter subtitles
uv run python -m conffy.worker.cli samples/charla_es.mp3 --lang es --to en \
  --soft-max 6 --soft-pause 0.15 --hard-max 12
```

**YouTube videos that are *not* live:** `yt-dlp -g` returns a normal
download URL. Without the 1× pacing that files get, ffmpeg would read the
whole video in seconds. Download it first (`yt-dlp -x --audio-format mp3 -o
"samples/local/talk.%(ext)s" URL`) and pass the file, or capture it through
OBS (`docs/OBS.md`).

**Microphone:** the CLI reads files and URLs, not audio devices. Publish the
mic to MediaMTX with `make mic DEVICE=<n> ID=sala-mic` (list devices with
`make mic-devices`), then read `rtmp://localhost:1935/sala-mic`.

## Other command-line tools

### Scripts

| Command | What it does |
|---|---|
| `./scripts/setup-host.sh` | Once: installs ffmpeg, whisper.cpp and Ollama if missing, downloads the models, and runs a smoke test. Env: `WHISPER_MODEL`, `MT_MODEL`, `SKIP_SMOKE=1`. |
| `./scripts/run-host.sh` | Starts whisper-server (:8081) and Ollama (:11434) with Metal. Keep it running. Env: `WHISPER_PORT`, `WHISPER_THREADS`, `OLLAMA_NUM_PARALLEL`, `BIND_HOST`. |

### Load and capacity tools

| Command | What it does |
|---|---|
| `uv run python loadtest/sse_clients.py --viewers 1500 --duration 120` | Opens N simulated viewers over every live talk and reports delivery delay (p50/p95/p99) and dropped connections. Options: `--base`, `--ramp`, `--json FILE`. |
| `uv run python loadtest/watch_sessions.py` | Every 5 s: each talk's ASR/MT latency, lag and last caption age. Options: `--base`, `--every`, `--csv FILE`. |

### Makefile shortcuts

| Target | What it does |
|---|---|
| `make setup` / `make host` | `setup-host.sh` / `run-host.sh` |
| `make up` | Build and start the stack at http://localhost:8080 |
| `make up-obs` | Same, plus MediaMTX to receive OBS/vMix/ffmpeg streams on `rtmp://localhost:1935` |
| `make down` | Stop everything |
| `make reset` | Stop and **wipe all talks and captions**; `sessions.yml` is seeded again on the next `up` |
| `make logs` / `make ps` | api and worker logs / container status |
| `make test` | Run the test suite |
| `make cli FILE=...` | The CLI with the `nerdearla` glossary |
| `make room ID=... FROM=en TO=es TITLE="..."` | Create a talk fed by RTMP (`rtmp://mediamtx:1935/<ID>`) and print its viewer and overlay URLs |
| `make start ID=...` / `make stop ID=...` | Start or stop a talk |
| `make mic-devices` / `make mic DEVICE=n ID=...` | List the Mac's audio inputs / send one of them to MediaMTX as a talk |
| `make demo-nomodels` | The stack with fake models (no GPU, no downloads) |
| `make loadtest-up` / `make loadtest` | 15 fake talks + 1,500 viewers (wipes Valkey) |
| `make watch` | `watch_sessions.py`, saving to `loadtest/capacity.csv` |