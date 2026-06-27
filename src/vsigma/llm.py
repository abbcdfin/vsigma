"""The LLM seam.

The agent depends only on the `LLM` protocol. `FakeLLM` drives offline,
deterministic tests and the demo; `OpenAICompatibleLLM` talks to any
OpenAI-compatible chat-completions endpoint (configured via constructor or env).
Swapping models is configuration, never a code change.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

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
        return OpenAICompatibleLLM(model=model, base_url=base_url, api_key=api_key)
    return None
