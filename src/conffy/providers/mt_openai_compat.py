"""
Translator adapter for any OpenAI-compatible chat completions API.

One adapter covers Ollama, vLLM, llama.cpp server, LM Studio and most hosted
providers: only base_url, model and api_key change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import httpx

from conffy.contracts import ProviderError, TranslationRequest

LANG_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian"}

SYSTEM_PROMPT = """You translate live subtitles of a technology conference from {src} to {tgt}.
Rules:
- Translate ONLY the text inside <text>. It is often an incomplete fragment of a longer
  sentence: translate that fragment as it is. Never complete it, reorder it or merge it
  with the context.
- Always translate into {tgt}. Keep in the original only code, commands, product and
  brand names, and glossary terms that map to themselves.
- If a <glossary> is given, use its translations exactly.
- <context> holds the previous subtitles, only to disambiguate. Never repeat or translate it.
- Reply with the translation only: no quotes, notes or explanations."""

# One worked example per language pair teaches small models the fragment rule
# better than any instruction. Pairs without an example rely on the rules alone.
FEW_SHOT: dict[tuple[str, str], tuple[str, str]] = {
    ("en", "es"): (
        "<context>\nWe migrated everything to Kubernetes last year.\n</context>\n<text>\nand then the pods started\n</text>",
        "y entonces los pods empezaron",
    ),
    ("es", "en"): (
        "<context>\nEl año pasado migramos todo a Kubernetes.\n</context>\n<text>\ny entonces los pods empezaron\n</text>",
        "and then the pods started",
    ),
}


def relevant_glossary(text: str, glossary: Mapping[str, str]) -> dict[str, str]:
    """Only the terms that appear in the text: shorter prompt, less distraction."""
    return {
        src: dst
        for src, dst in glossary.items()
        if re.search(rf"(?<!\w){re.escape(src)}(?!\w)", text, flags=re.IGNORECASE)
    }


def build_user_message(req: TranslationRequest) -> str:
    parts = []
    if req.context:
        parts.append("<context>\n" + "\n".join(req.context) + "\n</context>")
    terms = relevant_glossary(req.text, req.glossary)
    if terms:
        lines = [f"{src} -> {dst}" for src, dst in terms.items()]
        parts.append("<glossary>\n" + "\n".join(lines) + "\n</glossary>")
    parts.append("<text>\n" + req.text + "\n</text>")
    return "\n".join(parts)


def build_messages(req: TranslationRequest) -> list[dict[str, str]]:
    src = LANG_NAMES.get(req.source_lang, req.source_lang)
    tgt = LANG_NAMES.get(req.target_lang, req.target_lang)
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(src=src, tgt=tgt)}]
    if example := FEW_SHOT.get((req.source_lang, req.target_lang)):
        messages += [
            {"role": "user", "content": example[0]},
            {"role": "assistant", "content": example[1]},
        ]
    messages.append({"role": "user", "content": build_user_message(req)})
    return messages


def clean_translation(text: str) -> str:
    text = re.sub(r"</?text>", "", text).strip()
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
        payload = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 64 + 4 * len(req.text.split()),  # guards against runaway output
            "stream": False,
            "messages": build_messages(req),
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
