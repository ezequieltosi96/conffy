"""
Utterance segmentation driven by voice activity detection.

Cuts the continuous audio into utterances (roughly: sentences) that are sent
to ASR as a whole. Pure and synchronous: the VAD is injected, so tests can
script probabilities without a model.

Cutting rules, checked on every 32 ms frame while speech is active:
  1. min_silence_s of silence            -> natural end of sentence
  2. longer than soft_max_s and a short  -> speaker talks without long pauses;
     pause of soft_pause_s                  cut at the first breath
  3. longer than hard_max_s              -> cut no matter what
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from conffy.contracts import BYTES_PER_SECOND, SAMPLE_WIDTH

FRAME_SAMPLES = 512  # what Silero VAD expects at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH  # 1024
FRAME_S = FRAME_BYTES / BYTES_PER_SECOND  # 0.032

VadFn = Callable[[bytes], float]  # one FRAME_BYTES frame -> speech probability


@dataclass(frozen=True)
class SegmenterConfig:
    threshold: float = 0.5  # prob >= threshold: speech
    neg_threshold: float = 0.35  # prob < neg_threshold: silence (in between: keep state)
    min_silence_s: float = 0.6
    pre_roll_s: float = 0.3  # audio kept before speech onset (catches soft first syllables)
    keep_tail_s: float = 0.2  # trailing silence kept in the utterance
    soft_max_s: float = 8.0
    soft_pause_s: float = 0.2
    hard_max_s: float = 15.0
    min_speech_s: float = 0.3  # utterances with less speech than this are dropped


@dataclass(frozen=True)
class Utterance:
    pcm: bytes
    t0: float  # session seconds
    t1: float

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


def _frames(seconds: float) -> int:
    return max(1, round(seconds / FRAME_S))


class Segmenter:
    def __init__(self, vad: VadFn, cfg: SegmenterConfig | None = None) -> None:
        self._vad = vad
        self.cfg = cfg or SegmenterConfig()
        self._min_silence = _frames(self.cfg.min_silence_s)
        self._soft_pause = _frames(self.cfg.soft_pause_s)
        self._keep_tail = _frames(self.cfg.keep_tail_s)
        self._min_speech = _frames(self.cfg.min_speech_s)
        self._pre_roll: deque[bytes] = deque(maxlen=_frames(self.cfg.pre_roll_s))
        self._frame_idx = 0  # frames seen since the session started
        self._reset()

    # ------------------------------------------------------------------ state
    def _reset(self) -> None:
        self._active = False
        self._buf = bytearray()
        self._start_idx = 0
        self._silence = 0  # consecutive silent frames at the tail
        self._speech = 0  # speech frames in the current utterance

    @property
    def active(self) -> bool:
        return self._active

    @property
    def clock(self) -> float:
        """Session seconds of audio processed so far."""
        return self._frame_idx * FRAME_S

    # ------------------------------------------------------------------ api
    def feed(self, frame: bytes) -> Utterance | None:
        """Process one FRAME_BYTES frame. Returns an utterance if this frame closed one."""
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"frame must be {FRAME_BYTES} bytes, got {len(frame)}")
        idx = self._frame_idx
        self._frame_idx += 1
        p = self._vad(frame)

        if not self._active:
            self._pre_roll.append(frame)
            if p >= self.cfg.threshold:
                self._active = True
                self._start_idx = idx - len(self._pre_roll) + 1
                self._buf = bytearray(b"".join(self._pre_roll))
                self._pre_roll.clear()
                self._speech = 1
            return None

        self._buf += frame
        if p >= self.cfg.threshold:
            self._silence = 0
            self._speech += 1
        elif p < self.cfg.neg_threshold:
            self._silence += 1

        duration = len(self._buf) / BYTES_PER_SECOND
        if self._silence >= self._min_silence:
            return self._close(trim_frames=self._silence - self._keep_tail)
        if duration >= self.cfg.soft_max_s and self._silence >= self._soft_pause:
            return self._close(trim_frames=0)
        if duration >= self.cfg.hard_max_s:
            return self._close(trim_frames=0)
        return None

    def snapshot(self) -> Utterance | None:
        """The utterance in progress, for partial transcriptions. None if idle."""
        if not self._active:
            return None
        return self._make(bytes(self._buf))

    def flush(self) -> Utterance | None:
        """Close whatever is in progress (end of stream)."""
        if not self._active:
            return None
        return self._close(trim_frames=max(0, self._silence - self._keep_tail))

    # ------------------------------------------------------------------ internals
    def _make(self, pcm: bytes) -> Utterance:
        t0 = self._start_idx * FRAME_S
        return Utterance(pcm=pcm, t0=round(t0, 3), t1=round(t0 + len(pcm) / BYTES_PER_SECOND, 3))

    def _close(self, trim_frames: int) -> Utterance | None:
        pcm = bytes(self._buf)
        if trim_frames > 0:
            pcm = pcm[: len(pcm) - trim_frames * FRAME_BYTES]
        enough_speech = self._speech >= self._min_speech
        utt = self._make(pcm)
        self._reset()
        return utt if enough_speech and pcm else None
