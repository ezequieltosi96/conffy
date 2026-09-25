# Swapping AI models and providers

conffy uses two AI steps:

- **ASR** (speech → text)
- **MT** (text → text in another language)

The worker only knows two interfaces, `Transcriber` and `Translator` (in
`src/conffy/contracts.py`). Each provider is an *adapter* that implements one
of them, and `src/conffy/providers/registry.py` picks the adapters from
environment variables. Swapping a model is a configuration change. Adding a
provider is one new file.

**Short answer for Gemini:**

- **Translation with Gemini works today**, with configuration only.
- **Transcription with Gemini needs one new adapter** (~100 lines, spec below).
  After that, a deployment can run with **no Whisper and no GPU at all**.
- Gemini's **Live Translate** API goes further (streaming, even translated
  voice), but it needs a different pipeline shape. It is a roadmap item.

## Current adapters

| Step | `*_PROVIDER` | What it talks to | Notes |
|---|---|---|---|
| ASR | `whispercpp` | whisper.cpp `whisper-server` (`ASR_URL`) | default; local, GPU on the host |
| ASR | `replay` | nothing (fake text) | load tests and demos without models |
| MT | `openai_compat` | any OpenAI-compatible `/chat/completions` (`MT_URL`, `MT_MODEL`, `MT_API_KEY`) | default, with Ollama + Gemma 4; also vLLM, llama.cpp, LM Studio, **Gemini**, OpenAI and others |
| MT | `replay` | nothing (echoes `[es] …`) | load tests and demos |

The environment variables are set in `.env` for docker compose (copy
`.env.example`), or directly in the shell for the CLI.

| Variable | Default | Used by |
|---|---|---|
| `ASR_PROVIDER` | `whispercpp` | registry |
| `ASR_URL` | `http://host.docker.internal:8081` | whispercpp |
| `MT_PROVIDER` | `openai_compat` | registry |
| `MT_URL` | `http://host.docker.internal:11434/v1` | openai_compat |
| `MT_MODEL` | `gemma4:e4b` | openai_compat |
| `MT_API_KEY` | empty | openai_compat (sent as `Authorization: Bearer`) |
| `MT_REASONING_EFFORT` | `none` | openai_compat; see "Thinking models" |

Providers apply to the whole deployment: every worker uses the same ones.
Choosing a provider per talk is possible, but it needs a contract change (see
the roadmap at the end).

---

## Recipe 1: local models (default)

whisper.cpp and Ollama with Gemma 4 on the host. See the README quick start.
Variants:

- **Larger translation model:** `ollama pull gemma4:12b` and set
  `MT_MODEL=gemma4:12b`. Better quality, slower. On a Mac mini M4 Pro this
  reduces how many talks one machine can serve.
- **Another Whisper model:** `WHISPER_MODEL=large-v3 ./scripts/setup-host.sh`
  and the same variable for `run-host.sh`. Any `ggml-*.bin` name from the
  whisper.cpp model list works (`small`, `medium`, `large-v3`,
  `large-v3-turbo-q5_0`, …). Smaller is faster and less accurate.
- **Another Ollama model:** any chat model Ollama serves: `MT_MODEL=<tag>`.

## Recipe 2: a GPU server (vLLM, llama.cpp server, LM Studio)

For translation, only the URL and model change:

```bash
MT_URL=http://gpu-box:8000/v1
MT_MODEL=google/gemma-4-e4b-it    # whatever name the server exposes
```

For transcription, point `ASR_URL` to a `whisper-server` on that machine.
Batched Whisper servers (e.g. faster-whisper based) that expose
OpenAI-style `/v1/audio/transcriptions` need a small adapter; see
"Writing a new adapter".

## Recipe 3: Gemini for translation (works today)

Gemini exposes an OpenAI-compatible endpoint, so the existing `openai_compat`
adapter works unchanged. Transcription stays local with Whisper, and the
translation goes to Google.

`.env`:

```bash
MT_PROVIDER=openai_compat
MT_URL=https://generativelanguage.googleapis.com/v1beta/openai
MT_API_KEY=<your Gemini API key>
MT_MODEL=<a current Gemini Flash or Flash-Lite model>
MT_REASONING_EFFORT=minimal       # see "Thinking models"
```

**Choosing the model.** Use a *Flash* or *Flash-Lite* model: translation of
short sentences needs speed, not deep reasoning. Model names change often, so
check the Gemini models page. At the time of writing, Google's own examples use
`gemini-3.8-flash` and `gemini-3-flash-preview`.

**Check it before a live talk:**

```bash
curl https://generativelanguage.googleapis.com/v1beta/openai/chat/completions \
  -H "Authorization: Bearer $MT_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "'$MT_MODEL'", "reasoning_effort": "'$MT_REASONING_EFFORT'",
       "messages": [{"role": "user", "content": "Translate to Spanish: Let the saw do the work."}]}'
```

Then run a real clip through the CLI with the same variables exported, before
changing the stack:

```bash
export MT_URL=... MT_API_KEY=... MT_MODEL=... MT_REASONING_EFFORT=minimal
uv run python -m conffy.worker.cli samples/charla.mp3 --glossary nerdearla
```

The CLI and the workers read the same variables (`MT_URL`, `MT_MODEL`,
`MT_API_KEY`, `MT_REASONING_EFFORT`).

### Thinking models

Many recent models "think" before answering. For subtitles that is wasted time,
and it can use up the token budget so the answer comes back **empty**. conffy
sends `reasoning_effort` to control it:

| Server / model | Recommended `MT_REASONING_EFFORT` |
|---|---|
| Ollama (Gemma 4 and other thinking models) | `none` (default): disables thinking |
| Gemini 2.5 Flash / Flash-Lite | `none`: disables thinking |
| Gemini 3.x Flash / Flash-Lite | `minimal`: thinking can't be turned off on Gemini 3; this is the lowest level |
| A server that rejects the field | anything: the adapter drops it after the first 400 |
| Don't send the field at all | empty string: `MT_REASONING_EFFORT=` |

If translations fail with "empty translation", or with "model spent its tokens
reasoning", thinking is the cause: lower the effort or pick a non-thinking
model.

## Recipe 4: Gemini for transcription too (needs one adapter)

Gemini understands audio. Its OpenAI-compatible endpoint accepts WAV audio
inline in a chat message (`input_audio`), so conffy can send each utterance to
Gemini instead of Whisper. With Recipe 3 for translation, the deployment needs
**no local models and no GPU**. Workers become small CPU containers that could
run anywhere.

Everything else stays the same: the voice detector still cuts the audio into
utterances, partials and finals work as today, and the glossary still biases
recognition, sent as part of the prompt.

### Adapter spec: `providers/asr_openai_chat.py`

This spec is for whoever implements it.

- **Class:** `OpenAIChatTranscriber`, implementing `Transcriber`.
- **Registry:** registered as `ASR_PROVIDER=openai_chat`.
- **Settings:** `ASR_URL` (the OpenAI-compatible base URL), `ASR_MODEL` and
  `ASR_API_KEY`. `ASR_MODEL` and `ASR_API_KEY` are new settings.
- **Request:** `POST {ASR_URL}/chat/completions` with
  `temperature: 0`, `reasoning_effort` (same rules as MT), and one user message
  with two parts:
  - text: *"Transcribe this audio verbatim in {language name}. Output only
    the transcript, with punctuation. If there is no speech, output nothing.
    Spelling hints: {opts.prompt}"*
  - `{"type": "input_audio", "input_audio": {"data": <base64 WAV>, "format": "wav"}}`

  Reuse `providers/_audio.pcm_to_wav`.
- **Response:** `choices[0].message.content` → `Transcript(text=...)`. There
  are no word timestamps; the pipeline doesn't need them.
- **Errors:** the same mapping as `mt_openai_compat` (429/5xx retryable,
  other 4xx not). An empty result is fine: it means silence, and the pipeline
  drops it.
- **Tests:** respx tests for the request shape, `input_audio`, error mapping
  and the empty result.

### Things to know before using Gemini for ASR

- **Latency:** every utterance is a network round trip plus model time. Expect
  roughly 1–2 s per call instead of ~0.6 s local, so subtitles arrive a bit
  later. Measure it with `make watch`.
- **Partials multiply the calls.** Each partial re-sends the utterance so far.
  With a remote model, consider fewer partials. Today the interval adapts to
  twice the ASR latency. A `PARTIAL_EVERY_S` setting, 0 to disable partials
  entirely, would be a small addition to `PipelineConfig` and the worker.
- **Rate limits:** one talk makes roughly 20–30 ASR calls and ~15 MT calls
  per minute. For 15 talks that is ~450 ASR + ~225 MT requests per minute: use
  a paid tier and check the project's quotas. On 429, MT retries once. ASR
  drops that partial or final, and the talk continues.
- **Cost math:** audio is billed as input tokens (Gemini documents ~32 tokens
  per second of audio). Finals send each second once, and partials re-send
  parts of it. Budget 2–3× the talk length in audio. Check current prices on
  Google's pricing page.
- **Privacy:** the talk's audio leaves the venue. Make sure speakers and the
  organizers agree.
- **Connectivity:** the venue's internet becomes critical. A local deployment
  keeps working offline.

## Recipe 5: Gemini Live Translate (future: a different pipeline)

Gemini's Live API streams audio over a WebSocket and returns transcripts
continuously. Its **Live Translate** model does speech-to-speech translation
between 70+ languages, with transcripts of both the input and the output. That
would replace the voice detector, ASR and MT with one streaming session per
talk and target language. It could have lower latency, and it could even give
**translated audio**, for example as a second audio channel for the venue.

It does not fit the current `Transcriber`/`Translator` interfaces, which work
on whole utterances. It would need:

- a new `StreamingProvider` port that takes PCM chunks and emits partial and
  final captions directly
- a worker mode that uses it instead of `AsrPipeline`

Everything downstream stays the same: Valkey streams, fan-out, web and
exports. It is on the roadmap (P2).

## Choosing

| | Local (1) | Hybrid: Whisper + Gemini MT (3) | Full Gemini (3 + 4) | Live Translate (5) |
|---|---|---|---|---|
| Needs GPU / Apple Silicon | yes | yes (ASR only) | **no** | no |
| Works offline | **yes** | no | no | no |
| Cost | hardware only | per token (text only, cheap) | per token (audio + text) | per session time |
| Subtitle latency | ~1 s / ~2 s | ~1 s / ~2–3 s | ~2 s / ~3 s (estimate) | lowest (estimate) |
| Ops effort at 15 stages | several machines or GPU servers | fewer GPUs | **stateless containers only** | stateless, but a new code path |
| Status | done | done (config only) | one adapter away | roadmap |

For Nerdearla:

- **With a Gemini account and reliable venue internet**, Full Gemini (3 + 4)
  is the simplest way to cover 15 stages: no GPUs to rent or to carry, only
  containers.
- **For resilience**, keep the local setup ready as a fallback. Switching
  back is one `.env` change and a worker restart.

## Writing a new adapter

1. Create `src/conffy/providers/<name>.py` with a class that has a `name`
   attribute and implements `transcribe(pcm, opts)` or `translate(req)`, plus
   `aclose()`.
2. Raise `ProviderError(provider, message, retryable=...)` on failures:
   - retryable for timeouts, 429 and 5xx
   - not retryable for bad requests
3. Register it in `providers/registry.py` under a new `*_PROVIDER` value. Add
   any new settings to `settings.py`, `.env.example`, the `environment:`
   block in `docker-compose.yml` (otherwise containers never see them), and
   `docs/CONTRACTS.md`.
4. Add respx tests in `tests/`: request shape, success, error mapping.
5. Verify with the CLI on a real clip, then with `make watch` in the stack.

Nothing else changes: the worker, API, web and exports don't know which
provider produced the text.

## Roadmap items related to providers

- `asr_openai_chat` adapter (Recipe 4): enables Full Gemini. *P1.*
- `PARTIAL_EVERY_S` setting, to reduce calls with remote ASR. *P1, small.*
- Provider per session (e.g. local for most talks, Gemini for overflow). This
  needs optional `asr_provider` / `mt_provider` fields in `SessionConfig`, and
  a registry that builds adapters per session. *P2.*
- Streaming providers and Gemini Live Translate (Recipe 5). *P2.*