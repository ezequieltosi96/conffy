"""Transcriber adapter for whisper.cpp's `whisper-server` (POST /inference).

`detailed=False` (default) asks for plain `json`: text only. Measured on an
M4 Pro with large-v3-turbo, `verbose_json` (segment and word timestamps)
roughly doubles the latency of every call, so only enable it when needed.
"""

from __future__ import annotations

from typing import Any

import httpx

from conffy.contracts import (
    ProviderError,
    Segment,
    TranscribeOptions,
    Transcript,
    Word,
)
from conffy.providers._audio import pcm_to_wav


class WhisperCppTranscriber:
    name = "whispercpp"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        detailed: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._format = "verbose_json" if detailed else "json"

    async def transcribe(self, pcm: bytes, opts: TranscribeOptions) -> Transcript:
        data = {"response_format": self._format, "temperature": "0.0"}
        if opts.language:
            data["language"] = opts.language
        if opts.prompt:
            data["prompt"] = opts.prompt
        files = {"file": ("audio.wav", pcm_to_wav(pcm), "audio/wav")}

        try:
            resp = await self._client.post("/inference", data=data, files=files)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(self.name, f"request failed: {e!r}", retryable=True) from e

        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(self.name, f"HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False)

        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError(self.name, "response is not JSON", retryable=False) from e
        if isinstance(body, dict) and "error" in body:
            raise ProviderError(self.name, str(body["error"]), retryable=False)
        return _parse(body)

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse(body: dict[str, Any]) -> Transcript:
    """Defensive parse: whisper-server fields vary a bit across versions."""
    segments = []
    for s in body.get("segments") or []:
        words = tuple(
            Word(
                text=str(w.get("word", "")).strip(),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
                prob=w.get("probability"),
            )
            for w in (s.get("words") or [])
            if str(w.get("word", "")).strip()
        )
        segments.append(
            Segment(
                text=str(s.get("text", "")).strip(),
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                words=words,
            )
        )
    text = str(body.get("text", "")).strip() or " ".join(s.text for s in segments).strip()
    lang = body.get("language")
    return Transcript(
        text=text,
        language=lang if isinstance(lang, str) and len(lang) == 2 else None,
        segments=tuple(segments),
    )
