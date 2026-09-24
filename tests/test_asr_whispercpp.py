import httpx
import pytest
import respx

from conffy.contracts import ProviderError, TranscribeOptions
from conffy.providers.asr_whispercpp import WhisperCppTranscriber

BASE = "http://asr.test"


@respx.mock
async def test_parses_verbose_json_and_sends_options():
    route = respx.post(f"{BASE}/inference").mock(
        return_value=httpx.Response(200, json={
            "language": "en", "text": " Hello there.",
            "segments": [{"text": " Hello there.", "start": 0.0, "end": 1.2,
                          "words": [{"word": " Hello", "start": 0.0, "end": 0.5, "probability": 0.9}]}],
        })
    )
    asr = WhisperCppTranscriber(BASE, detailed=True)
    t = await asr.transcribe(b"\x00" * 32000, TranscribeOptions(language="en", prompt="Nerdearla"))
    await asr.aclose()
    assert t.text == "Hello there." and t.language == "en"
    assert t.segments[0].words[0].text == "Hello"
    body = route.calls[0].request.content
    assert b"Nerdearla" in body and b"RIFF" in body and b"verbose_json" in body


@respx.mock
@pytest.mark.parametrize("status,retryable", [(500, True), (429, True), (400, False)])
async def test_http_errors_map_to_provider_error(status, retryable):
    respx.post(f"{BASE}/inference").mock(return_value=httpx.Response(status, text="nope"))
    asr = WhisperCppTranscriber(BASE)
    with pytest.raises(ProviderError) as e:
        await asr.transcribe(b"\x00" * 3200, TranscribeOptions())
    assert e.value.retryable is retryable


@respx.mock
async def test_connection_error_is_retryable():
    respx.post(f"{BASE}/inference").mock(side_effect=httpx.ConnectError("down"))
    asr = WhisperCppTranscriber(BASE)
    with pytest.raises(ProviderError) as e:
        await asr.transcribe(b"\x00" * 3200, TranscribeOptions())
    assert e.value.retryable


@respx.mock
async def test_default_asks_plain_json_and_parses_text_only():
    route = respx.post(f"{BASE}/inference").mock(
        return_value=httpx.Response(200, json={"text": " Just text.\n"})
    )
    asr = WhisperCppTranscriber(BASE)
    t = await asr.transcribe(b"\x00" * 3200, TranscribeOptions(language="en"))
    body = route.calls[0].request.content
    assert b"verbose_json" not in body and b'name="response_format"\r\n\r\njson' in body
    assert t.text == "Just text." and t.segments == ()
