import asyncio

import httpx
import pytest
import respx

from conffy.contracts import Caption, CaptionKind, ProviderError, TranslationRequest
from conffy.glossary import load_glossary
from conffy.providers.mt_openai_compat import (
    OpenAICompatTranslator,
    build_messages,
    build_user_message,
    clean_translation,
    relevant_glossary,
)
from conffy.worker.translate import TranslationStage


def cap(seq, text, kind=CaptionKind.FINAL, lang="en"):
    return Caption(session_id="s", lang=lang, kind=kind, seq=seq, text=text, t0=seq, t1=seq + 1)


class FakeMT:
    name = "fake"

    def __init__(self, fail_first=0):
        self.requests: list[TranslationRequest] = []
        self.fail_first = fail_first

    async def translate(self, req):
        self.requests.append(req)
        if self.fail_first:
            self.fail_first -= 1
            raise ProviderError("fake", "busy", retryable=True)
        return f"{req.target_lang}:{req.text}"

    async def aclose(self):
        pass


async def run_stage(captions, mt, targets=("es",), **kw):
    out = []

    async def emit(c):
        out.append(c)

    stage = TranslationStage(mt, emit, source_lang="en", target_langs=targets, **kw)
    task = asyncio.create_task(stage.run())
    for c in captions:
        stage.submit(c)
    stage.close()
    await task
    return stage, out


async def test_translates_finals_only_in_order_with_context():
    mt = FakeMT()
    caps = [cap(0, "One."), cap(1, "partial", CaptionKind.PARTIAL), cap(1, "Two."), cap(2, "Three.")]
    _, out = await run_stage(caps, mt, context_size=2)
    assert [(c.lang, c.seq, c.source_seq, c.text) for c in out] == [
        ("es", 0, 0, "es:One."), ("es", 1, 1, "es:Two."), ("es", 2, 2, "es:Three."),
    ]
    assert [r.context for r in mt.requests] == [(), ("One.",), ("One.", "Two.")]
    assert out[1].t0 == 1 and out[1].t1 == 2


async def test_multiple_targets_and_glossary_per_lang():
    mt = FakeMT()
    _, out = await run_stage([cap(0, "Hi.")], mt, targets=("es", "pt"),
                             glossaries={"es": {"talk": "charla"}})
    assert sorted(c.lang for c in out) == ["es", "pt"]
    by_lang = {r.target_lang: r for r in mt.requests}
    assert by_lang["es"].glossary == {"talk": "charla"} and by_lang["pt"].glossary == {}


async def test_retryable_error_is_retried_then_gives_up():
    stage, out = await run_stage([cap(0, "A.")], FakeMT(fail_first=1))
    assert len(out) == 1 and stage.stats.errors == 0
    stage, out = await run_stage([cap(0, "A."), cap(1, "B.")], FakeMT(fail_first=2), retries=1)
    assert [c.seq for c in out] == [1] and stage.stats.errors == 1


def test_user_message_and_cleanup():
    msg = build_user_message(TranslationRequest(
        text="Deploy it on Kubernetes.", source_lang="en", target_lang="es",
        context=("Hello.",), glossary={"deploy": "despliegue", "talk": "charla", "Kubernetes": "Kubernetes"}))
    assert "<context>\nHello.\n</context>" in msg
    assert "deploy -> despliegue" in msg and "Kubernetes -> Kubernetes" in msg
    assert "talk" not in msg  # not in the text: filtered out
    assert msg.endswith("<text>\nDeploy it on Kubernetes.\n</text>")
    assert clean_translation(' "Hola mundo." ') == "Hola mundo."
    assert clean_translation("“Hola”") == "Hola"
    assert clean_translation("<text>Hola</text>") == "Hola"


def test_glossary_matches_whole_words_only():
    g = {"LLM": "LLM", "talk": "charla"}
    assert relevant_glossary("LLMs are great", g) == {}
    assert relevant_glossary("my talk, about an LLM.", g) == g


def test_messages_include_few_shot_only_for_known_pairs():
    es = build_messages(TranslationRequest(text="Hi", source_lang="en", target_lang="es"))
    pt = build_messages(TranslationRequest(text="Hi", source_lang="en", target_lang="pt"))
    assert [m["role"] for m in es] == ["system", "user", "assistant", "user"]
    assert [m["role"] for m in pt] == ["system", "user"]
    assert "Spanish" in es[0]["content"] and "Portuguese" in pt[0]["content"]


@respx.mock
async def test_openai_compat_request_and_errors():
    route = respx.post("http://mt.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Hola."}}]})
    )
    mt = OpenAICompatTranslator("http://mt.test/v1", "gemma4:e4b", api_key="k")
    req = TranslationRequest(text="Hello.", source_lang="en", target_lang="es")
    assert await mt.translate(req) == "Hola."
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer k"
    assert b'"model":"gemma4:e4b"' in sent.content.replace(b" ", b"") and b"Spanish" in sent.content
    route.mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError) as e:
        await mt.translate(req)
    assert e.value.retryable


def test_glossary_file(tmp_path):
    f = tmp_path / "g.yml"
    f.write_text("conf:\n  keep: [Nerdearla, Kubernetes]\n  translate:\n    es: {talk: charla}\n")
    g = load_glossary(f, "conf")
    assert g.for_lang("es") == {"Nerdearla": "Nerdearla", "Kubernetes": "Kubernetes", "talk": "charla"}
    assert g.for_lang("pt") == {"Nerdearla": "Nerdearla", "Kubernetes": "Kubernetes"}
    assert g.asr_prompt() == "Nerdearla, Kubernetes, talk"
    with pytest.raises(KeyError):
        load_glossary(f, "missing")


@respx.mock
async def test_reasoning_is_disabled_by_default_and_dropped_if_rejected():
    route = respx.post("http://mt.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": "unknown field reasoning_effort"}}),
        httpx.Response(200, json={"choices": [{"message": {"content": "Hi."}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "Bye."}}]}),
    ]
    mt = OpenAICompatTranslator("http://mt.test/v1", "m")
    req = TranslationRequest(text="Hola.", source_lang="es", target_lang="en")
    assert await mt.translate(req) == "Hi."
    assert b'"reasoning_effort":"none"' in route.calls[0].request.content.replace(b" ", b"")
    assert b"reasoning_effort" not in route.calls[1].request.content
    assert await mt.translate(req) == "Bye."
    assert b"reasoning_effort" not in route.calls[2].request.content  # remembered


@respx.mock
async def test_empty_content_with_reasoning_gives_a_clear_error():
    respx.post("http://mt.test/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "", "reasoning": "Thinking Process: ..."}, "finish_reason": "length"}]}))
    mt = OpenAICompatTranslator("http://mt.test/v1", "m", reasoning_effort=None)
    with pytest.raises(ProviderError) as e:
        await mt.translate(TranslationRequest(text="Hola.", source_lang="es", target_lang="en"))
    assert "reasoning" in str(e.value) and not e.value.retryable


@respx.mock
async def test_context_leak_is_detected_and_retried_without_context():
    leaked = "de la persona o equipo e incluso llegar a un punto muerto. En el exterior a menudo necesitas aserrar madera."
    route = respx.post("http://mt.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, json={"choices": [{"message": {"content": leaked}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "deja que la sierra haga el trabajo."}}]}),
    ]
    mt = OpenAICompatTranslator("http://mt.test/v1", "m")
    req = TranslationRequest(text="let the saw do the work.", source_lang="en", target_lang="es",
                             context=("of the person or team and even grinding to a halt.",
                                      "In the outdoors you often need to saw timber."))
    assert await mt.translate(req) == "deja que la sierra haga el trabajo."
    import json
    last_user = lambda call: json.loads(call.request.content)["messages"][-1]["content"]
    assert "<context>" in last_user(route.calls[0])
    assert "<context>" not in last_user(route.calls[1])  # (the few-shot example keeps its own)


@respx.mock
async def test_normal_length_translation_is_not_retried():
    route = respx.post("http://mt.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Hola a todos."}}]}))
    mt = OpenAICompatTranslator("http://mt.test/v1", "m")
    req = TranslationRequest(text="Hello everyone.", source_lang="en", target_lang="es", context=("Welcome.",))
    assert await mt.translate(req) == "Hola a todos." and route.call_count == 1