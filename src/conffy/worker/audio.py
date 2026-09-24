"""Audio ingestion: turns any Source into a stream of canonical PCM chunks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from conffy.contracts import SAMPLE_RATE, Source, SourceKind


class AudioSourceError(Exception):
    pass


def ffmpeg_args(source: Source) -> list[str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if source.kind is SourceKind.BROWSER:
        raise AudioSourceError("browser sources are read from Valkey, not ffmpeg")
    if source.kind is SourceKind.FILE:
        if source.realtime:
            args += ["-re"]  # read at native speed: simulates a live talk
        if source.loop:
            args += ["-stream_loop", "-1"]
    elif source.uri and source.uri.startswith("http"):
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    args += [
        "-i", str(source.uri),
        "-vn",  # drop video if present
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-acodec", "pcm_s16le",
        "-f", "s16le",
        "pipe:1",
    ]
    return args


async def ffmpeg_pcm(source: Source, chunk_bytes: int = 3_200) -> AsyncIterator[bytes]:
    """Yield canonical PCM chunks (default 100 ms) until the source ends."""
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_args(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        while chunk := await proc.stdout.read(chunk_bytes):
            yield chunk
        if await proc.wait() != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            raise AudioSourceError(f"ffmpeg exited with {proc.returncode}: {err[-500:]}")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
