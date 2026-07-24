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
import re
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
    raw_arguments: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """A single model turn: free text and/or a batch of tool calls.

    ``reasoning`` is a side channel for thinking models: the deliberation the
    provider surfaced (separate field or inline ``<think>`` block). It is never
    part of ``text`` and must never be echoed back into the message history —
    providers expect reasoning dropped from context. Consumers that want it
    read it explicitly (`LoggingLLM` logs it).
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""


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


#: Inline reasoning serialization: a <think> block, tolerating truncation
#: (an unterminated block strips to end of text — passing the fragment through
#: would leak deliberation exactly when the model misbehaved most).
_THINK_BLOCK = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL)


def _split_reasoning(content: str) -> tuple[str, str]:
    """Split inline ``<think>`` blocks out of content: (clean text, reasoning)."""
    blocks = [block.strip() for block in _THINK_BLOCK.findall(content)]
    if not blocks:
        return content, ""
    return _THINK_BLOCK.sub("", content).strip(), "\n\n".join(b for b in blocks if b)


@dataclass
class OpenAICompatibleLLM:
    """Client for any OpenAI-compatible /chat/completions endpoint.

    Thinking models are handled at this seam so nothing above it needs to know
    they exist: reasoning (a separate ``reasoning_content``/``reasoning`` field,
    or inline ``<think>`` blocks) lands on `LLMResponse.reasoning`, and ``text``
    is always clean of it. ``extra_body`` passes provider-specific switches
    through verbatim (e.g. ``{"chat_template_kwargs": {"enable_thinking":
    False}}`` on vLLM/Qwen3, ``{"think": False}`` on Ollama) — deliberately a
    passthrough, not a normalized flag, so no client release chases provider
    spellings. ``max_tokens`` is the hard bound on a runaway turn.
    """

    model: str
    base_url: str
    api_key: str
    timeout: float = 60.0
    max_tokens: int | None = None
    temperature: float | None = None
    tool_choice: str | dict[str, object] | None = None
    extra_body: dict[str, object] = field(default_factory=dict)

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        payload: dict[str, object] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = self.tool_choice if self.tool_choice is not None else "auto"
        elif self.tool_choice is not None:
            payload["tool_choice"] = self.tool_choice
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        payload.update(self.extra_body)  # merged last: the operator's word wins
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
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
                error = None
            except json.JSONDecodeError as exc:
                args = {}
                clipped_raw = raw_args[:200] + "…" if len(raw_args) > 200 else raw_args
                error = (
                    f"error: Malformed tool arguments JSON: {exc}. "
                    f"Raw argument string emitted: {clipped_raw}"
                )
            calls.append(
                ToolCall(
                    id=str(raw.get("id", "")),
                    name=str(fn["name"]),
                    arguments=args,
                    raw_arguments=raw_args,
                    error=error,
                )
            )
        text, inline = _split_reasoning(str(message.get("content") or ""))
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        return LLMResponse(text=text, tool_calls=calls, reasoning=reasoning or inline)


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
        parts = [f"[thought: {_clip(response.reasoning)}]"] if response.reasoning else []
        parts += [_clip(response.text)] if response.text else []
        parts += [
            f"-> {tc.name}({_clip(json.dumps(tc.arguments, default=str), 200)})"
            for tc in response.tool_calls
        ]
        if response.reasoning and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("reasoning (full): %s", response.reasoning)
        self.logger.info(
            "reply (%.2fs, %d messages in): %s",
            elapsed,
            len(messages),
            " | ".join(parts) or "(empty)",
        )
        return response


def default_llm() -> LLM | None:
    """Build an `OpenAICompatibleLLM` from env vars, or None if unconfigured.

    Reads ``VSIGMA_LLM_BASE_URL``, ``VSIGMA_LLM_API_KEY`` and the optional
    ``VSIGMA_LLM_MODEL``, ``VSIGMA_LLM_TIMEOUT``, ``VSIGMA_LLM_MAX_TOKENS`` and
    ``VSIGMA_LLM_EXTRA_BODY`` (a JSON object merged into every request — the
    hook for provider-specific switches like disabling thinking). Returns None
    when no remote is configured, so callers can fall back to a `FakeLLM`.
    Raises ValueError when ``VSIGMA_LLM_EXTRA_BODY`` is set but not a JSON
    object — a misspelled thinking switch must not silently vanish.
    """
    base_url = os.getenv("VSIGMA_LLM_BASE_URL")
    api_key = os.getenv("VSIGMA_LLM_API_KEY")
    if not (base_url and api_key):
        return None
    raw_extra = os.getenv("VSIGMA_LLM_EXTRA_BODY", "")
    extra_body: dict[str, object] = {}
    if raw_extra:
        try:
            extra_body = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            raise ValueError(f"VSIGMA_LLM_EXTRA_BODY is not valid JSON: {exc}") from exc
        if not isinstance(extra_body, dict):
            raise ValueError("VSIGMA_LLM_EXTRA_BODY must be a JSON object")
    raw_max = os.getenv("VSIGMA_LLM_MAX_TOKENS", "")
    raw_temp = os.getenv("VSIGMA_LLM_TEMPERATURE", "")
    raw_choice = os.getenv("VSIGMA_LLM_TOOL_CHOICE", "")
    tool_choice: str | dict[str, object] | None = None
    if raw_choice:
        if raw_choice.startswith("{"):
            try:
                tool_choice = json.loads(raw_choice)
            except json.JSONDecodeError as exc:
                raise ValueError(f"VSIGMA_LLM_TOOL_CHOICE is not valid JSON: {exc}") from exc
        else:
            tool_choice = raw_choice

    return OpenAICompatibleLLM(
        model=os.getenv("VSIGMA_LLM_MODEL", "gpt-4o-mini"),
        base_url=base_url,
        api_key=api_key,
        timeout=float(os.getenv("VSIGMA_LLM_TIMEOUT", "60.0")),
        max_tokens=int(raw_max) if raw_max else None,
        temperature=float(raw_temp) if raw_temp else None,
        tool_choice=tool_choice,
        extra_body=extra_body,
    )
