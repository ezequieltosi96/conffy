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
- Translate ONLY the text inside <text>. It is often a fragment of a longer sentence:
  translate it as it is. Never complete it, reorder it or merge it with the context.
- Translate ALL of it, including incomplete clauses at the start or the end (for
  example a verb without its subject). Never drop a part. Only pure filler words
  ("eh", "¿sí?", "you know") may be left out.
- Always translate into {tgt}. Keep in the original only code, commands, product and
  brand names, and glossary terms that map to themselves.
- If a <glossary> is given, use its translations exactly.
- <context> holds the previous subtitles, only to disambiguate. Never repeat or translate it.
- Reply with the translation only: no quotes, notes or explanations."""

# One worked example per language pair teaches small models the fragment rules
# (translate a subject-less start, don't complete a cut-off end) better than any
# instruction. Pairs without an example rely on the rules alone.
FEW_SHOT: dict[tuple[str, str], tuple[str, str]] = {
    ("en", "es"): (
        "<context>\nThe platform team moved everything to Kubernetes.\n</context>\n"
        "<text>\nmanaged to scale without touching the code. How? With patience and\n</text>",
        "lograron escalar sin tocar el código. ¿Cómo? Con paciencia y",
    ),
    ("es", "en"): (
        "<context>\nEl equipo de plataforma migró todo a Kubernetes.\n</context>\n"
        "<text>\nlograron escalar sin tocar el código. ¿Cómo? Con paciencia y\n</text>",
        "managed to scale without touching the code. How? With patience and",
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
        reasoning_effort: str | None = "none",
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", timeout=timeout_s, headers=headers
        )
        self._model = model
        # Thinking models (Gemma 4 on Ollama, among others) reason by default: the
        # reasoning eats max_tokens and `content` comes back empty. "none" turns it
        # off in Ollama. Servers that reject the field get it dropped automatically.
        self._reasoning_effort = reasoning_effort or None

    async def translate(self, req: TranslationRequest) -> str:
        text = await self._translate_once(req)
        if req.context and len(text) > 2.5 * len(req.text) + 40:
            # Small models sometimes translate the <context> instead of the <text>
            # (seen with a 5-word sentence after 3 long ones). An answer far longer
            # than the source is the tell: retry once without context.
            text = await self._translate_once(req.model_copy(update={"context": ()}))
        return text

    async def _translate_once(self, req: TranslationRequest) -> str:
        payload = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 64 + 4 * len(req.text.split()),  # guards against runaway output
            "stream": False,
            "messages": build_messages(req),
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        resp = await self._post(payload)
        if resp.status_code == 400 and "reasoning" in resp.text.lower() and "reasoning_effort" in payload:
            self._reasoning_effort = None  # this server doesn't take it: stop sending it
            del payload["reasoning_effort"]
            resp = await self._post(payload)

        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(self.name, f"HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False)

        try:
            choice = resp.json()["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(self.name, f"unexpected response: {resp.text[:200]}", retryable=False) from e
        text = clean_translation(message.get("content") or "")
        if not text:
            if message.get("reasoning") or message.get("reasoning_content"):
                raise ProviderError(
                    self.name,
                    f"model spent its tokens reasoning (finish_reason={choice.get('finish_reason')}); "
                    "set MT_REASONING_EFFORT=none or use a non-thinking model",
                    retryable=False,
                )
            raise ProviderError(self.name, "empty translation", retryable=True)
        return text

    async def _post(self, payload: dict) -> httpx.Response:
        try:
            return await self._client.post("chat/completions", json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(self.name, f"request failed: {e!r}", retryable=True) from e

    async def aclose(self) -> None:
        await self._client.aclose()