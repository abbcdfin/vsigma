"""Session handle — multi-round dialog on LangGraph threads (Phase 4.8)."""

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import MemorySaver

from vsigma import (
    FakeLLM,
    Interceptor,
    LLMResponse,
    Run,
    SessionPaused,
    State,
    StaticLLM,
    Tool,
    call,
    make_guarded_react_agent,
    make_react_agent,
    say,
)


@dataclass
class _SpyLLM:
    responses: list[LLMResponse]
    seen: list[list[dict[str, object]]] = field(default_factory=list)
    _cursor: int = 0

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        self.seen.append(list(messages))
        response = self.responses[self._cursor]
        self._cursor += 1
        return response


def test_second_round_carries_the_dialog() -> None:
    spy = _SpyLLM([say("It is blue."), say("Because of Rayleigh scattering.")])
    agent = make_react_agent(spy, [], system="be brief")
    session = agent.session("s1", user_id="alice")

    session.send("What color is the sky?")
    run2 = session.send("Why?")

    assert run2.status == "done"
    prompt2 = spy.seen[1]
    contents = [str(m["content"]) for m in prompt2]
    assert contents[0] == "be brief"  # system prompt prepended fresh, not in messages history
    assert contents[1:] == ["What color is the sky?", "It is blue.", "Why?"]


def test_tool_noise_is_not_carried_across_rounds() -> None:
    clock = Tool(
        name="clock",
        description="time",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "12:00",
    )
    spy = _SpyLLM([call("clock"), say("It is noon."), say("You asked the time.")])
    agent = make_react_agent(spy, [clock])
    session = agent.session()

    session.send("what time is it?")
    session.send("what did I ask?")

    roles = [m["role"] for m in session.dialog]
    assert "tool" not in roles  # tool traffic is episode-local, not dialog
    assert all(not m.get("tool_calls") for m in session.dialog)


def test_stateless_handle_and_recreated_session() -> None:
    """A second handle created for the same session sees identical dialog and pause state."""
    checkpointer: MemorySaver = MemorySaver()
    spy = _SpyLLM([call("missing_tool")])
    agent = make_guarded_react_agent(spy, [], limit=1, checkpointer=checkpointer)

    session1 = agent.session("shared-session")
    run1 = session1.send("task 1")
    assert run1.status == "escalated"
    assert session1.paused

    # Recreate a fresh Session handle for the same session_id
    session2 = agent.session("shared-session")
    assert session2.paused
    assert session2.interrupt == {
        "reason": "attempt limit reached",
        "kind": "escalation",
        "task_id": run1.task_id,
    }
    assert session2.dialog == [{"role": "user", "content": "task 1"}]


def test_send_while_paused_raises_unless_abandon() -> None:
    llm = FakeLLM([call("missing_tool"), say("abandoned and answered")])
    agent = make_guarded_react_agent(llm, [], limit=1)
    session = agent.session("pause-session")

    run1 = session.send("need help")
    assert run1.status == "escalated"
    assert session.paused

    # 1. Calling send without abandon raises SessionPaused
    with pytest.raises(SessionPaused) as exc_info:
        session.send("another request")
    assert exc_info.value.session_id == "pause-session"
    assert exc_info.value.interrupt["kind"] == "escalation"

    # 2. Calling send with abandon=True proceeds
    run2 = session.send("abandon the pause and do this instead", abandon=True)
    assert run2.status == "done"
    assert not session.paused


def test_resume_reuses_paused_run_id() -> None:
    llm = FakeLLM([call("missing_tool"), say("Done with your guidance.")])
    agent = make_guarded_react_agent(llm, [], limit=1)
    session = agent.session("resume-session")

    run1 = session.send("do the thing")
    assert run1.status == "escalated"
    paused_run_id = run1.run_id

    # Resume via session.resume(feedback)
    run2 = session.resume("here is what you needed")
    assert run2.status == "done"
    assert run2.run_id == paused_run_id


def test_system_prompt_updates_across_turns() -> None:
    """Changing Agent.system reaches an existing session at its next turn,
    and each turn's checkpoint records the prompt used.
    """
    spy = _SpyLLM([say("reply 1"), say("reply 2")])
    agent = make_react_agent(spy, [], system="Prompt Version 1")
    session = agent.session("prompt-session")

    session.send("turn 1")
    assert spy.seen[0][0]["content"] == "Prompt Version 1"

    # Update system prompt on the agent
    agent.system = "Prompt Version 2"
    session.send("turn 2")
    assert spy.seen[1][0]["content"] == "Prompt Version 2"

    # State records the system prompt
    assert session.snapshot.values.get("system") == "Prompt Version 2"


def test_channel_is_recorded_in_checkpoint_metadata() -> None:
    spy = _SpyLLM([say("ok")])
    agent = make_react_agent(spy, [])
    session = agent.session("meta-session", user_id="alice")

    run = session.send("hello", channel="slack")
    assert run.status == "done"
    assert all("channel" not in m for m in spy.seen[0])  # model never sees it

    snapshot = session.snapshot
    assert snapshot.metadata is not None
    assert snapshot.metadata.get("channel") == "slack"
    assert snapshot.metadata.get("user_id") == "alice"


def test_sessions_are_isolated_from_each_other() -> None:
    spy = _SpyLLM([say("a"), say("b")])
    agent = make_react_agent(spy, [])
    agent.session("s1").send("secret for session one")
    agent.session("s2").send("hello from session two")

    contents = [str(m["content"]) for m in spy.seen[1]]
    assert all("secret" not in c for c in contents)


def test_dialog_property_and_fresh_session_id() -> None:
    agent = make_react_agent(StaticLLM("hi"), [])
    session = agent.session()
    assert session.session_id  # generated
    session.send("hello")
    assert session.dialog == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_checkpoint_facts_metadata_tagging_and_exit_durability() -> None:
    """Verify §H.1 & §H.5: Each run's checkpoints carry its run_id tag,
    and durability='exit' writes one checkpoint per turn.
    """
    saver = MemorySaver()
    spy = _SpyLLM([say("turn 1 done"), say("turn 2 done")])
    agent = make_react_agent(spy, [], checkpointer=saver, durability="exit")

    session = agent.session("exit-durability-session")
    run1 = session.send("first message")
    run2 = session.send("second message")

    cfg = {"configurable": {"thread_id": "exit-durability-session"}}
    checkpoints = list(saver.list(cast("Any", cfg)))

    # Two turns with durability='exit' write 2 checkpoints
    assert len(checkpoints) == 2

    # Verify run_id tagging in checkpoint metadata (§H.1)
    run_ids = [cp.metadata.get("run_id") for cp in checkpoints]
    assert run1.run_id in run_ids
    assert run2.run_id in run_ids


def test_turn_result_does_not_leak_into_next_turn() -> None:
    # Turn 1 finishes with a result; turn 2 escalates.
    from vsigma import InMemoryStore

    recorded_states: list[State] = []
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        run_end=lambda state, mem: recorded_states.append(dict(state)),
    )
    llm = FakeLLM([say("answer one"), call("missing_tool")])
    agent = make_guarded_react_agent(llm, [], limit=1, interceptor=itc)
    session = agent.session("leak-session")

    run1 = session.send("first task")
    assert run1.status == "done"
    assert run1.result == "answer one"

    run2 = session.send("second task")
    assert run2.status == "escalated"
    assert run2.result is None
    assert recorded_states[-1]["result"] is None


def test_agent_run_on_paused_session_raises_session_paused() -> None:
    llm = FakeLLM([call("missing_tool"), say("done")])
    agent = make_guarded_react_agent(llm, [], limit=1)
    run1 = agent.run("first task", session_id="pause-direct-session")
    assert run1.status == "escalated"

    with pytest.raises(SessionPaused) as exc_info:
        agent.run("second task", session_id="pause-direct-session")
    assert exc_info.value.session_id == "pause-direct-session"
    assert exc_info.value.interrupt["kind"] == "escalation"

    # With abandon=True it proceeds
    run2 = agent.run("second task", session_id="pause-direct-session", abandon=True)
    assert run2.status == "done"


def test_resume_preserves_metadata_tags() -> None:
    saver = MemorySaver()
    llm = FakeLLM([call("missing_tool"), say("done")])
    agent = make_guarded_react_agent(llm, [], limit=1, checkpointer=saver)
    session = agent.session("tag-session", user_id="alice")

    run1 = session.send("task", channel="slack")
    assert run1.status == "escalated"

    run2 = session.resume("feedback")
    assert run2.status == "done"

    checkpoints = list(
        saver.list(
            cast("Any", {"configurable": {"thread_id": "tag-session"}}),
            filter={"user_id": "alice"},
        )
    )
    assert len(checkpoints) >= 2
    for cp in checkpoints:
        assert cp.metadata.get("user_id") == "alice"
        assert cp.metadata.get("channel") == "slack"


def test_run_reports_its_own_run_not_threads_newest() -> None:
    # Turn 1 finishes with result "one"; turn 2 escalates.
    llm = FakeLLM([say("answer one"), call("missing_tool")])
    agent = make_guarded_react_agent(llm, [], limit=1)
    session = agent.session("own-run-session")

    run1 = session.send("first task")
    assert run1.status == "done"
    assert run1.result == "answer one"

    run2 = session.send("second task")
    assert run2.status == "escalated"
    assert run2.interrupted

    # run1 must keep its own status and result!
    assert run1.status == "done"
    assert run1.result == "answer one"
    assert not run1.interrupted


def test_abandoned_run_refuses_resume() -> None:
    llm = FakeLLM([call("missing_tool"), say("new work done")])
    agent = make_guarded_react_agent(llm, [], limit=1)
    session = agent.session("abandon-resume-session")

    run1 = session.send("first task")
    assert run1.status == "escalated"
    assert run1.interrupted

    # Abandon turn 1 and run turn 2
    run2 = session.send("second task", abandon=True)
    assert run2.status == "done"

    # run1 stays "escalated", but is no longer the thread's active paused turn
    assert run1.status == "escalated"
    assert not run1.interrupted
    with pytest.raises(RuntimeError):
        run1.resume("late feedback")


def test_session_resume_inherits_agent_recursion_limit() -> None:
    llm = FakeLLM([call("missing_tool"), say("resumed")])
    agent = make_guarded_react_agent(llm, [], limit=1, recursion_limit=42)
    session = agent.session("resume-limit-session")

    run1 = session.send("task")
    assert run1.status == "escalated"
    assert run1.config.get("recursion_limit") == 42

    run2 = session.resume("feedback")
    assert run2.status == "done"
    assert run2.config.get("recursion_limit") == 42


def test_run_snapshot_raises_when_no_checkpoint_matches_run_id() -> None:
    agent = make_react_agent(StaticLLM("ok"), [])
    run = Run(agent, "nonexistent-run-id", "task-id", session_id="s1")
    with pytest.raises(RuntimeError, match="No checkpoint found for run 'nonexistent-run-id'"):
        _ = run.snapshot


