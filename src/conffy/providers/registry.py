"""Picks the AI adapters from settings. The only place that knows concrete classes."""

from __future__ import annotations

import os

from conffy.contracts import Transcriber, Translator
from conffy.settings import Settings


def make_transcriber(s: Settings) -> Transcriber:
    if s.asr_provider == "whispercpp":
        from conffy.providers.asr_whispercpp import WhisperCppTranscriber

        return WhisperCppTranscriber(s.asr_url)
    if s.asr_provider == "replay":
        from conffy.providers.replay import ReplayTranscriber

        return ReplayTranscriber(s.replay_asr_latency_ms, s.replay_transcript)
    raise ValueError(f"unknown ASR_PROVIDER {s.asr_provider!r} (available: whispercpp, replay)")


def make_translator(s: Settings) -> Translator:
    if s.mt_provider == "openai_compat":
        from conffy.providers.mt_openai_compat import OpenAICompatTranslator

        # "none" disables thinking (Ollama); set MT_REASONING_EFFORT="" to not send the field
        effort = os.environ.get("MT_REASONING_EFFORT", "none")
        return OpenAICompatTranslator(s.mt_url, s.mt_model, api_key=s.mt_api_key, reasoning_effort=effort)
    if s.mt_provider == "replay":
        from conffy.providers.replay import ReplayTranslator

        return ReplayTranslator(s.replay_mt_latency_ms)
    raise ValueError(f"unknown MT_PROVIDER {s.mt_provider!r} (available: openai_compat, replay)")