"""Picks the AI adapters from settings. The only place that knows concrete classes."""

from __future__ import annotations

from conffy.contracts import Transcriber, Translator
from conffy.settings import Settings


def make_transcriber(s: Settings) -> Transcriber:
    if s.asr_provider == "whispercpp":
        from conffy.providers.asr_whispercpp import WhisperCppTranscriber

        return WhisperCppTranscriber(s.asr_url)
    raise ValueError(f"unknown ASR_PROVIDER {s.asr_provider!r} (available: whispercpp)")


def make_translator(s: Settings) -> Translator:
    if s.mt_provider == "openai_compat":
        from conffy.providers.mt_openai_compat import OpenAICompatTranslator

        return OpenAICompatTranslator(s.mt_url, s.mt_model, api_key=s.mt_api_key)
    raise ValueError(f"unknown MT_PROVIDER {s.mt_provider!r} (available: openai_compat)")
