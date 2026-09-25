import pytest

from conffy.contracts import CaptionKind, TranscribeOptions, Transcript, ProviderError
from conffy.worker.pipeline import AsrPipeline, PipelineConfig, clean_text
from conffy.worker.segmenter import FRAME_BYTES, FRAME_S, Segmenter


class FakeASR:
    name = "fake"

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls: list[TranscribeOptions] = []

    async def transcribe(self, pcm, opts):
        self.calls.append(opts)
        t = self.texts.pop(0)
        if isinstance(t, Exception):
            raise t
        return Transcript(text=t)

    async def aclose(self):
        pass


def speech_plan(*blocks):
    """blocks of (prob, seconds) -> list of per-frame probs"""
    return [p for prob, secs in blocks for p in [prob] * round(secs / FRAME_S)]


async def source(n_frames, chunk=3200):
    data = b"\x00" * (n_frames * FRAME_BYTES)
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


async def run_pipeline(probs, texts, **cfg):
    it = iter(probs)
    seg = Segmenter(lambda _f: next(it))
    asr = FakeASR(texts)
    out = []

    async def emit(c):
        out.append(c)

    p = AsrPipeline(asr, seg, emit, PipelineConfig(session_id="s1", partial_every_s=0, **cfg))
    await p.run(source(len(probs)))
    return p, asr, out


async def test_finals_get_consecutive_seqs_and_prompt_carries_context():
    probs = speech_plan((0.0, 0.5), (0.9, 1.0), (0.0, 1.0), (0.9, 1.0), (0.0, 1.0))
    p, asr, out = await run_pipeline(probs, ["Hello world.", "Second sentence."])
    assert [(c.kind, c.seq, c.text) for c in out] == [
        (CaptionKind.FINAL, 0, "Hello world."),
        (CaptionKind.FINAL, 1, "Second sentence."),
    ]
    assert asr.calls[1].prompt == "Hello world."
    assert out[0].t0 < out[0].t1 <= out[1].t0


async def test_empty_and_failed_transcriptions_do_not_consume_seq():
    probs = speech_plan((0.9, 1.0), (0.0, 1.0), (0.9, 1.0), (0.0, 1.0), (0.9, 1.0), (0.0, 1.0))
    p, _, out = await run_pipeline(
        probs, ["[BLANK_AUDIO]", ProviderError("fake", "boom", retryable=True), "Real text."]
    )
    assert [(c.seq, c.text) for c in out] == [(0, "Real text.")]
    assert p.stats.dropped == 2 and p.stats.errors == 1


async def test_short_hallucination_is_dropped():
    probs = speech_plan((0.9, 1.0), (0.0, 1.0))
    _, _, out = await run_pipeline(probs, ["Thank you."])
    assert out == []


def test_clean_text():
    assert clean_text(" [BLANK_AUDIO] hello (music)  world ") == "hello world"


def test_partial_interval_adapts_to_asr_latency():
    from conffy.worker.segmenter import Segmenter
    p = AsrPipeline(FakeASR([]), Segmenter(lambda f: 0.0), None, PipelineConfig(session_id="s"))
    assert p._partial_interval() == 1.0  # no measurement yet: the configured floor
    p.stats.last_asr_ms = 620
    assert abs(p._partial_interval() - 1.24) < 1e-9
