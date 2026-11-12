"""Test configuration and shared fixtures."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient VSIGMA_LLM_* environment variables before each test.

    Keeps the test suite strictly offline unless a test explicitly sets them
    using `monkeypatch.setenv`.
    """
    for key in list(os.environ):
        if key.startswith("VSIGMA_LLM_"):
            monkeypatch.delenv(key, raising=False)
