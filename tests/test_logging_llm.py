"""LoggingLLM — the live tap at the LLM protocol seam, plus its on/off switch."""

import logging
from pathlib import Path

import pytest

from vsigma import (
    FakeLLM,
    LoggingLLM,
    Tool,
    call,
    disable_llm_logging,
    enable_llm_logging,
    make_react_agent,
    say,
)

_LOGGER = "vsigma.llm"


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def test_reply_and_tool_calls_logged_at_info_end_to_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Tool(
        name="clock",
        description="Return the time.",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "12:00",
    )
    llm = LoggingLLM(FakeLLM([call("clock"), say("It is noon.")]))
    agent = make_react_agent(llm, [clock])

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        run = agent.run("what time is it?")

    assert run.status == "done"
    logged = _messages(caplog)
    assert "-> clock(" in logged  # the tool call, with arguments
    assert "It is noon." in logged  # the final reply


def test_prompt_logged_only_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    prompt: list[dict[str, object]] = [{"role": "user", "content": "FULL-PROMPT-TEXT"}]

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        LoggingLLM(FakeLLM([say("hi")])).complete(prompt, [])
    assert "FULL-PROMPT-TEXT" not in _messages(caplog)  # INFO shows replies only

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        LoggingLLM(FakeLLM([say("hi")])).complete(prompt, [])
    assert "FULL-PROMPT-TEXT" in _messages(caplog)  # DEBUG opts into the prompt


def test_response_passes_through_unchanged() -> None:
    response = say("untouched")
    assert LoggingLLM(FakeLLM([response])).complete([], []) is response


def test_long_replies_are_clipped_at_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        LoggingLLM(FakeLLM([say("x" * 1000)])).complete([], [])
    logged = _messages(caplog)
    assert "x" * 399 + "…" in logged and "x" * 1000 not in logged


def test_enable_to_file_and_disable_silences(tmp_path: Path) -> None:
    log_file = tmp_path / "llm.log"
    enable_llm_logging(file=str(log_file))
    try:
        LoggingLLM(FakeLLM([say("while enabled")])).complete([], [])
    finally:
        disable_llm_logging()
    LoggingLLM(FakeLLM([say("after disable")])).complete([], [])

    logged = log_file.read_text()
    assert "while enabled" in logged
    assert "after disable" not in logged


def test_enable_defaults_to_stdout_and_is_idempotent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    enable_llm_logging()
    enable_llm_logging()  # re-enable must not duplicate handlers/lines
    try:
        LoggingLLM(FakeLLM([say("to stdout")])).complete([], [])
    finally:
        disable_llm_logging()
    out = capsys.readouterr().out
    assert out.count("to stdout") == 1
