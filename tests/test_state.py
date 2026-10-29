"""Tests for state schemas."""

from __future__ import annotations

from typing import get_args, get_origin, get_type_hints

from langchain_core.messages import AnyMessage

from vsigma.state import BaseState, ReActState


def test_base_state_keys() -> None:
    assert "task_id" in BaseState.__annotations__
    assert "run_id" in BaseState.__annotations__
    assert "attempt_count" in BaseState.__annotations__
    assert "status" in BaseState.__annotations__


def test_react_state_annotations() -> None:
    hints = get_type_hints(ReActState, include_extras=True)
    assert "messages" in hints
    args = get_args(hints["messages"])
    assert get_origin(args[0]) is list
    assert get_args(args[0])[0] == AnyMessage
