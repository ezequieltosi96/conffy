"""
Glossaries: one file, two uses.

  * ASR: every term is passed to Whisper as prompt, so names like "Nerdearla"
    or "Kubernetes" are recognized instead of misheard.
  * MT: `keep` terms stay as they are; `translate` fixes the translation of
    specific terms per target language.

config/glossary.yml:

    nerdearla:
      keep: [Nerdearla, Kubernetes, open source, pull request]
      translate:
        es: {deploy: despliegue, "feature flag": "feature flag"}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from conffy.contracts import LangCode


@dataclass(frozen=True)
class Glossary:
    keep: tuple[str, ...] = ()
    translate: dict[LangCode, dict[str, str]] = field(default_factory=dict)

    def for_lang(self, lang: LangCode) -> dict[str, str]:
        """Term map for TranslationRequest.glossary."""
        terms = {t: t for t in self.keep}
        terms.update(self.translate.get(lang, {}))
        return terms

    def asr_prompt(self, max_chars: int = 400) -> str | None:
        """Comma-separated terms for Whisper's prompt."""
        terms = list(dict.fromkeys([*self.keep, *(t for m in self.translate.values() for t in m)]))
        text = ", ".join(terms)
        return text[:max_chars] or None


EMPTY = Glossary()


def load_glossary(path: str | Path, name: str) -> Glossary:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if name not in data:
        raise KeyError(f"glossary {name!r} not found in {path} (available: {', '.join(data) or 'none'})")
    entry = data[name] or {}
    keep = tuple(str(t) for t in entry.get("keep") or [])
    translate = {
        str(lang): {str(k): str(v) for k, v in (terms or {}).items()}
        for lang, terms in (entry.get("translate") or {}).items()
    }
    return Glossary(keep=keep, translate=translate)
