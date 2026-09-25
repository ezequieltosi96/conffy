"""Runtime settings from environment variables (see docs/CONTRACTS.md)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    valkey_url: str = field(default_factory=lambda: _env("VALKEY_URL", "redis://localhost:6379/0"))
    asr_provider: str = field(default_factory=lambda: _env("ASR_PROVIDER", "whispercpp"))
    asr_url: str = field(default_factory=lambda: _env("ASR_URL", "http://127.0.0.1:8081"))
    mt_provider: str = field(default_factory=lambda: _env("MT_PROVIDER", "openai_compat"))
    mt_url: str = field(default_factory=lambda: _env("MT_URL", "http://127.0.0.1:11434/v1"))
    mt_model: str = field(default_factory=lambda: _env("MT_MODEL", "gemma4:e4b"))
    mt_api_key: str | None = field(default_factory=lambda: os.environ.get("MT_API_KEY") or None)
    sessions_file: str = field(default_factory=lambda: _env("SESSIONS_FILE", "config/sessions.yml"))
    glossary_file: str = field(default_factory=lambda: _env("GLOSSARY_FILE", "config/glossary.yml"))
    replay_asr_latency_ms: float = field(default_factory=lambda: float(_env("REPLAY_ASR_LATENCY_MS", "600")))
    replay_mt_latency_ms: float = field(default_factory=lambda: float(_env("REPLAY_MT_LATENCY_MS", "1200")))
    replay_transcript: str | None = field(default_factory=lambda: os.environ.get("REPLAY_TRANSCRIPT") or None)
    worker_id: str = field(
        default_factory=lambda: _env("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
    )