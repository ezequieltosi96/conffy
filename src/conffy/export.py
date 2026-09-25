"""Subtitle file formats. Pure functions over FINAL captions."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence

from conffy.contracts import Caption

LINE_CHARS = 42  # broadcast subtitle convention: max ~42 chars per line, 2 lines


def _ts(seconds: float, sep: str) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _cues(captions: Sequence[Caption]) -> list[tuple[float, float, str]]:
    """Split long captions into cues of at most 2 lines, sharing time proportionally."""
    cues = []
    for c in captions:
        lines = textwrap.wrap(c.text, LINE_CHARS) or [""]
        chunks = ["\n".join(lines[i : i + 2]) for i in range(0, len(lines), 2)]
        total = sum(len(ch) for ch in chunks) or 1
        t = c.t0
        for ch in chunks:
            end = t + (c.t1 - c.t0) * len(ch) / total
            cues.append((t, end, ch))
            t = end
    return cues


def to_srt(captions: Sequence[Caption]) -> str:
    blocks = [
        f"{i}\n{_ts(a, ',')} --> {_ts(b, ',')}\n{text}"
        for i, (a, b, text) in enumerate(_cues(captions), start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(captions: Sequence[Caption]) -> str:
    blocks = [f"{_ts(a, '.')} --> {_ts(b, '.')}\n{text}" for a, b, text in _cues(captions)]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def to_txt(captions: Sequence[Caption]) -> str:
    return "\n".join(c.text for c in captions) + ("\n" if captions else "")


FORMATS = {
    "srt": (to_srt, "application/x-subrip"),
    "vtt": (to_vtt, "text/vtt"),
    "txt": (to_txt, "text/plain"),
}
