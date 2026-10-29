"""Tests for Phase 4.11: per-thread concurrency lock (SessionBusy) and pause id (PauseChanged)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from vsigma import (
    Agent,
    FakeLLM,
    Interceptor,
    LLMResponse,
    PauseChanged,
    ReActState,
    SessionBusy,
    SessionPaused,
    Tool,
    ToolCall,
    keep_latest,
    make_guarded_react_agent,
    make_react_agent,
)


class BlockingLLM:
    """An LLM that signals `entered` when called and blocks until `release` is set."""

    def __init__(self, responses: Sequence[LLMResponse] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._responses = list(responses or [LLMResponse(text="blocking reply")])
        self._idx = 0
        self._lock = threading.Lock()

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> LLMResponse:
        self.entered.set()
        self.release.wait(timeout=5.0)
        with self._lock:
            resp = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            return resp


def test_concurrent_send_raises_session_busy_and_first_turn_survives() -> None:
    saver = MemorySaver()
    llm = BlockingLLM([LLMResponse(text="turn 1 reply"), LLMResponse(text="turn 2 reply")])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess = agent.session("s1")

    errors: list[BaseException] = []
    t1_done = threading.Event()

    def run_turn_1() -> None:
        try:
            sess.send("hello 1")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            t1_done.set()

    t1 = threading.Thread(target=run_turn_1)
    t1.start()
    assert llm.entered.wait(timeout=5.0)

    # Second send on the same session fails fast with SessionBusy
    with pytest.raises(SessionBusy) as exc_info:
        sess.send("hello 2")
    assert exc_info.value.thread_id == "s1"

    # Release turn 1 and confirm its messages survive intact
    llm.release.set()
    t1.join(timeout=5.0)
    assert t1_done.is_set()
    assert not errors
    assert sess.dialog == [
        {"role": "user", "content": "hello 1"},
        {"role": "assistant", "content": "turn 1 reply"},
    ]


def test_concurrent_resumes_second_raises_session_busy() -> None:
    saver = MemorySaver()
    fail_tool = Tool(
        name="fail",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "failed",
    )

    # First call fails tool -> escalates; on resume, blocks in BlockingLLM
    class EscalateThenBlockLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.resume_entered = threading.Event()
            self.resume_release = threading.Event()

        def complete(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
        ) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="fail", arguments={})],
                )
            self.resume_entered.set()
            self.resume_release.wait(timeout=5.0)
            return LLMResponse(text="resumed reply")

    llm = EscalateThenBlockLLM()
    agent = make_guarded_react_agent(llm, tools=[fail_tool], checkpointer=saver, limit=1)
    sess = agent.session("s-resume")

    run1 = sess.send("do work")
    assert sess.paused
    assert run1.interrupted

    errors: list[BaseException] = []

    def do_first_resume() -> None:
        try:
            sess.resume("guidance 1")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=do_first_resume)
    t1.start()
    assert llm.resume_entered.wait(timeout=5.0)

    # Both Session.resume and Run.resume raise SessionBusy while the first resume is in flight
    with pytest.raises(SessionBusy) as exc_info:
        sess.resume("guidance 2")
    assert exc_info.value.thread_id == "s-resume"

    with pytest.raises(SessionBusy):
        run1.resume("guidance 3")

    llm.resume_release.set()
    t1.join(timeout=5.0)
    assert not errors
    assert not sess.paused
    assert run1.result == "resumed reply"


def test_end_during_turn_raises_session_busy_and_succeeds_after() -> None:
    saver = MemorySaver()
    llm = BlockingLLM([LLMResponse(text="reply")])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess = agent.session("s-end")

    t1 = threading.Thread(target=lambda: sess.send("in flight"))
    t1.start()
    assert llm.entered.wait(timeout=5.0)

    # Session.end() during a running turn raises SessionBusy (even with abandon=True)
    with pytest.raises(SessionBusy):
        sess.end()
    with pytest.raises(SessionBusy):
        sess.end(abandon=True)

    llm.release.set()
    t1.join(timeout=5.0)

    # After the turn finishes, messages are present, and end() deletes the thread
    assert len(sess.dialog) == 2
    sess.end()
    assert sess.dialog == []

    # Same for Run.end() on a one-shot run
    llm2 = BlockingLLM([LLMResponse(text="one-shot reply")])
    agent2 = make_react_agent(llm2, tools=[], checkpointer=saver)
    run_holder: list[Any] = []

    def start_one_shot() -> None:
        run_holder.append(agent2.run("task", run_id="r-oneshot"))

    t2 = threading.Thread(target=start_one_shot)
    t2.start()
    assert llm2.entered.wait(timeout=5.0)

    probe_run = _run_probe(agent2, "r-oneshot")
    with pytest.raises(SessionBusy):
        probe_run.end()

    llm2.release.set()
    t2.join(timeout=5.0)
    assert run_holder[0].result == "one-shot reply"
    run_holder[0].end()
    with pytest.raises(RuntimeError, match="No checkpoint"):
        _ = run_holder[0].result


def _run_probe(agent: Agent, run_id: str) -> Any:
    from vsigma.run import Run

    return Run(agent, run_id, run_id, None)


def test_subgraph_pause_survives_concurrent_keep_latest() -> None:
    saver = MemorySaver()
    sub_entered = threading.Event()
    sub_release = threading.Event()

    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def sub_ask(state: ReActState) -> dict[str, object]:
        sub_entered.set()
        sub_release.wait(timeout=5.0)
        ans = interrupt({"kind": "clarify", "question": "which wafer?"})
        return {
            "messages": [{"role": "assistant", "content": f"got {ans}"}],
            "status": "done",
            "result": f"got {ans}",
        }

    sub.add_node("ask", sub_ask)
    sub.add_edge(START, "ask")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")

    agent = Agent(parent.compile(checkpointer=saver), Interceptor())
    sess = agent.session("s-sub")

    t1 = threading.Thread(target=lambda: sess.send("go"))
    t1.start()
    assert sub_entered.wait(timeout=5.0)

    # Calling keep_latest while the turn is running raises SessionBusy
    with pytest.raises(SessionBusy):
        keep_latest(agent.graph, "s-sub")

    sub_release.set()
    t1.join(timeout=5.0)

    # Turn is now paused inside the subgraph; keep_latest preserves the subgraph namespace
    assert sess.paused
    keep_latest(agent.graph, "s-sub")
    sub_ns = [ns for ns in saver.storage["s-sub"] if ns != ""]
    assert len(sub_ns) == 1
    assert sess.paused

    # Resuming completes cleanly
    run2 = sess.resume("wafer-7")
    assert run2.status == "done"
    assert run2.result == "got wafer-7"


def test_other_thread_on_same_checkpointer_is_not_blocked() -> None:
    saver = MemorySaver()
    llm1 = BlockingLLM([LLMResponse(text="s1 reply")])

    class RoutingLLM:
        def complete(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
        ) -> LLMResponse:
            last = str(messages[-1].get("content", ""))
            if "block" in last:
                return llm1.complete(messages, tools)
            return LLMResponse(text="fast reply")

    agent = make_react_agent(RoutingLLM(), tools=[], checkpointer=saver)
    s1 = agent.session("s1")
    s2 = agent.session("s2")

    t1 = threading.Thread(target=lambda: s1.send("please block"))
    t1.start()
    assert llm1.entered.wait(timeout=5.0)

    # s2 on the same agent and checkpointer runs without blocking
    run2 = s2.send("quick question")
    assert run2.status == "done"
    assert run2.result == "fast reply"

    llm1.release.set()
    t1.join(timeout=5.0)


def test_two_agents_over_one_checkpointer_share_thread_lock() -> None:
    saver = MemorySaver()
    llm1 = BlockingLLM([LLMResponse(text="from agent 1")])
    llm2 = FakeLLM([LLMResponse(text="from agent 2")])

    agent1 = make_react_agent(llm1, tools=[], checkpointer=saver)
    agent2 = make_react_agent(llm2, tools=[], checkpointer=saver)

    t1 = threading.Thread(target=lambda: agent1.session("shared-sess").send("turn 1"))
    t1.start()
    assert llm1.entered.wait(timeout=5.0)

    # Second Agent instance over the same checkpointer sees the lock on "shared-sess"
    with pytest.raises(SessionBusy) as exc_info:
        agent2.session("shared-sess").send("turn 2")
    assert exc_info.value.thread_id == "shared-sess"

    llm1.release.set()
    t1.join(timeout=5.0)


def test_pause_id_replays_g3_and_rejects_stale_or_finished_resume() -> None:
    saver = MemorySaver()
    fail_tool = Tool(
        name="fail",
        description="always fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "error",
    )
    llm = FakeLLM([
        # Attempt 1 fails -> pauses at Q1
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
        # After Q1 resumed, attempt 2 fails -> pauses at Q2
        LLMResponse(text="", tool_calls=[ToolCall(id="c2", name="fail", arguments={})]),
        # After Q2 resumed, attempt 3 succeeds
        LLMResponse(text="all done"),
    ])
    agent = make_guarded_react_agent(llm, tools=[fail_tool], checkpointer=saver, limit=1)
    sess = agent.session("s-pause-id")

    # Before any pause, interrupt_id is None
    assert sess.interrupt_id is None

    run1 = sess.send("start")
    assert sess.paused
    assert run1.interrupted
    q1 = sess.interrupt
    q1_id = sess.interrupt_id
    assert isinstance(q1_id, str) and len(q1_id) > 0
    assert run1.interrupt_id == q1_id

    # SessionPaused also exposes interrupt_id
    with pytest.raises(SessionPaused) as paused_exc:
        sess.send("accidental send")
    assert paused_exc.value.interrupt == q1
    assert paused_exc.value.interrupt_id == q1_id

    # Client A answers Q1 with q1_id -> run moves on and pauses again at Q2
    sess.resume("client A answer to Q1", interrupt_id=q1_id)
    assert sess.paused
    q2 = sess.interrupt
    q2_id = sess.interrupt_id
    assert isinstance(q2_id, str) and q2_id != q1_id
    assert run1.interrupt_id == q2_id

    # Client B's late answer to Q1 raises PauseChanged carrying Q2
    with pytest.raises(PauseChanged) as changed_exc:
        sess.resume("client B stale answer to Q1", interrupt_id=q1_id)
    assert changed_exc.value.interrupt == q2
    assert changed_exc.value.interrupt_id == q2_id

    # Same via Run.resume directly
    with pytest.raises(PauseChanged) as changed_run_exc:
        run1.resume("client B stale answer to Q1", interrupt_id=q1_id)
    assert changed_run_exc.value.interrupt == q2
    assert changed_run_exc.value.interrupt_id == q2_id

    # Q2 stays pending, and a resume with Q2's id continues to done
    assert sess.paused
    assert sess.interrupt_id == q2_id
    sess.resume("answer to Q2", interrupt_id=q2_id)
    assert not sess.paused
    assert sess.interrupt_id is None
    assert run1.interrupt_id is None
    assert run1.status == "done"
    assert run1.result == "all done"

    # A late resume with an interrupt_id after the run has finished raises
    # PauseChanged carrying no pause.
    with pytest.raises(PauseChanged) as fin_sess_exc:
        sess.resume("too late", interrupt_id=q2_id)
    assert fin_sess_exc.value.interrupt is None
    assert fin_sess_exc.value.interrupt_id is None

    with pytest.raises(PauseChanged) as fin_run_exc:
        run1.resume("too late", interrupt_id=q2_id)
    assert fin_run_exc.value.interrupt is None
    assert fin_run_exc.value.interrupt_id is None


def test_lock_registry_is_empty_after_operations_finish() -> None:
    import vsigma.retention as ret

    saver = MemorySaver()
    llm = FakeLLM([LLMResponse(text=f"ans-{i}") for i in range(11)])
    agent = make_react_agent(llm, tools=[], checkpointer=saver, checkpoints="latest")

    # Run several one-shot runs and end them, plus a session turn and keep_latest
    for i in range(10):
        run = agent.run(f"task-{i}")
        run.end()

    sess = agent.session("sess-clean")
    sess.send("hi")
    keep_latest(agent.graph, "sess-clean")
    sess.end()

    # No per-thread entry remains in the registry once operations finish
    busy_for_saver = ret._BUSY_THREADS.get(saver, set())
    assert len(busy_for_saver) == 0
    assert saver not in ret._BUSY_THREADS


