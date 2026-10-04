"""Gemini integration, with Google's client replaced by a fake (no network, no key needed)."""
from types import SimpleNamespace

import pytest
from google import genai
from google.genai import errors

import llm
from rag import Chunk

CHUNKS = [Chunk(0, "Qorvexa_HR_Handbook.pdf", 16, "13. Conflict of Interest & Gifts",
                "Do not accept cash or cash-equivalent benefits from vendors.")]


class FakeClient:
    calls = []
    reply = ["You can't keep it ", "[p. 16]."]
    error = None

    def __init__(self, api_key):
        assert api_key == "test-key"
        self.models = self

    def generate_content_stream(self, model, contents, config):
        FakeClient.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        if FakeClient.error:
            raise FakeClient.error
        for text in FakeClient.reply:
            yield SimpleNamespace(text=text)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setattr(genai, "Client", FakeClient)
    FakeClient.calls, FakeClient.error = [], None


def test_provider_selection(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "")
    assert llm.provider() is None
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert llm.provider() == "gemini"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert llm.provider() == "claude"          # both keys: Claude unless told otherwise
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm.provider() == "gemini"
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert llm.provider() is None              # chosen provider has no key -> passage mode


def test_gemini_streams_grounded_answer(gemini):
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"}]
    answer = "".join(llm.stream_answer("Can I keep a gift card?", CHUNKS, history))
    assert answer == "You can't keep it [p. 16]."
    call = FakeClient.calls[0]
    assert call.model == llm.GEMINI_MODEL
    assert [c.role for c in call.contents] == ["user", "model", "user"]
    assert "<passage" in call.contents[-1].parts[0].text and "page=\"16\"" in call.contents[-1].parts[0].text
    assert call.config.system_instruction == llm.SYSTEM_PROMPT


def test_gemini_rate_limit(gemini):
    FakeClient.error = errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    assert "busy" in "".join(llm.stream_answer("q", CHUNKS, []))


def test_gemini_bad_key_falls_back_to_passages(gemini):
    FakeClient.error = errors.ClientError(400, {"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}})
    answer = "".join(llm.stream_answer("q", CHUNKS, []))
    assert "check GEMINI_API_KEY" in answer and "cash-equivalent" in answer
