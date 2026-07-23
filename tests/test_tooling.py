"""Tests for tool adapters/helpers and the fail-fast guard."""

import pytest
from langchain_core.tools import tool as lc_tool

from vsigma import FakeLLM, Tool, from_langchain, react_node, tool


def test_tool_helper_from_plain_function() -> None:
    @tool
    def clock(args: dict[str, object]) -> str:
        """Return today's date."""
        return "2026-06-28"

    assert isinstance(clock, Tool)
    assert clock.name == "clock"
    assert clock.description == "Return today's date."
    assert clock.run({}) == "2026-06-28"


def test_tool_helper_with_overrides() -> None:
    built = tool(
        lambda args: "ok",
        name="custom",
        description="d",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    assert isinstance(built, Tool)
    assert built.name == "custom"
    assert built.parameters["properties"] == {"x": {"type": "string"}}


def test_from_langchain_adapter() -> None:
    @lc_tool(return_direct=True)
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    adapted = from_langchain(add)
    assert isinstance(adapted, Tool)
    assert adapted.name == "add"
    assert adapted.return_direct is True
    props = adapted.parameters["properties"]
    assert isinstance(props, dict)
    assert "a" in props
    assert adapted.run({"a": 2, "b": 3}) == "5"  # coerced to str observation


def test_tool_decorator_return_direct() -> None:
    @tool(return_direct=True)
    def submit(args: dict[str, object]) -> str:
        return "submitted"

    assert isinstance(submit, Tool)
    assert submit.return_direct is True


def test_react_node_rejects_non_tool_with_clear_error() -> None:
    with pytest.raises(TypeError, match="expects vsigma Tool objects"):
        react_node(FakeLLM([]), [object()])  # type: ignore[list-item]
