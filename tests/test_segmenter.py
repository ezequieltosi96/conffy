from conffy.worker.segmenter import FRAME_BYTES, FRAME_S, Segmenter, SegmenterConfig

FRAME = b"\x00" * FRAME_BYTES


def scripted(probs):
    it = iter(probs)
    return lambda _frame: next(it)


def run(probs, cfg=None):
    seg = Segmenter(scripted(probs), cfg)
    out = [u for p in probs if (u := seg.feed(FRAME))]
    return seg, out


def frames(seconds):
    return round(seconds / FRAME_S)


def test_silence_closes_utterance_and_trims_tail():
    probs = [0.0] * 20 + [0.9] * frames(1.0) + [0.0] * frames(0.7)
    _, out = run(probs)
    assert len(out) == 1
    u = out[0]
    # starts ~pre_roll (0.3 s) before onset at 20 frames
    assert abs(u.t0 - (20 - frames(0.3) + 1) * FRAME_S) < 1e-3
    # speech (1 s) + pre-roll + kept tail (0.2 s), trailing silence trimmed
    assert 1.4 < u.duration < 1.6


def test_short_blip_is_dropped():
    probs = [0.9] * 3 + [0.0] * frames(1.0)
    _, out = run(probs)
    assert out == []


def test_soft_max_cuts_at_short_pause():
    cfg = SegmenterConfig(soft_max_s=2.0, hard_max_s=10.0)
    probs = [0.9] * frames(2.5) + [0.1] * frames(0.25) + [0.9] * frames(1.0)
    seg, out = run(probs, cfg)
    assert len(out) == 1 and 2.5 < out[0].duration < 3.0
    assert seg.active  # speaker kept talking: a new utterance is in progress


def test_hard_max_cuts_continuous_speech():
    cfg = SegmenterConfig(soft_max_s=2.0, hard_max_s=3.0)
    _, out = run([0.9] * frames(6.5), cfg)
    assert len(out) == 2
    assert all(abs(u.duration - 3.0) < 0.05 for u in out)
    assert out[1].t0 >= out[0].t1 - 1e-6


def test_snapshot_and_flush():
    probs = [0.9] * frames(1.0)
    seg, out = run(probs)
    assert out == [] and seg.snapshot() is not None
    u = seg.flush()
    assert u is not None and seg.snapshot() is None and not seg.active
