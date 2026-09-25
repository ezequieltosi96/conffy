"""Small audio helpers shared by ASR adapters."""

import io
import wave

from conffy.contracts import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap canonical PCM (s16le, 16 kHz, mono) in a WAV container, in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()
