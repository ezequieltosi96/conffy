# conffy · Contracts

The code version of this document is `src/conffy/contracts.py`. If they disagree, the code wins, and both get fixed in the same commit.

## Processes

| Process | Owns | Talks to |
|---|---|---|
| `api` (FastAPI, stateless, N replicas) | sessions CRUD, browser audio ingest, SSE fan-out, exports | Valkey only |
| `worker` (N replicas, one session each) | audio → VAD → ASR → MT → captions | Valkey, ASR provider, MT provider |

`api` and `worker` never call each other. Valkey is the only shared medium.

## Audio

Canonical format everywhere after ingest: PCM s16le, 16 kHz, mono (32 000 bytes/s). Browsers send 100 ms frames (3 200 bytes).

Sources (`SessionConfig.source.kind`):

- `file`: path inside the container. `realtime=true` reads at 1x speed (ffmpeg `-re`), `loop=true` restarts at the end.
- `url`: anything ffmpeg can read (`rtmp://mediamtx:1935/<id>`, `srt://`, `rtsp://`, HLS, http).
- `browser`: api receives binary WebSocket frames and appends them to `Keys.audio(id)`.

## Caption streams

One Valkey stream per session and language: `cf:s:{id}:captions:{lang}`. Each entry has a single field: `c` (Caption JSON) or `e` (SessionEvent JSON).

Rules:

1. `seq` grows by 1 per FINAL.
2. A PARTIAL with seq N replaces the previous PARTIAL with seq N.
3. A FINAL with seq N replaces PARTIAL N and never changes again.
4. Target-language streams carry FINALs only; `seq == source_seq`, pointing to the source FINAL. A failed translation leaves a gap.
5. Viewers render every FINAL plus at most one current PARTIAL.
6. `t0`/`t1` are seconds since the session's audio started. Exports use them directly.

Example:

```json
{"session_id":"sala-a","lang":"es","kind":"final","seq":42,
 "text":"Kubernetes escala los pods automáticamente.",
 "t0":81.2,"t1":84.9,"source_seq":42,"emitted_at":1790000000.12}
```

## Valkey keys

| Key | Type | Written by | Content |
|---|---|---|---|
| `cf:sessions` | SET | api | session ids |
| `cf:s:{id}:config` | STRING | api | `SessionConfig` JSON |
| `cf:s:{id}:state` | STRING | worker | `SessionState` JSON |
| `cf:s:{id}:lease` | STRING, PX 10 s | worker | worker id (`SET NX`, renewed every 3 s) |
| `cf:s:{id}:audio` | STREAM | api | field `pcm`, browser sources only |
| `cf:s:{id}:captions:{lang}` | STREAM | worker | captions and session events |

Streams are capped with `XADD MAXLEN ~` (captions 50 000, audio 3 000).

## HTTP API (behind nginx, prefix `/api`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness + Valkey ping |
| GET | `/api/sessions` | list configs + states |
| POST | `/api/sessions` | create from `SessionConfig` |
| GET | `/api/sessions/{id}` | config + state |
| POST | `/api/sessions/{id}/stop` | mark ENDED; worker releases the lease |
| POST | `/api/sessions/{id}/start` | (re)start an ENDED or ERROR session: back to WAITING |
| GET | `/api/sessions/{id}/captions/{lang}/stream` | SSE (see below) |
| GET | `/api/sessions/{id}/export.{srt,vtt,txt}?lang=es` | FINALs formatted as a file |
| WS | `/api/sessions/{id}/ingest` | binary PCM frames, browser sources only (not implemented yet) |

### SSE

- `event: caption`, `id: <valkey stream id>`, `data: <Caption JSON>`
- `event: session`, `id: <valkey stream id>`, `data: <SessionEvent JSON>`
- Keep-alive comment every 15 s.
- Resume: the browser sends `Last-Event-ID` automatically on reconnect; `?from=<id>` does the same explicitly. Without either, the api replays the last 20 FINALs and then goes live.
- Fan-out: each api replica runs one `XREAD` loop per active (session, lang) and pushes to its local subscribers. Valkey load scales with sessions × langs × replicas, never with viewers.

## AI providers

Worker code depends only on the `Transcriber` and `Translator` protocols. Adapters raise `ProviderError(retryable=...)`.

| Env var | Default | Notes |
|---|---|---|
| `ASR_PROVIDER` | `whispercpp` | `whispercpp`, `replay` (fake, no model), `gemini` (planned) |
| `ASR_URL` | `http://host.docker.internal:8081` | whisper-server base URL |
| `MT_PROVIDER` | `openai_compat` | covers Ollama, vLLM, llama.cpp, LM Studio; `replay` (fake), `gemini` (planned) |
| `MT_URL` | `http://host.docker.internal:11434/v1` | OpenAI-compatible base URL |
| `MT_MODEL` | `gemma4:e4b` | any model the MT server knows |
| `MT_API_KEY` | empty | only for hosted providers |
| `VALKEY_URL` | `redis://valkey:6379/0` | |
| `REPLAY_ASR_LATENCY_MS` | `600` | replay ASR: simulated latency per call (±20%) |
| `REPLAY_MT_LATENCY_MS` | `1200` | replay MT: simulated latency per call (±20%) |
| `REPLAY_TRANSCRIPT` | built-in text | replay ASR: text file to read words from |
| `SESSIONS_FILE` | `config/sessions.yml` | sessions created at api startup |
| `GLOSSARY_FILE` | `/app/config/glossary.yml` | |