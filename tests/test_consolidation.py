"""Two-round consolidation (§E) + the sliding-window context policy."""

import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vsigma import (
    AGENT,
    FakeLLM,
    InMemoryStore,
    Interceptor,
    SlidingWindowContext,
    State,
    TrajectoryLog,
    call,
    episode,
    ingest_bundle,
    make_guarded_react_agent,
    make_react_agent,
    make_reflexion_agent,
    persona,
    say,
    session,
    write_bundle,
)
from vsigma.consolidation import deep_consolidate, episode_trajectory, reflexive_consolidator
from vsigma.tools import Tool

_NOOP = Tool(
    name="noop",
    description="noop",
    parameters={"type": "object", "properties": {}},
    func=lambda args: "ok",
)

_FACT = "The upload endpoint requires a session cookie."
_FACT_LINE = json.dumps({"content": _FACT, "source_ref": "trajectory:r1"})


def test_mechanical_reflexive_consolidation_without_llm() -> None:
    # llm=None degrades to mechanical episode capture — nothing breaks.
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog(), run_end=reflexive_consolidator())
    agent = make_react_agent(FakeLLM([say("done!")]), [_NOOP], interceptor=itc)
    run = agent.run("do it", session_id="s1", user_id="alice", task_id="T-42")

    assert run.status == "done"
    stored = store.read(session("s1").namespace("context"), f"episode:{run.run_id}")
    assert isinstance(stored, dict)
    assert "task T-42" in str(stored["content"])  # correlation key survives
    prov = stored["provenance"]
    assert isinstance(prov, dict)
    assert prov["origin"] == "consolidated"
    assert prov["consolidator"] == "reflexive"
    assert prov["confidence"] == 0.3


def test_retry_run_can_correlate_prior_attempts_by_task() -> None:
    # Same task, two episodes: attempt 2 finds attempt 1 in session context by
    # task_id — cross-attempt continuity via consolidation, no task scope rung.
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog(), run_end=reflexive_consolidator())

    def attempt() -> None:
        agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], interceptor=itc)
        agent.run("migrate the db", session_id="s1", task_id="T-42")

    attempt()
    attempt()

    entries = store.query(session("s1").namespace("context"))
    same_task = [v for _, v in entries if "task T-42" in str(v["content"])]  # type: ignore[index]
    assert len(same_task) == 2  # both attempts, distinct episodes, one task


def test_reflexive_round_distills_provisional_knowledge() -> None:
    store = InMemoryStore()
    consolidator_llm = FakeLLM([say(_FACT_LINE)])  # the one bounded call
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(consolidator_llm, constructor="fake/agent-llm"),
    )
    agent = make_react_agent(FakeLLM([call("noop"), say("done")]), [_NOOP], interceptor=itc)
    run = agent.run("figure out uploads", session_id="s1", user_id="alice")

    assert run.status == "done"
    # Provisional knowledge lands at the narrowest durable scope: the session.
    entries = store.query(session("s1").namespace("knowledge"))
    assert len(entries) == 1
    _, value = entries[0]
    assert isinstance(value, dict)
    assert value["content"] == _FACT
    prov = value["provenance"]
    assert isinstance(prov, dict)
    assert (prov["consolidator"], prov["confidence"]) == ("reflexive", 0.3)


def test_malformed_reflexive_pass_is_skipped_not_fatal() -> None:
    store = InMemoryStore()
    chatty = FakeLLM([say("here's what I learned: stuff")])  # breaks the shape
    itc = Interceptor(
        memory=store, observer=TrajectoryLog(), run_end=reflexive_consolidator(chatty)
    )
    agent = make_react_agent(FakeLLM([say("ok")]), [_NOOP], interceptor=itc)
    run = agent.run("task", session_id="s1", user_id="alice")

    assert run.status == "done"  # the run itself is untouched
    assert store.query(session("s1").namespace("knowledge")) == []


def test_episode_trajectory_dedupes_history_without_losing_the_conversation() -> None:
    # Each node update carries only the newly added messages (append-only semantics),
    # so reading the rows in insertion order yields the complete conversation
    # without cumulative duplicates or need for masking.
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    critiques = iter(["not good enough", None])  # reject once, then accept
    agent = make_reflexion_agent(
        FakeLLM([say("first try"), say("the final answer")]),
        [],
        validator=lambda candidate: next(critiques),
        interceptor=itc,
    )
    run = agent.run("q")
    assert run.status == "done"

    _, text = episode_trajectory(store, run.run_id)
    assert "the final answer" in text  # the conversation survives
    # The critique message existed once in the critic step and is never duplicated.
    assert text.count("not good enough") == 1
    assert "cumulative history omitted" not in text


def test_episode_trajectory_window_and_deep_pass_regression() -> None:
    """Regression test: a 30-step episode reaches the deep pass whole,
    and the reflexive source still starts with the task.
    """
    from uuid import uuid4

    from vsigma.consolidation import REFLEXIVE_WINDOW
    from vsigma.scope import episode

    store = InMemoryStore()
    run_id = "run-long-30"
    ns = episode(run_id).namespace("context")

    # Step 0: input row
    store.write(
        ns,
        f"input:{uuid4().hex}",
        {"messages": [{"role": "user", "content": "initial task"}]},
    )

    # Steps 1 to 29: node rows
    for i in range(1, 30):
        store.write(
            ns,
            f"act:{uuid4().hex}",
            {"messages": [{"role": "assistant", "content": f"msg_{i}_payload"}]},
        )

    # 1. Reflexive round: passes window=REFLEXIVE_WINDOW (20)
    _, reflexive_text = episode_trajectory(store, run_id, window=REFLEXIVE_WINDOW)
    # Task is always preserved even when outside the last 20 steps
    assert "initial task" in reflexive_text
    assert "msg_29_payload" in reflexive_text
    # Older intermediate steps beyond the last 20 are excluded from reflexive window
    assert "msg_1_payload" not in reflexive_text

    # 2. Deep pass: passes window=None (all steps preserved)
    _, deep_text = episode_trajectory(store, run_id, window=None)
    assert "initial task" in deep_text
    assert "msg_1_payload" in deep_text
    assert "msg_15_payload" in deep_text
    assert "msg_29_payload" in deep_text


def test_escalated_run_writes_summary_but_never_distills() -> None:
    # An escalated attempt leaves a factual episode summary (so a retry can
    # find the failed prior attempt), but the LLM distillation is skipped —
    # a cheap pass over a failure trajectory risks storing anti-knowledge.
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        # FakeLLM([]) raises if `complete` is ever called: proves no distill.
        run_end=reflexive_consolidator(FakeLLM([])),
    )
    llm = FakeLLM([call("missing_tool")])
    agent = make_guarded_react_agent(llm, [_NOOP], limit=1, interceptor=itc)
    run = agent.run("do it", session_id="s1", user_id="alice")

    assert run.status == "escalated"
    stored = store.read(session("s1").namespace("context"), f"episode:{run.run_id}")
    assert isinstance(stored, dict)
    assert "status=escalated" in str(stored["content"])
    assert store.query(persona("alice").namespace("knowledge")) == []


def test_resume_to_done_overwrites_summary_and_distills_once() -> None:
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        # Exactly one scripted response: a second distill call would raise.
        run_end=reflexive_consolidator(FakeLLM([say(_FACT_LINE)])),
    )
    llm = FakeLLM([call("missing_tool"), say("done!")])
    agent = make_guarded_react_agent(llm, [_NOOP], limit=1, interceptor=itc)
    run = agent.run("do it", session_id="s1", user_id="alice")
    assert run.status == "escalated"

    run.resume("use the noop tool instead")

    assert run.status == "done"
    # Same fixed id ⇒ the final status supersedes the interim "escalated".
    stored = store.read(session("s1").namespace("context"), f"episode:{run.run_id}")
    assert isinstance(stored, dict)
    assert "status=done" in str(stored["content"])
    assert len(store.query(session("s1").namespace("knowledge"))) == 1


def test_deep_pass_same_engine_supersedes_round_one(tmp_path: Path) -> None:
    store = InMemoryStore()

    # Round 1 (reflexive) wrote the fact provisionally.
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(FakeLLM([say(_FACT_LINE)])),
    )
    agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], interceptor=itc)
    run = agent.run("task", session_id="s1", user_id="alice")

    ns = session("s1").namespace("knowledge")
    [(record_id, before)] = store.query(ns)
    assert isinstance(before, dict)
    before_prov = before["provenance"]
    assert isinstance(before_prov, dict)
    assert before_prov["confidence"] == 0.3

    # Round 2 (deep): same engine, capable constructor, across episodes —
    # committed through the same bundle gate as any construction.
    records = deep_consolidate(
        [episode_trajectory(store, run.run_id)],
        FakeLLM([say(_FACT_LINE)]),
        # Same scope as round 1: ids are content-addressed but namespaces are
        # not, so a deep pass aimed elsewhere *adds* a record rather than
        # superseding one.
        scope=session("s1"),
        constructor="capable-model/deep",
    )
    bundle = write_bundle(tmp_path / "deep", records, constructor="capable-model/deep")
    ingest_bundle(bundle, store)

    # Content-addressed id ⇒ the deep record superseded the reflexive one.
    [(after_id, after)] = store.query(ns)
    assert after_id == record_id
    assert isinstance(after, dict)
    prov = after["provenance"]
    assert isinstance(prov, dict)
    assert (prov["consolidator"], prov["confidence"]) == ("deep", 0.9)
    assert prov["constructor"] == "capable-model/deep"


def test_sliding_window_context_compacts() -> None:
    ctx = SlidingWindowContext(max_messages=3)
    messages = [HumanMessage(content=f"m{i}") for i in range(10)]
    state: State = {"messages": messages, "run_id": "r1"}

    window = ctx.assemble(state, InMemoryStore())
    kept = window["messages"]
    assert isinstance(kept, list)
    assert [m.content for m in kept] == ["m7", "m8", "m9"]
    # Original state untouched (assemble builds a window, not a mutation).
    assert len(messages) == 10


def test_sliding_window_drops_orphaned_tool_results() -> None:
    ctx = SlidingWindowContext(max_messages=2)
    state: State = {
        "messages": [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"name": "c1", "args": {}, "id": "c1"}]),
            ToolMessage(tool_call_id="c1", content="obs"),
            AIMessage(content="answer"),
        ]
    }
    window = ctx.assemble(state, InMemoryStore())
    kept = window["messages"]
    assert isinstance(kept, list)
    assert [m.type for m in kept] == ["ai"]  # orphaned tool turn dropped


def test_provisional_knowledge_never_widens_to_the_persona() -> None:
    """§A.3: a `user_id` is an identity claim, not a review. One cheap
    unreviewed call must not become visible to every later session of that
    persona."""
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(FakeLLM([say(_FACT_LINE)])),
    )
    agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], interceptor=itc)

    run = agent.run("task", session_id="s1", user_id="alice")

    assert run.status == "done"
    assert len(store.query(session("s1").namespace("knowledge"))) == 1
    assert store.query(persona("alice").namespace("knowledge")) == []


def test_a_sessionless_run_writes_no_durable_knowledge() -> None:
    """§A.4: the old fallback was AGENT, which `visible_scopes` returns for
    *every* persona — the widest exposure in the system. With no session there
    is no narrow durable home, so nothing durable is written and the run is
    otherwise unaffected."""
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(FakeLLM([say(_FACT_LINE)])),
    )
    agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], interceptor=itc)

    run = agent.run("task", user_id="alice")

    assert run.status == "done"
    assert run.result == "done"
    for scope in (AGENT, persona("alice"), episode(run.run_id)):
        assert store.query(scope.namespace("knowledge")) == []
    # The mechanical episode summary is context, not knowledge, and still lands.
    assert store.query(episode(run.run_id).namespace("context"))


def test_the_widening_rule_is_available_as_an_explicit_choice() -> None:
    """The escape hatch is configuration, never an inferred fallback."""
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(
            FakeLLM([say(_FACT_LINE)]),
            knowledge_scope=lambda state: persona(str(state["user_id"])),
        ),
    )
    agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], interceptor=itc)

    run = agent.run("task", session_id="s1", user_id="alice")

    assert run.status == "done"
    assert len(store.query(persona("alice").namespace("knowledge"))) == 1


def test_prior_turns_respect_insertion_order_and_zero_bound() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("r1"), say("r2"), say("r3")]), [_NOOP], interceptor=itc)

    run1 = agent.run("t1", session_id="s1")
    run2 = agent.run("t2", session_id="s1")
    run3 = agent.run("t3", session_id="s1")

    # 1. Turn 1 must not contain Turn 2 or Turn 3 as prior context
    _, text1 = episode_trajectory(store, run1.run_id)
    assert f"prior_context:{run2.run_id}" not in text1
    assert f"prior_context:{run3.run_id}" not in text1

    # 2. max_prior_turns=0 must produce NO prior context
    _, text3_zero = episode_trajectory(store, run3.run_id, max_prior_turns=0)
    assert "prior_context" not in text3_zero


def test_prior_turns_contain_only_dialog_not_tool_noise() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    llm = FakeLLM([call("noop"), say("r1 done"), say("r2 done")])
    agent = make_react_agent(llm, [_NOOP], interceptor=itc)

    run1 = agent.run("t1 with tool", session_id="s1")
    run2 = agent.run("t2", session_id="s1")

    _, text2 = episode_trajectory(store, run2.run_id, max_prior_turns=1)
    assert f"prior_context:{run1.run_id}" in text2
    data = json.loads(text2)
    prior = data[f"prior_context:{run1.run_id}"]
    assert isinstance(prior, list)
    for turn in prior:
        assert turn["role"] in ("user", "assistant")
        assert "tool_calls" not in turn
