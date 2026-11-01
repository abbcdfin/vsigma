"""Tests for Phase 4.15: Run Context — One Agent, Many Sessions."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from vsigma import (
    Agent,
    ContextManager,
    FakeLLM,
    InMemoryStore,
    Interceptor,
    NoRunContext,
    PersistentStore,
    ReActState,
    SessionBusy,
    State,
    Tool,
    add_wrapped,
    call,
    make_guarded_react_agent,
    make_react_agent,
    run_context,
    say,
)
from vsigma.episodes import EPISODE_STATUS_NS
from vsigma.threads import thread_lock


def _make_sqlite_saver() -> SqliteSaver:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_run_context_reaches_all_execution_points(saver_factory: Any) -> None:
    """1. Object reaches:
    - a tool,
    - a hand-built node,
    - a router,
    - a ContextManager (e.g. prompt builder),
    - a subgraph executed as a node,
    - and the step after a resume.
    Cover both InMemorySaver and SqliteSaver.
    """
    seen_points: list[tuple[Any, ...]] = []

    class CustomPromptManager(ContextManager):
        def assemble(self, state: State, memory: Any = None) -> dict[str, Any]:
            seen_points.append(("context_manager", run_context()))
            return dict(state)

    def tool_fn(args: dict[str, Any]) -> str:
        ctx = run_context()
        seen_points.append(("tool", ctx))
        return f"tool_saw:{ctx}"

    sample_tool = Tool(
        name="sample_tool",
        description="A sample tool",
        parameters={"type": "object", "properties": {}},
        func=tool_fn,
    )

    # Subgraph executed as a node
    sub_builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def subgraph_node(state: Any) -> dict[str, Any]:
        seen_points.append(("subgraph", run_context()))
        return {}

    sub_builder.add_node("sub_step", subgraph_node)
    sub_builder.add_edge(START, "sub_step")
    sub_builder.add_edge("sub_step", END)
    sub_compiled = sub_builder.compile()

    # Main graph
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def hand_built_node(state: Any) -> dict[str, Any]:
        seen_points.append(("hand_built_node", run_context()))
        sample_tool.run({})
        return {}

    def router_fn(state: Any) -> str:
        seen_points.append(("router", run_context()))
        return "subgraph_step"

    def pause_node(state: Any) -> dict[str, Any]:
        val = interrupt("need_approval")
        seen_points.append(("after_resume", run_context(), val))
        return {"status": "done", "result": f"resumed:{val}"}

    cm = CustomPromptManager()
    interceptor = Interceptor(context=cm)

    add_wrapped(builder, "hb_node", hand_built_node, interceptor)
    builder.add_node("subgraph_step", sub_compiled)
    builder.add_node("pause_step", pause_node)

    builder.add_edge(START, "hb_node")
    builder.add_conditional_edges("hb_node", router_fn, {"subgraph_step": "subgraph_step"})
    builder.add_edge("subgraph_step", "pause_step")
    builder.add_edge("pause_step", END)

    saver = saver_factory()
    graph = builder.compile(checkpointer=saver)
    agent = Agent(graph, interceptor=interceptor)

    session = agent.session("sess_all_points", run_context="MY_CONTEXT_OBJ")
    _ = session.send("start")

    assert session.paused is True
    assert session.interrupt == "need_approval"

    # Resume the session
    session.resume("approved")
    assert session.paused is False

    labels = [p[0] for p in seen_points]
    assert "context_manager" in labels
    assert "hand_built_node" in labels
    assert "router" in labels
    assert "subgraph" in labels
    assert "tool" in labels
    assert "after_resume" in labels

    for item in seen_points:
        if item[0] == "after_resume":
            assert item[1] == "MY_CONTEXT_OBJ"
            assert item[2] == "approved"
        else:
            assert item[1] == "MY_CONTEXT_OBJ"


def test_concurrent_sessions_isolated_with_barrier() -> None:
    """2. Two concurrent sessions on one Agent, synchronized with a threading.Barrier
    inside a tool so both are in the tool together: each sees only its own object.
    """
    barrier = threading.Barrier(2)
    seen_in_tool: dict[str, str] = {}
    tool_exceptions: list[Exception] = []

    def barrier_tool_fn(args: dict[str, Any]) -> str:
        try:
            ctx = run_context()
            # Wait for both sessions to be in the tool concurrently
            barrier.wait(timeout=5)
            seen_in_tool[ctx["user"]] = ctx["secret"]
            return f"ok:{ctx['user']}"
        except Exception as exc:
            tool_exceptions.append(exc)
            raise

    sync_tool = Tool(
        name="sync_tool",
        description="barrier sync tool",
        parameters={"type": "object", "properties": {}},
        func=barrier_tool_fn,
    )

    class ConcurrentLLM:
        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> Any:
            last_msg = messages[-1] if messages else {}
            role = (
                last_msg.get("role")
                if isinstance(last_msg, dict)
                else getattr(last_msg, "type", None)
            )
            if role == "tool":
                return say("done")
            return call("sync_tool")

    saver = MemorySaver()
    agent = make_react_agent(ConcurrentLLM(), [sync_tool], checkpointer=saver)

    def run_session_turn(user_name: str, secret: str) -> None:
        ctx = {"user": user_name, "secret": secret}
        sess = agent.session(f"sess_{user_name}", run_context=ctx)
        sess.send("execute turn")

    t1 = threading.Thread(target=run_session_turn, args=("alice", "alice_secret_123"))
    t2 = threading.Thread(target=run_session_turn, args=("bob", "bob_secret_456"))

    t1.start()
    t2.start()

    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not tool_exceptions
    assert seen_in_tool["alice"] == "alice_secret_123"
    assert seen_in_tool["bob"] == "bob_secret_456"


def test_tool_reading_no_context_fails_closed() -> None:
    """3. Tool reading with no object raises NoRunContext: the run stops, the LLM
    is called once, no tool message is added to state, and the episode's recorded
    status is 'failed'.
    """
    def need_context_tool_fn(args: dict[str, Any]) -> str:
        ctx = run_context()
        return f"got:{ctx}"

    ctx_tool = Tool(
        name="need_context_tool",
        description="needs context",
        parameters={"type": "object", "properties": {}},
        func=need_context_tool_fn,
    )

    # FakeLLM provides tool call, then a final message that should never be reached
    llm = FakeLLM([call("need_context_tool"), say("final message")])
    store = InMemoryStore()
    saver = MemorySaver()
    agent = make_react_agent(
        llm,
        [ctx_tool],
        checkpointer=saver,
        interceptor=Interceptor(memory=store),
    )

    with pytest.raises(NoRunContext):
        # Run without run_context
        agent.run(
            "run without context", session_id="sess_no_ctx", run_id="run_tool_ctx_fail"
        )

    # LLM called only once
    assert llm._cursor == 1

    # No tool message added to thread state
    state = agent.graph.get_state(cast("Any", {"configurable": {"thread_id": "sess_no_ctx"}}))
    messages = state.values.get("messages", [])
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "type", None)
        assert role != "tool", f"Tool message should not be present: {msg}"

    # Episode status in memory is 'failed'
    status_val = store.read(EPISODE_STATUS_NS, "run_tool_ctx_fail")
    assert status_val == "failed"


def test_run_context_raises_outside_run_and_on_unmanaged_thread() -> None:
    """4. run_context() outside a run raises NoRunContext; run_context() on a
    threading.Thread started inside a tool raises NoRunContext.
    """
    # Outside a run
    with pytest.raises(NoRunContext):
        run_context()

    # On unmanaged threading.Thread started inside a tool
    spawned_thread_exceptions: list[Exception] = []

    def spawn_tool_fn(args: dict[str, Any]) -> str:
        # On the managed runner thread, run_context() works
        assert run_context() == "active_context"

        def child_thread_worker() -> None:
            try:
                run_context()
            except Exception as exc:
                spawned_thread_exceptions.append(exc)

        child = threading.Thread(target=child_thread_worker)
        child.start()
        child.join(timeout=5)
        return "ok"

    spawn_tool = Tool(
        name="spawn_tool",
        description="spawns thread",
        parameters={"type": "object", "properties": {}},
        func=spawn_tool_fn,
    )

    llm = FakeLLM([call("spawn_tool"), say("done")])
    saver = MemorySaver()
    agent = make_react_agent(llm, [spawn_tool], checkpointer=saver)

    agent.run("test thread", run_context="active_context")

    assert len(spawned_thread_exceptions) == 1
    assert isinstance(spawned_thread_exceptions[0], NoRunContext)


def test_resume_without_context_refused_and_renewed_succeeds() -> None:
    """5. Resume with no object: resume(value) and session.resume(value) on a
    paused session that ran with an object raise NoRunContext; the session remains
    paused (snapshot interrupts unchanged); a new session = agent.session(id, run_context=renewed)
    finishes the turn and sees the renewed object.
    """
    post_resume_seen: list[Any] = []

    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def init_step(state: Any) -> dict[str, Any]:
        return {}

    def pause_step(state: Any) -> dict[str, Any]:
        val = interrupt("need_token")
        post_resume_seen.append(run_context())
        return {"status": "done", "result": f"token:{val}"}

    builder.add_node("init", init_step)
    builder.add_node("pause", pause_step)
    builder.add_edge(START, "init")
    builder.add_edge("init", "pause")
    builder.add_edge("pause", END)

    saver = MemorySaver()
    graph = builder.compile(checkpointer=saver)
    agent = Agent(graph, interceptor=Interceptor())

    session = agent.session("sess_resume_test", run_context="initial_ctx")
    run = session.send("start")

    assert session.paused is True
    orig_interrupts = session.snapshot.interrupts
    assert len(orig_interrupts) == 1
    assert orig_interrupts[0].value == "need_token"

    # Calling run.resume with run.run_context = None raises NoRunContext
    run.run_context = None
    with pytest.raises(NoRunContext):
        run.resume("token_val")

    # Session remains paused; interrupts untouched
    assert session.paused is True
    assert session.snapshot.interrupts == orig_interrupts

    # Calling session.resume with no run_context on the session handle raises NoRunContext
    session_no_ctx = agent.session("sess_resume_test")
    with pytest.raises(NoRunContext):
        session_no_ctx.resume("token_val")

    # Session still paused
    assert session.paused is True
    assert session.snapshot.interrupts == orig_interrupts

    # Renewed handle finishes turn and sees the renewed object
    session_renewed = agent.session("sess_resume_test", run_context="renewed_ctx")
    final_run = session_renewed.resume("valid_token")

    assert session_renewed.paused is False
    assert final_run.status == "done"
    assert post_resume_seen == ["renewed_ctx"]


def test_refusing_turns_without_context() -> None:
    """6. Refusing turns:
    - session.send(task) with no object on a session that ran with one raises NoRunContext.
    - session.send(task, abandon=True) with no object on a paused session that ran with one
      raises NoRunContext: no new episode is recorded, and the old episode's status remains
      'escalated' (not 'abandoned').
    """
    store = InMemoryStore()
    saver = MemorySaver()

    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def step(state: Any) -> dict[str, Any]:
        return {"status": "done"}

    def pause_step(state: Any) -> dict[str, Any]:
        interrupt("hold")
        return {"status": "done"}

    builder.add_node("step", step)
    builder.add_node("pause", pause_step)
    builder.add_edge(START, "step")
    builder.add_edge("step", END)

    graph = builder.compile(checkpointer=saver)
    agent = Agent(graph, interceptor=Interceptor(memory=store))

    # Part A: session.send on a session that finished turn 1 with context
    s1 = agent.session("sess_part_a", run_context="ctx_a")
    s1.send("turn 1")

    # Rehydrate session without context
    s1_unauthed = agent.session("sess_part_a")
    with pytest.raises(NoRunContext):
        s1_unauthed.send("turn 2")

    # Part B: session.send(abandon=True) on paused session
    paused_builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    paused_builder.add_node("pause", pause_step)
    paused_builder.add_edge(START, "pause")
    paused_builder.add_edge("pause", END)

    paused_graph = paused_builder.compile(checkpointer=saver)
    paused_agent = Agent(paused_graph, interceptor=Interceptor(memory=store))

    s2 = paused_agent.session("sess_part_b", run_context="ctx_b")
    r2 = s2.send("pause turn")
    assert s2.paused is True
    old_run_id = r2.run_id

    # Verify old episode status is 'escalated'
    status_item = store.read(EPISODE_STATUS_NS, old_run_id)
    assert status_item == "escalated"

    episodes_before = [
        ns for ns in store.namespaces() if len(ns) >= 2 and ns[0] == "episodes"
    ]

    # Rehydrate session without context and call send with abandon=True
    s2_unauthed = paused_agent.session("sess_part_b")
    with pytest.raises(NoRunContext):
        s2_unauthed.send("abandon turn", abandon=True)

    # Verify no new episode was recorded
    episodes_after = [
        ns for ns in store.namespaces() if len(ns) >= 2 and ns[0] == "episodes"
    ]
    assert len(episodes_after) == len(episodes_before)

    # Verify old episode's status remains 'escalated', NOT 'abandoned'
    status_item_after = store.read(EPISODE_STATUS_NS, old_run_id)
    assert status_item_after == "escalated"


def test_guard_holds_after_checkpoints_latest_pruning() -> None:
    """7. The guard holds after checkpoints='latest' prunes old checkpoints:
    run, pause, prune, then resume with no object still raises NoRunContext.
    """
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def pause_step(state: Any) -> dict[str, Any]:
        interrupt("pause_latest")
        return {"status": "done"}

    builder.add_node("pause", pause_step)
    builder.add_edge(START, "pause")
    builder.add_edge("pause", END)

    saver = _make_sqlite_saver()
    graph = builder.compile(checkpointer=saver)
    agent = Agent(graph, interceptor=Interceptor(), checkpoints="latest")

    sess = agent.session("sess_latest", run_context="my_ctx")
    sess.send("start")
    assert sess.paused is True

    # Rehydrate without context
    sess_no_ctx = agent.session("sess_latest")
    with pytest.raises(NoRunContext):
        sess_no_ctx.resume("resume_val")


def test_secret_hygiene_not_persisted_to_disk(tmp_path: Path) -> None:
    """8. Secret hygiene: an object holding a secret string runs through turns,
    pause, resume and maintain; the secret is not present anywhere in any SQLite
    saver or PersistentStore file or -wal.
    """
    secret = "SUPER_SECRET_TOKEN_XYZ_9876543210"

    class SessionServices:
        def __init__(self, token: str) -> None:
            self.token = token

    sqlite_db_path = tmp_path / "checkpoints.db"
    store_db_path = tmp_path / "memory.db"

    conn = sqlite3.connect(str(sqlite_db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()

    store = PersistentStore(str(store_db_path))

    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def step1(state: Any) -> dict[str, Any]:
        services: SessionServices = run_context()
        # Use token in memory, but don't write it to state or memory store
        assert services.token == secret
        return {"result": "step1_done"}

    def pause_step(state: Any) -> dict[str, Any]:
        services: SessionServices = run_context()
        assert services.token == secret
        interrupt("approval")
        services_resumed: SessionServices = run_context()
        assert services_resumed.token == secret
        return {"status": "done", "result": "finished"}

    builder.add_node("step1", step1)
    builder.add_node("pause_step", pause_step)
    builder.add_edge(START, "step1")
    builder.add_edge("step1", "pause_step")
    builder.add_edge("pause_step", END)

    graph = builder.compile(checkpointer=saver)
    agent = Agent(graph, interceptor=Interceptor(memory=store))

    sess = agent.session("sess_secret", run_context=SessionServices(secret))
    sess.send("run turn 1")

    assert sess.paused is True
    sess_renewed = agent.session("sess_secret", run_context=SessionServices(secret))
    sess_renewed.resume("approved")
    assert sess_renewed.paused is False

    # Run maintenance
    agent.maintain()

    # Close store and connection to flush WAL
    store.close()
    conn.close()

    secret_bytes = secret.encode("utf-8")

    # Check all files in tmp_path
    for p in tmp_path.iterdir():
        content = p.read_bytes()
        assert secret_bytes not in content, f"Secret leaked into file {p.name}"


def test_one_agent_persistent_store_maintain_and_busy_retry(tmp_path: Path) -> None:
    """9. One Agent on persistent store with multiple open sessions: maintain()
    does not raise 'several live agents on store'; while maintain holds a thread
    lock, session.send raises SessionBusy and does not corrupt state; send succeeds
    once maintain releases the lock.
    """
    store_db_path = tmp_path / "maintain_test.db"
    store = PersistentStore(str(store_db_path))
    saver = _make_sqlite_saver()

    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def work_node(state: Any) -> dict[str, Any]:
        return {"status": "done", "result": "work_ok"}

    builder.add_node("work", work_node)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)

    graph = builder.compile(checkpointer=saver)
    agent = Agent(graph, interceptor=Interceptor(memory=store))

    s1 = agent.session("sess_m1", run_context={"id": 1})
    s2 = agent.session("sess_m2", run_context={"id": 2})

    r1 = s1.send("task 1")
    r2 = s2.send("task 2")
    assert r1.status == "done"
    assert r2.status == "done"

    # maintain() does not raise "several live agents on store"
    report = agent.maintain()
    assert report is not None

    # While thread lock is held, session.send raises SessionBusy
    with thread_lock(agent.graph.checkpointer, "sess_m1"):
        with pytest.raises(SessionBusy):
            s1.send("task 1 during lock")

    # After thread lock is released, session.send succeeds without state corruption
    r1_retry = s1.send("task 1 retry")
    assert r1_retry.status == "done"
    assert r1_retry.result == "work_ok"

    store.close()


def test_backward_compatibility_no_run_context() -> None:
    """10. Backward compatibility: an agent run without run_context continues
    to run, pause and resume unchanged.
    """
    def dummy_tool(args: dict[str, Any]) -> str:
        return "tool_ok"

    t = Tool(
        name="dummy_tool",
        description="dummy",
        parameters={"type": "object", "properties": {}},
        func=dummy_tool,
    )

    llm = FakeLLM([call("dummy_tool"), say("paused"), say("finished")])
    saver = MemorySaver()
    agent = make_guarded_react_agent(llm, [t], checkpointer=saver, limit=1)

    # One-shot run without run_context
    r_one_shot = agent.run("legacy one shot")
    # Due to limit=1, it pauses
    assert r_one_shot.interrupted is True

    # Resume the one shot run
    r_resumed = r_one_shot.resume("resume one shot")
    assert r_resumed.status == "done"

    # Session without run_context
    llm2 = FakeLLM([call("dummy_tool"), say("paused"), say("finished")])
    agent2 = make_guarded_react_agent(llm2, [t], checkpointer=saver, limit=1)
    s = agent2.session("legacy_session")
    _ = s.send("legacy turn")
    assert s.paused is True

    r_sess_resumed = s.resume("resume session")
    assert s.paused is False
    assert r_sess_resumed.status == "done"
