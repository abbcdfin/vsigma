"""Thinking models at the LLM seam (docs/proposals/thinking-models.md §B)."""

import json
import logging

import pytest

from vsigma import FakeLLM, LoggingLLM, OpenAICompatibleLLM
from vsigma.llm import LLMResponse, _split_reasoning, default_llm

_LOGGER = "vsigma.llm"


def _client(
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, object],
    captured: dict[str, object] | None = None,
    max_tokens: int | None = None,
    extra_body: dict[str, object] | None = None,
) -> OpenAICompatibleLLM:
    class _Response:
        @staticmethod
        def raise_for_status() -> None:
            pass

        @staticmethod
        def json() -> dict[str, object]:
            return {"choices": [{"message": message}]}

    def fake_post(url: str, *, json: object, headers: object, timeout: object) -> _Response:
        if captured is not None:
            assert isinstance(json, dict)
            captured.update(json)
        return _Response()

    monkeypatch.setattr("vsigma.llm.httpx.post", fake_post)
    return OpenAICompatibleLLM(
        model="m",
        base_url="http://x",
        api_key="k",
        max_tokens=max_tokens,
        extra_body=extra_body or {},
    )


def test_split_reasoning_handles_terminated_multiple_and_truncated_blocks() -> None:
    assert _split_reasoning("no blocks here") == ("no blocks here", "")
    assert _split_reasoning("<think>hmm</think>answer") == ("answer", "hmm")
    text, reasoning = _split_reasoning("<think>a</think>mid<think>b</think> tail")
    assert text == "mid tail" and reasoning == "a\n\nb"
    # Truncation mid-thought: strip to end — never leak the fragment into text.
    assert _split_reasoning("<think>cut off by max_tok") == ("", "cut off by max_tok")


def test_client_captures_separate_reasoning_field(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _client(
        monkeypatch, {"content": "the answer", "reasoning_content": "step 1... step 2..."}
    )
    response = llm.complete([{"role": "user", "content": "q"}], [])
    assert response.text == "the answer"
    assert response.reasoning == "step 1... step 2..."


def test_client_strips_inline_think_blocks_out_of_text(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _client(monkeypatch, {"content": "<think>let me ponder</think>clean answer"})
    response = llm.complete([], [])
    assert response.text == "clean answer"  # distill/summaries never see think-tags
    assert response.reasoning == "let me ponder"


def test_client_merges_max_tokens_and_extra_body_into_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    llm = _client(
        monkeypatch,
        {"content": "ok"},
        captured,
        max_tokens=1024,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    llm.complete([{"role": "user", "content": "q"}], [])
    assert captured["max_tokens"] == 1024
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_logging_llm_surfaces_reasoning(caplog: pytest.LogCaptureFixture) -> None:
    scripted = LLMResponse(text="final", reasoning="because of X")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        LoggingLLM(FakeLLM([scripted])).complete([], [])
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "[thought: because of X]" in logged and "final" in logged


def test_default_llm_reads_extra_body_and_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VSIGMA_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("VSIGMA_LLM_API_KEY", "k")
    monkeypatch.setenv("VSIGMA_LLM_MAX_TOKENS", "512")
    monkeypatch.setenv("VSIGMA_LLM_EXTRA_BODY", json.dumps({"think": False}))

    llm = default_llm()
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.max_tokens == 512 and llm.extra_body == {"think": False}


def test_default_llm_rejects_malformed_extra_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VSIGMA_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("VSIGMA_LLM_API_KEY", "k")
    monkeypatch.setenv("VSIGMA_LLM_EXTRA_BODY", "{not json")
    with pytest.raises(ValueError, match="EXTRA_BODY"):
        default_llm()
    monkeypatch.setenv("VSIGMA_LLM_EXTRA_BODY", '["a", "list"]')
    with pytest.raises(ValueError, match="JSON object"):
        default_llm()
