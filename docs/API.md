# conffy HTTP API

The api is stateless: it reads and writes Valkey only, so any number of
replicas can run behind nginx. Every endpoint lives under `/api` and is
reached through nginx at `http://<host>:8080/api/...`.

- Requests and responses are JSON (`Content-Type: application/json`),
  except the live stream (`text/event-stream`) and the exports (text files).
- Models are defined in `src/conffy/contracts.py`; `docs/CONTRACTS.md` has the
  internal details (Valkey keys, worker behavior).
- Errors follow FastAPI's format: `{"detail": "..."}` for 404 and 409, and a
  list of field errors for 422 (validation).

## Endpoints at a glance

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/health` | Liveness check: pings Valkey |
| `GET` | `/api/sessions` | List every talk with its live state |
| `POST` | `/api/sessions` | Create a talk |
| `GET` | `/api/sessions/{id}` | One talk with its live state |
| `POST` | `/api/sessions/{id}/start` | (Re)start a stopped or failed talk |
| `POST` | `/api/sessions/{id}/stop` | Stop a live talk |
| `GET` | `/api/sessions/{id}/captions/{lang}/stream` | Live captions (Server-Sent Events) |
| `GET` | `/api/sessions/{id}/export.{srt,vtt,txt}` | Full transcript as a subtitle or text file |

The production panel adds more endpoints; see
[Production panel endpoints](#production-panel-endpoints).

---

## Data models

### SessionConfig: a talk

```json
{
  "id": "sala-a",
  "title": "KPIs, presión y burnout",
  "source": {"kind": "file", "uri": "samples/charla.mp3", "realtime": true, "loop": true},
  "source_lang": "en",
  "target_langs": ["es"],
  "glossary": "nerdearla"
}
```

| Field | Type | Rules |
|---|---|---|
| `id` | string | lowercase slug `a-z 0-9 -`, 2–63 chars, starts with a letter or digit. Used in URLs and as the OBS stream key. |
| `title` | string | shown to the audience |
| `source.kind` | `file` \| `url` \| `browser` | `browser` is reserved; not implemented yet |
| `source.uri` | string | `file`: path relative to the project root (`/app` in containers). `url`: anything ffmpeg reads (`rtmp://`, `srt://`, `rtsp://`, HLS, `http(s)://`). |
| `source.realtime` | bool, default `true` | `file` only: read at 1× speed like a live talk. `false` processes as fast as possible, which is useful to subtitle a recording. |
| `source.loop` | bool, default `false` | `file` only: restart at the end (demos) |
| `source_lang` | ISO 639-1, default `en` | the spoken language |
| `target_langs` | list, default `["es"]` | translations; must not include `source_lang` |
| `glossary` | string \| null | a name in `config/glossary.yml` |

Each talk has one caption stream per language: `source_lang` plus every
target language.

### SessionState: live status (written by the worker)

```json
{
  "status": "live",
  "worker_id": "a1b2c3-7",
  "started_at": 1790000000.1,
  "updated_at": 1790000123.4,
  "last_caption_at": 1790000121.9,
  "audio_lag_s": 0.9,
  "asr_ms": 612.3,
  "mt_ms": 1204.8,
  "error": null
}
```

- **`status`** is one of:
  - `waiting`: created, no worker yet
  - `live`: a worker is producing captions
  - `ended`: the source finished, or someone stopped the talk
  - `error`: the source failed; see `error`
- **Metrics:** `audio_lag_s`, `asr_ms` and `mt_ms` are refreshed by the
  worker every ~3 s. Times are Unix epoch seconds.

### SessionView: what the session endpoints return

```json
{"config": { ...SessionConfig }, "state": { ...SessionState }}
```

### Caption

```json
{
  "session_id": "sala-a", "lang": "es", "kind": "final", "seq": 42,
  "text": "Kubernetes escala los pods automáticamente.",
  "t0": 81.2, "t1": 84.9, "source_seq": 42, "emitted_at": 1790000084.9
}
```

- **`kind`** is `partial` (provisional, may change) or `final` (never
  changes). **A `final` with `seq` N replaces the `partial` with `seq` N.**
- **`t0` / `t1`** are seconds since the talk's audio started.
- **`source_seq`** is set in translations only: the `seq` of the original sentence.
- **Target-language streams carry finals only**, with `seq == source_seq`. A
  translation that fails leaves a gap in the numbering.

### SessionEvent

```json
{"session_id": "sala-a", "kind": "ended", "detail": null, "emitted_at": 1790000500.0}
```

`kind` is `ended` or `error` (with a message in `detail`).

---

## Health

### `GET /api/health`

Checks that the api is up and can reach Valkey. It also reports this
replica's SSE viewers per stream.

```bash
curl localhost:8080/api/health
```

```json
{"status": "ok", "viewers": {"sala-a/es": 88, "sala-a/en": 12}, "time": 1790000000.0}
```

`viewers` covers only the replica that answered. For totals, use
`/api/overview`.

## Talks

### `GET /api/sessions`

Every talk with its state, sorted by id. The audience app polls this every 5 s.

```bash
curl localhost:8080/api/sessions
```

→ `200` with `[SessionView, ...]`

### `POST /api/sessions`

Creates a talk. A free worker picks it up within ~2 s.

```bash
curl -X POST localhost:8080/api/sessions -H 'Content-Type: application/json' -d '{
  "id": "sala-yt", "title": "Charla desde YouTube",
  "source": {"kind": "url", "uri": "rtmp://mediamtx:1935/sala-yt"},
  "source_lang": "en", "target_langs": ["es"], "glossary": "nerdearla"}'
```

Or with the Makefile: `make room ID=sala-yt FROM=en TO=es TITLE="Charla desde YouTube"`.

| Status | When |
|---|---|
| `201` | created; returns `SessionView` |
| `409` | a talk with that id exists |
| `422` | invalid fields (bad id, target includes the source language, missing uri, …) |

Talks created through the API live in Valkey. `config/sessions.yml` is only
the seed, loaded when the api starts (existing ids are never overwritten).

For RTMP sources, **start streaming before creating the talk**. If the worker
connects first, ffmpeg fails and the talk goes to `error`; start it again
once the stream is up.

### `GET /api/sessions/{id}`

→ `200` with `SessionView`, or `404`.

### `POST /api/sessions/{id}/stop`

Marks the talk `ended`. Its worker notices within ~3 s, flushes the pending
translations, publishes an `ended` event to every caption stream and becomes
free. Captions stay available for export.

```bash
curl -X POST localhost:8080/api/sessions/sala-a/stop      # or: make stop ID=sala-a
```

→ `200` with `SessionState`, or `404`.

### `POST /api/sessions/{id}/start`

Puts an `ended` or `error` talk back to `waiting`, so any free worker picks it
up. Numbering and the timeline continue after the last caption. It has no
effect on a `live` talk.

```bash
curl -X POST localhost:8080/api/sessions/sala-a/start     # or: make start ID=sala-a
```

→ `200` with `SessionState`, or `404`.

## Live captions

### `GET /api/sessions/{id}/captions/{lang}/stream`

A Server-Sent Events stream of one talk in one language. It stays open;
nginx allows up to 1 hour between messages, and a keep-alive is sent every
15 s.

```bash
curl -N localhost:8080/api/sessions/sala-a/captions/es/stream
```

```
retry: 2000

id: 1790000084912-0
event: caption
data: {"session_id":"sala-a","lang":"es","kind":"final","seq":42,"text":"...","t0":81.2,"t1":84.9,"source_seq":42,"emitted_at":1790000084.9}

: keep-alive

id: 1790000500001-0
event: session
data: {"session_id":"sala-a","kind":"ended","detail":null,"emitted_at":1790000500.0}
```

- **On connect, you get the last 20 finals** (context for late arrivals),
  then the stream goes live.
- **Resuming:** browsers send `Last-Event-ID` automatically when they
  reconnect, and the stream continues exactly after that event, with no gaps
  and no duplicates. Other clients can do the same with the header or with
  `?from=<id>`.
- **Rendering rule:** keep every `final` (keyed by `seq`) and at most one
  `partial`. Drop the partial when a final with the same or a higher `seq`
  arrives.
- **Errors:** `404` if the talk doesn't exist or has no captions in `lang`.
- **Slow clients:** a client more than 1,000 events behind is disconnected. It
  reconnects and resumes.

Minimal browser client:

```js
const es = new EventSource("/api/sessions/sala-a/captions/es/stream");
es.addEventListener("caption", (e) => console.log(JSON.parse(e.data)));
es.addEventListener("session", (e) => console.log("talk", JSON.parse(e.data).kind));
```

## Exports

### `GET /api/sessions/{id}/export.{fmt}?lang={lang}`

The full transcript of one language, built from the stored finals. It works
while the talk is live, and after it ends.

| `fmt` | Content | Notes |
|---|---|---|
| `srt` | SubRip subtitles | cues of ≤ 2 lines × ~42 chars; long sentences are split and their time shared |
| `vtt` | WebVTT subtitles | same cues; upload it to YouTube as a subtitle track |
| `txt` | plain text | one sentence per line |

`lang` defaults to the talk's `source_lang`.

```bash
curl -OJ "localhost:8080/api/sessions/sala-a/export.srt?lang=es"   # saves sala-a-es.srt
```

→ `200` with the file (`Content-Disposition: attachment`), or `404` for an
unknown talk, language or format.

---

## Production panel endpoints

These are added by the production panel work (`web/admin.html`). **Check them
against `src/conffy/api/main.py`** in your version: if a stage of the panel is
not merged, its endpoints don't exist.

| Method | Path | Stage | What it does |
|---|---|---|---|
| `GET` | `/api/overview` | 1 | Everything the panel shows in one call: talks with state and viewers per language, workers (from heartbeats), total viewers across api replicas, and the number of live api replicas |
| `DELETE` | `/api/sessions/{id}?purge=false` | 2 | Delete a talk that is not live (`409` if it is). With `purge=true` its captions are deleted too. |
| `GET` | `/api/sources` | 2 | Options for the "new talk" form: audio files under `samples/`, glossary names, languages |

With `ADMIN_TOKEN` set (stage 2), the endpoints that change state require
`Authorization: Bearer <token>`: create, start, stop and delete. Without the
token they return `401`. Reads stay open, since the audience app needs them.