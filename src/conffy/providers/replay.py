"""
Replay providers: fake ASR and MT that need no models.

Used for load tests (simulate 15+ talks without a GPU) and for trying conffy
on any machine. They behave like the real ones where it matters:

* ASR returns text proportional to the audio length (~2.5 words/s), taken from
  a transcript that loops. Partials of an utterance are prefixes of its final,
  like real Whisper output, so the viewer shows the same partial/final flow.
* Both sleep a configurable latency (with +/-20% jitter) to load the pipeline
  the way real models do.
* MT prefixes the text with the target language, e.g. "[es] ...", so nobody
  mistakes it for a real translation.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from pathlib import Path

from conffy.contracts import (
    BYTES_PER_SECOND,
    TranscribeOptions,
    Transcript,
    TranslationRequest,
)

WORDS_PER_SECOND = 2.5

DEFAULT_TRANSCRIPT = """\
Welcome everyone, and thanks for coming to this talk.
Today we are going to talk about how we run live subtitles for a whole conference.
The idea is simple: every stage sends its audio, and every attendee reads the talk on their phone.
We split the audio into sentences using voice activity detection.
Each sentence is transcribed by Whisper and then translated by a small language model.
Captions go into a stream, one per talk and language.
The API reads each stream once and pushes it to every connected browser.
That is why the cost grows with the number of talks, not with the number of viewers.
If a worker dies in the middle of a talk, another one takes over in a few seconds.
Viewers reconnect automatically and continue exactly where they left off.
Let's look at the numbers we measured on a single machine.
"""


def _jitter(ms: float) -> float:
    return max(0.0, ms * random.uniform(0.8, 1.2)) / 1000


class ReplayTranscriber:
    name = "replay"

    def __init__(self, latency_ms: float = 600, transcript_path: str | None = None) -> None:
        text = DEFAULT_TRANSCRIPT
        if transcript_path and Path(transcript_path).is_file():
            text = Path(transcript_path).read_text(encoding="utf-8")
        self._words = text.split() or ["..."]
        self._latency_ms = latency_ms
        self._cursor = 0  # first word of the current utterance
        self._utt_key: bytes | None = None
        self._utt_words = 0  # words the current utterance has shown so far

    async def transcribe(self, pcm: bytes, opts: TranscribeOptions) -> Transcript:
        await asyncio.sleep(_jitter(self._latency_ms))
        # Partials and the final of one utterance share the same first half second
        # of audio (partials are >= 0.8 s): same key -> same utterance -> same words.
        key = hashlib.blake2b(pcm[: BYTES_PER_SECOND // 2], digest_size=8).digest()
        if key != self._utt_key:
            self._cursor = (self._cursor + self._utt_words) % len(self._words)
            self._utt_key, self._utt_words = key, 0
        n = max(1, round(len(pcm) / BYTES_PER_SECOND * WORDS_PER_SECOND))
        self._utt_words = n
        words = [self._words[(self._cursor + i) % len(self._words)] for i in range(n)]
        return Transcript(text=" ".join(words), language=opts.language)

    async def aclose(self) -> None:
        pass


class ReplayTranslator:
    name = "replay"

    def __init__(self, latency_ms: float = 1200) -> None:
        self._latency_ms = latency_ms

    async def translate(self, req: TranslationRequest) -> str:
        await asyncio.sleep(_jitter(self._latency_ms))
        return f"[{req.target_lang}] {req.text}"

    async def aclose(self) -> None:
        pass