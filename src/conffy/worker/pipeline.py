"""
ASR pipeline for one session: PCM chunks in, Caption objects out.

Two concurrent tasks:
  * reader: re-frames PCM into 32 ms frames, runs the segmenter (cheap, CPU) and
    enqueues finished utterances. It never waits on ASR, so audio is never lost
    when the model is slow.
  * asr loop: transcribes finished utterances first (FINAL captions). When
    idle, it transcribes the utterance in progress (PARTIAL captions), at most
    once per max(partial_every_s, partial_asr_ratio x last ASR latency).
    Partials are skipped, never queued, so a slow model degrades gracefully
    to finals only.

Captions follow docs/CONTRACTS.md: PARTIAL N is replaced by FINAL N.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from conffy.contracts import (
    Caption,
    CaptionKind,
    LangCode,
    TranscribeOptions,
    Transcriber,
)
from conffy.worker.segmenter import FRAME_BYTES, Segmenter, Utterance

Emit = Callable[[Caption], Awaitable[None]]

# whisper.cpp markers like [BLANK_AUDIO], (music), [Applause]
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")
# classic Whisper hallucinations on near-silence; only dropped for short utterances
_HALLUCINATIONS = {
    "you", "thank you.", "thanks for watching!", "thank you for watching.",
    "gracias.", "gracias por ver el video.", "¡gracias por ver!", "subtítulos realizados por la comunidad de amara.org",
}


def clean_text(text: str) -> str:
    return " ".join(_NON_SPEECH.sub(" ", text).split())


@dataclass(frozen=True)
class PipelineConfig:
    session_id: str
    language: LangCode | None = "en"
    partial_every_s: float = 1.0  # minimum interval between partials; 0 disables them
    partial_asr_ratio: float = 2.0  # interval >= ratio x last ASR latency (caps partials at 50% of ASR time)
    min_partial_audio_s: float = 0.8
    prompt_chars: int = 200  # recent committed text passed as ASR prompt
    base_prompt: str | None = None  # e.g. glossary terms


@dataclass
class PipelineStats:
    finals: int = 0
    partials: int = 0
    dropped: int = 0
    last_asr_ms: float = 0.0
    asr_ms_total: float = 0.0
    last_lag_s: float = 0.0  # audio clock minus end of the last final's audio
    max_lag_s: float = 0.0
    errors: int = 0
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=5))

    @property
    def avg_asr_ms(self) -> float:
        n = self.finals + self.partials
        return self.asr_ms_total / n if n else 0.0


_EOF = object()


class AsrPipeline:
    def __init__(
        self,
        transcriber: Transcriber,
        segmenter: Segmenter,
        emit: Emit,
        cfg: PipelineConfig,
    ) -> None:
        self._asr = transcriber
        self._seg = segmenter
        self._emit = emit
        self.cfg = cfg
        self.stats = PipelineStats()
        self._queue: asyncio.Queue[Utterance | object] = asyncio.Queue()
        self._next_seq = 0
        self._committed: deque[str] = deque(maxlen=20)

    # ------------------------------------------------------------------ public
    async def run(self, pcm: AsyncIterator[bytes]) -> None:
        reader = asyncio.create_task(self._read(pcm), name=f"reader:{self.cfg.session_id}")
        try:
            await self._asr_loop()
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if reader.exception():  # surface ffmpeg / source errors
            raise reader.exception()  # type: ignore[misc]

    # ------------------------------------------------------------------ reader
    async def _read(self, pcm: AsyncIterator[bytes]) -> None:
        pending = bytearray()
        try:
            async for chunk in pcm:
                pending += chunk
                while len(pending) >= FRAME_BYTES:
                    frame = bytes(pending[:FRAME_BYTES])
                    del pending[:FRAME_BYTES]
                    if utt := self._seg.feed(frame):
                        self._queue.put_nowait(utt)
                await asyncio.sleep(0)  # let the ASR loop run between chunks
            if utt := self._seg.flush():
                self._queue.put_nowait(utt)
        finally:
            self._queue.put_nowait(_EOF)

    # ------------------------------------------------------------------ asr
    async def _asr_loop(self) -> None:
        last_partial_at = 0.0
        last_partial_bytes = 0
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                item = None

            if item is _EOF:
                return
            if isinstance(item, Utterance):
                await self._final(item)
                last_partial_bytes = 0
                continue

            if self.cfg.partial_every_s <= 0:
                continue
            snap = self._seg.snapshot()
            now = time.monotonic()
            if (
                snap is not None
                and snap.duration >= self.cfg.min_partial_audio_s
                and now - last_partial_at >= self._partial_interval()
                and len(snap.pcm) != last_partial_bytes
            ):
                last_partial_at = now
                last_partial_bytes = len(snap.pcm)
                # utterances already queued will take the seqs before this one
                await self._partial(snap, seq=self._next_seq + self._queue.qsize())

    async def _transcribe(self, utt: Utterance) -> str | None:
        prompt_parts = [p for p in (self.cfg.base_prompt, self._recent_text()) if p]
        opts = TranscribeOptions(language=self.cfg.language, prompt=" ".join(prompt_parts) or None)
        started = time.perf_counter()
        try:
            transcript = await self._asr.transcribe(utt.pcm, opts)
        except Exception as e:  # ProviderError or anything unexpected: never kill the session
            self.stats.errors += 1
            self.stats.recent.append(f"{type(e).__name__}: {e}")
            return None
        ms = (time.perf_counter() - started) * 1000
        self.stats.last_asr_ms = ms
        self.stats.asr_ms_total += ms
        text = clean_text(transcript.text)
        if utt.duration < 2.0 and text.lower() in _HALLUCINATIONS:
            return ""
        return text

    async def _final(self, utt: Utterance) -> None:
        text = await self._transcribe(utt)
        if not text:
            self.stats.dropped += 1
            return  # seq not consumed: the next caption replaces any stale partial
        seq = self._next_seq
        self._next_seq += 1
        self._committed.append(text)
        self.stats.finals += 1
        self.stats.last_lag_s = max(0.0, self._seg.clock - utt.t1)
        self.stats.max_lag_s = max(self.stats.max_lag_s, self.stats.last_lag_s)
        await self._emit(self._caption(CaptionKind.FINAL, seq, text, utt))

    async def _partial(self, utt: Utterance, seq: int) -> None:
        text = await self._transcribe(utt)
        if not text:
            return
        self.stats.partials += 1
        await self._emit(self._caption(CaptionKind.PARTIAL, seq, text, utt))

    # ------------------------------------------------------------------ helpers
    def _partial_interval(self) -> float:
        """Adapts to the model: a slow ASR gets fewer partials, so finals never wait long."""
        adaptive = self.cfg.partial_asr_ratio * self.stats.last_asr_ms / 1000
        return max(self.cfg.partial_every_s, adaptive)

    def _recent_text(self) -> str:
        text = " ".join(self._committed)
        return text[-self.cfg.prompt_chars :] if self.cfg.prompt_chars > 0 else ""

    def _caption(self, kind: CaptionKind, seq: int, text: str, utt: Utterance) -> Caption:
        return Caption(
            session_id=self.cfg.session_id,
            lang=self.cfg.language or "und",
            kind=kind,
            seq=seq,
            text=text,
            t0=utt.t0,
            t1=utt.t1,
        )
