"""
Translator adapter for any OpenAI-compatible chat completions API.

One adapter covers Ollama, vLLM, llama.cpp server, LM Studio and most hosted
providers: only base_url, model and api_key change.
"""

from __future__ import annotations

import httpx

from conffy.contracts import ProviderError, TranslationRequest

LANG_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian"}

SYSTEM_PROMPT = """You are a live subtitle translator for a technology conference.
Translate the text from {src} to {tgt}.
Rules:
- Reply with the translation only. No quotes, notes or explanations.
- Keep it concise and natural, like a subtitle; keep the speaker's meaning and tone.
- Keep code, commands, product names, acronyms and technical terms in their usual form.
- If a glossary is given, use its translations exactly.
- The context sentences are only there to disambiguate; never translate them."""


def build_user_message(req: TranslationRequest) -> str:
    parts = []
    if req.context:
        parts.append("Context (previous sentences, do not translate):\n" + "\n".join(req.context))
    if req.glossary:
        lines = [f"- {src} -> {dst}" for src, dst in req.glossary.items()]
        parts.append("Glossary:\n" + "\n".join(lines))
    parts.append("Text to translate:\n" + req.text)
    return "\n\n".join(parts)


def clean_translation(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'«“":
        text = text[1:-1].strip()
    if text.startswith("“") and text.endswith("”"):
        text = text[1:-1].strip()
    return text


class OpenAICompatTranslator:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", timeout=timeout_s, headers=headers
        )
        self._model = model

    async def translate(self, req: TranslationRequest) -> str:
        src = LANG_NAMES.get(req.source_lang, req.source_lang)
        tgt = LANG_NAMES.get(req.target_lang, req.target_lang)
        payload = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 64 + 4 * len(req.text.split()),  # guards against runaway output
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(src=src, tgt=tgt)},
                {"role": "user", "content": build_user_message(req)},
            ],
        }
        try:
            resp = await self._client.post("chat/completions", json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(self.name, f"request failed: {e!r}", retryable=True) from e

        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(self.name, f"HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False)

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(self.name, f"unexpected response: {resp.text[:200]}", retryable=False) from e
        text = clean_translation(content or "")
        if not text:
            raise ProviderError(self.name, "empty translation", retryable=True)
        return text

    async def aclose(self) -> None:
        await self._client.aclose()
