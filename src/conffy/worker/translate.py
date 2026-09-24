"""
Translation stage for one session.

Receives the source-language captions, translates FINALs only, and emits one
FINAL per target language with `source_seq` pointing at the original.

It runs on its own queue so a slow translator never delays transcription.
Captions are translated in order (context stays coherent); target languages
of the same caption are translated concurrently.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from conffy.contracts import (
    Caption,
    CaptionKind,
    LangCode,
    ProviderError,
    TranslationRequest,
    Translator,
)

Emit = Callable[[Caption], Awaitable[None]]


@dataclass
class TranslationStats:
    done: int = 0
    errors: int = 0
    last_mt_ms: float = 0.0
    mt_ms_total: float = 0.0
    last_error: str | None = None

    @property
    def avg_mt_ms(self) -> float:
        return self.mt_ms_total / self.done if self.done else 0.0


_CLOSE = object()


class TranslationStage:
    def __init__(
        self,
        translator: Translator,
        emit: Emit,
        *,
        source_lang: LangCode,
        target_langs: tuple[LangCode, ...],
        glossaries: Mapping[LangCode, Mapping[str, str]] | None = None,
        context_size: int = 3,
        retries: int = 1,
    ) -> None:
        self._mt = translator
        self._emit = emit
        self._src = source_lang
        self._targets = target_langs
        self._glossaries = glossaries or {}
        self._context: deque[str] = deque(maxlen=context_size)
        self._retries = retries
        self._queue: asyncio.Queue[Caption | object] = asyncio.Queue()
        self.stats = TranslationStats()

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    def submit(self, caption: Caption) -> None:
        """Non-blocking. Call with every source caption; partials are ignored."""
        if caption.kind is CaptionKind.FINAL and caption.lang == self._src and self._targets:
            self._queue.put_nowait(caption)

    def close(self) -> None:
        """No more captions: run() returns after draining the queue."""
        self._queue.put_nowait(_CLOSE)

    async def run(self) -> None:
        while (item := await self._queue.get()) is not _CLOSE:
            assert isinstance(item, Caption)
            context = tuple(self._context)
            await asyncio.gather(*(self._one(item, lang, context) for lang in self._targets))
            self._context.append(item.text)

    async def _one(self, cap: Caption, lang: LangCode, context: tuple[str, ...]) -> None:
        req = TranslationRequest(
            text=cap.text,
            source_lang=self._src,
            target_lang=lang,
            context=context,
            glossary=dict(self._glossaries.get(lang, {})),
        )
        for attempt in range(self._retries + 1):
            started = time.perf_counter()
            try:
                text = await self._mt.translate(req)
            except Exception as e:  # never kill the session over one sentence
                retryable = isinstance(e, ProviderError) and e.retryable
                if retryable and attempt < self._retries:
                    continue
                self.stats.errors += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
                return
            ms = (time.perf_counter() - started) * 1000
            self.stats.done += 1
            self.stats.last_mt_ms = ms
            self.stats.mt_ms_total += ms
            await self._emit(
                Caption(
                    session_id=cap.session_id,
                    lang=lang,
                    kind=CaptionKind.FINAL,
                    seq=cap.seq,
                    text=text,
                    t0=cap.t0,
                    t1=cap.t1,
                    source_seq=cap.seq,
                )
            )
            return
