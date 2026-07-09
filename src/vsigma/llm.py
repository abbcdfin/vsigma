"""The LLM seam.

The agent depends only on the `LLM` protocol. `FakeLLM` drives offline,
deterministic tests and the demo; `OpenAICompatibleLLM` talks to any
OpenAI-compatible chat-completions endpoint (configured via constructor or env).
Swapping models is configuration, never a code change.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

import httpx


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class LLMResponse:
    """A single model turn: free text and/or a batch of tool calls."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def say(text: str) -> LLMResponse:
    """Build a scripted final-answer turn (no tool calls). For use with `FakeLLM`."""
    return LLMResponse(text=text)


def call(name: str, **arguments: object) -> LLMResponse:
    """Build a scripted single tool-call turn (id auto-generated). For use with `FakeLLM`."""
    return LLMResponse(
        tool_calls=[ToolCall(id=uuid4().hex[:8], name=name, arguments=dict(arguments))]
    )


class LLM(Protocol):
    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse: ...


@dataclass
class FakeLLM:
    """Returns scripted responses in order. For tests and the offline demo."""

    responses: list[LLMResponse]
    _cursor: int = 0

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        if self._cursor >= len(self.responses):
            raise RuntimeError("FakeLLM ran out of scripted responses")
        response = self.responses[self._cursor]
        self._cursor += 1
        return response


@dataclass
class StaticLLM:
    """An LLM stub that always returns the same final answer.

    For smoke-testing that a graph wires up and terminates, without scripting each
    step like `FakeLLM`.
    """

    text: str = "done"

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        return LLMResponse(text=self.text)


@dataclass
class OpenAICompatibleLLM:
    """Client for any OpenAI-compatible /chat/completions endpoint."""

    model: str
    base_url: str
    api_key: str
    timeout: float = 60.0

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        payload: dict[str, object] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw["function"]
            calls.append(
                ToolCall(
                    id=str(raw.get("id", "")),
                    name=str(fn["name"]),
                    arguments=json.loads(fn.get("arguments") or "{}"),
                )
            )
        return LLMResponse(text=message.get("content") or "", tool_calls=calls)


#: Logger for `LoggingLLM`. Library rule: silent by default (NullHandler) —
#: output only when the consumer opts in via `enable_llm_logging()` or their
#: own logging config on "vsigma.llm".
_LLM_LOGGER_NAME = "vsigma.llm"
_llm_logger = logging.getLogger(_LLM_LOGGER_NAME)
_llm_logger.addHandler(logging.NullHandler())
_managed_handlers: list[logging.Handler] = []


def enable_llm_logging(level: int = logging.INFO, *, file: str | None = None) -> None:
    """Turn on LLM-call logging: replies at INFO, full prompts at DEBUG.

    Attaches a handler to the ``vsigma.llm`` logger — stdout by default, or a
    file when ``file`` is given. Idempotent: re-enabling replaces the handlers
    this function previously attached (handlers you configured yourself are
    never touched). Pass ``logging.DEBUG`` to also see the full prompt.
    """
    disable_llm_logging()
    handler: logging.Handler = (
        logging.FileHandler(file) if file else logging.StreamHandler(sys.stdout)
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    _llm_logger.addHandler(handler)
    _managed_handlers.append(handler)
    _llm_logger.setLevel(level)
    _llm_logger.propagate = False  # don't double-print through the root logger


def disable_llm_logging() -> None:
    """Remove the handlers `enable_llm_logging` attached; back to silent."""
    for handler in _managed_handlers:
        _llm_logger.removeHandler(handler)
        handler.close()
    _managed_handlers.clear()
    _llm_logger.setLevel(logging.NOTSET)
    _llm_logger.propagate = True


def _clip(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class LoggingLLM:
    """Augment any `LLM` with call logging — the live tap at the LLM seam.

    Wraps ``inner`` and delegates `complete()` unchanged, logging each call to
    the ``vsigma.llm`` logger: the reply (clipped text and/or tool calls) with
    latency at INFO; the full prompt at DEBUG (opt-in — it can be large).
    Composes with any LLM (including `FakeLLM`) and any template; silent until
    `enable_llm_logging()` (or your own logging config) turns output on.
    """

    inner: LLM
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger(_LLM_LOGGER_NAME)
    )

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                "prompt (%d messages, %d tools): %s",
                len(messages),
                len(tools),
                json.dumps(messages, default=str),
            )
        started = time.perf_counter()
        response = self.inner.complete(messages, tools)
        elapsed = time.perf_counter() - started
        parts = [_clip(response.text)] if response.text else []
        parts += [
            f"-> {tc.name}({_clip(json.dumps(tc.arguments, default=str), 200)})"
            for tc in response.tool_calls
        ]
        self.logger.info(
            "reply (%.2fs, %d messages in): %s",
            elapsed,
            len(messages),
            " | ".join(parts) or "(empty)",
        )
        return response


def default_llm() -> LLM | None:
    """Build an `OpenAICompatibleLLM` from env vars, or None if unconfigured.

    Reads ``VSIGMA_LLM_BASE_URL``, ``VSIGMA_LLM_API_KEY`` and
    (optional) ``VSIGMA_LLM_MODEL``. Returns None when no remote is configured,
    so callers can fall back to a `FakeLLM`.
    """
    base_url = os.getenv("VSIGMA_LLM_BASE_URL")
    api_key = os.getenv("VSIGMA_LLM_API_KEY")
    if base_url and api_key:
        model = os.getenv("VSIGMA_LLM_MODEL", "gpt-4o-mini")
        timeout = float(os.getenv("VSIGMA_LLM_TIMEOUT", "60.0"))
        return OpenAICompatibleLLM(model=model, base_url=base_url, api_key=api_key, timeout=timeout)
    return None
