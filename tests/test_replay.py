import os

from conffy.contracts import BYTES_PER_SECOND, Transcriber, TranscribeOptions, TranslationRequest, Translator
from conffy.providers.registry import make_transcriber, make_translator
from conffy.providers.replay import ReplayTranscriber, ReplayTranslator
from conffy.settings import Settings


def audio(seconds: float, seed: int) -> bytes:
    # deterministic, distinct content per utterance (seed changes the first bytes)
    return bytes([seed % 256]) * int(seconds * BYTES_PER_SECOND)


async def test_partials_are_prefixes_of_their_final_and_utterances_advance(tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("one two three four five six seven eight nine ten eleven twelve")
    asr = ReplayTranscriber(latency_ms=0, transcript_path=str(t))
    opts = TranscribeOptions(language="en")
    p1 = (await asr.transcribe(audio(0.8, 1), opts)).text
    p2 = (await asr.transcribe(audio(1.6, 1), opts)).text
    f1 = (await asr.transcribe(audio(2.0, 1), opts)).text
    assert f1 == "one two three four five" and f1.startswith(p2) and p2.startswith(p1)
    f2 = (await asr.transcribe(audio(1.2, 2), opts)).text  # new utterance continues the text
    assert f2 == "six seven eight"


async def test_transcript_loops_and_default_text_works():
    asr = ReplayTranscriber(latency_ms=0)
    t = await asr.transcribe(audio(40, 3), TranscribeOptions(language="en"))
    assert len(t.text.split()) == 100 and t.language == "en"


async def test_translator_marks_output_as_fake():
    mt = ReplayTranslator(latency_ms=0)
    out = await mt.translate(TranslationRequest(text="Hello.", source_lang="en", target_lang="es"))
    assert out == "[es] Hello."


def test_registry_builds_replay_providers(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "replay")
    monkeypatch.setenv("MT_PROVIDER", "replay")
    monkeypatch.setenv("REPLAY_ASR_LATENCY_MS", "10")
    s = Settings()
    assert isinstance(make_transcriber(s), Transcriber) and make_transcriber(s).name == "replay"
    assert isinstance(make_translator(s), Translator) and make_translator(s).name == "replay"