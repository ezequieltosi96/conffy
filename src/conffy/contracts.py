"""
conffy · contracts
=====================

Single source of truth shared by the API and the worker.

Rules
-----
* Everything that crosses a process boundary (Redis, HTTP, SSE) is defined here.
* Provider adapters (``conffy.providers``) implement the Protocols below and
  depend on nothing else in the codebase.
* Changing this file is a design decision: update ``docs/CONTRACTS.md`` in the
  same commit.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# =============================================================================
# Audio format
# Every audio source is normalized (by ffmpeg or by the browser) to this format
# before touching VAD or ASR: raw PCM, signed 16-bit little-endian, 16 kHz, mono.
# =============================================================================

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes per sample (s16le)
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH  # 32_000

BROWSER_CHUNK_MS = 100  # browser pushes 100 ms frames = 3_200 bytes


def pcm_seconds(pcm: bytes) -> float:
    """Duration in seconds of a PCM buffer in the canonical format."""
    return len(pcm) / BYTES_PER_SECOND


# =============================================================================
# Enums
# =============================================================================

LangCode = str  # ISO 639-1: "en", "es", "pt"


class SourceKind(StrEnum):
    FILE = "file"  # path inside the container, e.g. /app/samples/talk.mp3
    URL = "url"  # anything ffmpeg reads: rtmp://, rtsp://, srt://, http(s)://, HLS
    BROWSER = "browser"  # PCM pushed by a browser over WebSocket into Keys.audio()


class SessionStatus(StrEnum):
    WAITING = "waiting"  # created, no worker has claimed it yet
    LIVE = "live"  # a worker holds the lease and is producing captions
    ENDED = "ended"  # source finished or session stopped
    ERROR = "error"  # unrecoverable failure, see SessionState.error


class CaptionKind(StrEnum):
    PARTIAL = "partial"  # provisional text, may change; source language only
    FINAL = "final"  # committed text, never changes


class SessionEventKind(StrEnum):
    ENDED = "ended"
    ERROR = "error"


# =============================================================================
# Session configuration and state
# =============================================================================

_SESSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_LANG_RE = re.compile(r"^[a-z]{2}$")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Frozen):
    kind: SourceKind
    uri: str | None = None
    realtime: bool = True  # FILE only: read at 1x speed (ffmpeg -re) to simulate a live talk
    loop: bool = False  # FILE only: restart when finished (demos and load tests)

    @model_validator(mode="after")
    def _check_uri(self) -> Source:
        if self.kind is SourceKind.BROWSER:
            if self.uri is not None:
                raise ValueError("browser sources take no uri")
        elif not self.uri:
            raise ValueError(f"{self.kind} sources require a uri")
        return self


class SessionConfig(_Frozen):
    """Immutable description of a talk. Stored as JSON in Keys.config()."""

    id: str
    title: str
    source: Source
    source_lang: LangCode = "en"
    target_langs: tuple[LangCode, ...] = ("es",)
    glossary: str | None = None  # glossary name in config/glossary.yml

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _SESSION_ID_RE.match(v):
            raise ValueError("id must be a lowercase slug: a-z, 0-9 and '-', 2-63 chars")
        return v

    @field_validator("source_lang")
    @classmethod
    def _check_lang(cls, v: str) -> str:
        if not _LANG_RE.match(v):
            raise ValueError("language must be an ISO 639-1 code like 'en'")
        return v

    @field_validator("target_langs")
    @classmethod
    def _check_targets(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for lang in v:
            if not _LANG_RE.match(lang):
                raise ValueError(f"invalid target language: {lang!r}")
        return tuple(dict.fromkeys(v))  # dedupe, keep order

    @model_validator(mode="after")
    def _source_not_in_targets(self) -> SessionConfig:
        if self.source_lang in self.target_langs:
            raise ValueError("target_langs must not include source_lang")
        return self

    @property
    def langs(self) -> tuple[LangCode, ...]:
        """Every caption stream this session produces."""
        return (self.source_lang, *self.target_langs)


class SessionState(BaseModel):
    """Mutable runtime state. Written by the worker, read by API and admin panel.
    Stored as JSON in Keys.state()."""

    model_config = ConfigDict(extra="forbid")

    status: SessionStatus = SessionStatus.WAITING
    worker_id: str | None = None
    started_at: float | None = None  # epoch seconds
    updated_at: float = Field(default_factory=time.time)
    last_caption_at: float | None = None
    audio_lag_s: float = 0.0  # how far the pipeline runs behind the audio clock
    asr_ms: float | None = None  # last ASR call latency
    mt_ms: float | None = None  # last translation latency
    error: str | None = None


# =============================================================================
# Stream messages (Redis streams -> SSE)
#
# Semantics per (session, lang) stream:
#   * seq grows by 1 per FINAL caption.
#   * A PARTIAL with seq N replaces any previous PARTIAL with seq N.
#   * A FINAL with seq N replaces PARTIAL N and is immutable.
#   * Viewers therefore render: all FINALs + at most one current PARTIAL.
#   * Target-language streams carry FINALs only (we never translate partials).
#   * In target-language streams seq == source_seq; a failed translation leaves a gap.
# =============================================================================


class Caption(_Frozen):
    session_id: str
    lang: LangCode
    kind: CaptionKind
    seq: int = Field(ge=0)
    text: str
    t0: float = Field(ge=0)  # seconds since the session's audio started
    t1: float = Field(ge=0)
    source_seq: int | None = None  # translations: seq of the source FINAL they translate
    emitted_at: float = Field(default_factory=time.time)  # for end-to-end latency metrics

    @model_validator(mode="after")
    def _check_times(self) -> Caption:
        if self.t1 < self.t0:
            raise ValueError("t1 must be >= t0")
        return self


class SessionEvent(_Frozen):
    """Control message published to every caption stream of a session."""

    session_id: str
    kind: SessionEventKind
    detail: str | None = None
    emitted_at: float = Field(default_factory=time.time)


StreamMessage = Caption | SessionEvent

_FIELD_CAPTION = "c"
_FIELD_EVENT = "e"


def encode_message(msg: StreamMessage) -> dict[str, str]:
    """Redis stream fields for XADD. One field, JSON payload, keyed by type."""
    field = _FIELD_CAPTION if isinstance(msg, Caption) else _FIELD_EVENT
    return {field: msg.model_dump_json()}


def decode_message(fields: Mapping[bytes | str, bytes | str]) -> StreamMessage:
    """Inverse of encode_message. Accepts bytes or str keys (decode_responses on or off)."""
    norm = {(k.decode() if isinstance(k, bytes) else k): v for k, v in fields.items()}
    if _FIELD_CAPTION in norm:
        return Caption.model_validate_json(norm[_FIELD_CAPTION])
    if _FIELD_EVENT in norm:
        return SessionEvent.model_validate_json(norm[_FIELD_EVENT])
    raise ValueError(f"unknown stream message fields: {sorted(norm)}")


# =============================================================================
# AI ports
# Adapters implement these. All times are relative to the start of the PCM
# buffer passed in; the worker converts them to session time.
# =============================================================================


class Word(_Frozen):
    text: str
    start: float
    end: float
    prob: float | None = None


class Segment(_Frozen):
    text: str
    start: float
    end: float
    words: tuple[Word, ...] = ()  # empty if the provider has no word timestamps


class Transcript(_Frozen):
    text: str
    language: LangCode | None = None  # detected or echoed language
    segments: tuple[Segment, ...] = ()


class TranscribeOptions(_Frozen):
    language: LangCode | None = None  # None = let the provider detect it
    prompt: str | None = None  # biasing text: glossary terms + recently committed text


class TranslationRequest(_Frozen):
    text: str
    source_lang: LangCode
    target_lang: LangCode
    context: tuple[str, ...] = ()  # previous source-language FINALs, oldest first
    glossary: Mapping[str, str] = Field(default_factory=dict)  # source term -> target term


@runtime_checkable
class Transcriber(Protocol):
    name: str

    async def transcribe(self, pcm: bytes, opts: TranscribeOptions) -> Transcript:
        """Transcribe a canonical PCM buffer. Must raise ProviderError on failure."""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Translator(Protocol):
    name: str

    async def translate(self, req: TranslationRequest) -> str:
        """Return the translated text only. Must raise ProviderError on failure."""
        ...

    async def aclose(self) -> None: ...


class ProviderError(Exception):
    """Raised by adapters. retryable=True for timeouts, 429s and 5xx."""

    def __init__(self, provider: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


# =============================================================================
# Redis / Valkey keys
# =============================================================================

KEY_PREFIX = "cf"


class Keys:
    """All keys live here so nobody formats them by hand."""

    SESSIONS = f"{KEY_PREFIX}:sessions"  # SET of session ids

    @staticmethod
    def config(sid: str) -> str:  # STRING: SessionConfig JSON
        return f"{KEY_PREFIX}:s:{sid}:config"

    @staticmethod
    def state(sid: str) -> str:  # STRING: SessionState JSON
        return f"{KEY_PREFIX}:s:{sid}:state"

    @staticmethod
    def lease(sid: str) -> str:  # STRING: worker_id, SET NX PX=LEASE_TTL_MS
        return f"{KEY_PREFIX}:s:{sid}:lease"

    @staticmethod
    def audio(sid: str) -> str:  # STREAM: field "pcm" (bytes), browser sources only
        return f"{KEY_PREFIX}:s:{sid}:audio"

    @staticmethod
    def captions(sid: str, lang: LangCode) -> str:  # STREAM: encode_message() fields
        return f"{KEY_PREFIX}:s:{sid}:captions:{lang}"


AUDIO_FIELD = "pcm"

# =============================================================================
# Tunables that both sides must agree on
# =============================================================================

LEASE_TTL_MS = 10_000  # a dead worker loses its session after this
LEASE_RENEW_EVERY_S = 3.0  # renew well before expiry
CAPTIONS_STREAM_MAXLEN = 50_000  # XADD MAXLEN ~ ; hours of partials + finals per lang
AUDIO_STREAM_MAXLEN = 3_000  # ~5 min of 100 ms browser chunks

SSE_EVENT_CAPTION = "caption"
SSE_EVENT_SESSION = "session"
SSE_KEEPALIVE_S = 15.0  # comment line so proxies do not close idle connections
