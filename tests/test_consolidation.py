"""Two-round consolidation (§E) + the sliding-window context policy."""

import json
from pathlib import Path

from vsigma import (
    FakeLLM,
    InMemoryStore,
    Services,
    SlidingWindowContext,
    State,
    TrajectoryLog,
    call,
    ingest_bundle,
    make_guarded_react_agent,
    make_react_agent,
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
    svc = Services(
        memory=store, observer=TrajectoryLog(), run_end=reflexive_consolidator()
    )
    agent = make_react_agent(FakeLLM([say("done!")]), [_NOOP], services=svc)
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
    svc = Services(memory=store, observer=TrajectoryLog(), run_end=reflexive_consolidator())

    def attempt() -> None:
        agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], services=svc)
        agent.run("migrate the db", session_id="s1", task_id="T-42")

    attempt()
    attempt()

    entries = store.query(session("s1").namespace("context"))
    same_task = [v for _, v in entries if "task T-42" in str(v["content"])]  # type: ignore[index]
    assert len(same_task) == 2  # both attempts, distinct episodes, one task


def test_reflexive_round_distills_provisional_knowledge() -> None:
    store = InMemoryStore()
    consolidator_llm = FakeLLM([say(_FACT_LINE)])  # the one bounded call
    svc = Services(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(consolidator_llm, constructor="fake/agent-llm"),
    )
    agent = make_react_agent(FakeLLM([call("noop"), say("done")]), [_NOOP], services=svc)
    run = agent.run("figure out uploads", user_id="alice")

    assert run.status == "done"
    # Provisional knowledge landed in the serving persona's scope.
    entries = store.query(persona("alice").namespace("knowledge"))
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
    svc = Services(
        memory=store, observer=TrajectoryLog(), run_end=reflexive_consolidator(chatty)
    )
    agent = make_react_agent(FakeLLM([say("ok")]), [_NOOP], services=svc)
    run = agent.run("task", user_id="alice")

    assert run.status == "done"  # the run itself is untouched
    assert store.query(persona("alice").namespace("knowledge")) == []


def test_escalated_run_writes_summary_but_never_distills() -> None:
    # An escalated attempt leaves a factual episode summary (so a retry can
    # find the failed prior attempt), but the LLM distillation is skipped —
    # a cheap pass over a failure trajectory risks storing anti-knowledge.
    store = InMemoryStore()
    svc = Services(
        memory=store,
        observer=TrajectoryLog(),
        # FakeLLM([]) raises if `complete` is ever called: proves no distill.
        run_end=reflexive_consolidator(FakeLLM([])),
    )
    llm = FakeLLM([call("missing_tool")])
    agent = make_guarded_react_agent(llm, [_NOOP], limit=1, services=svc)
    run = agent.run("do it", session_id="s1", user_id="alice")

    assert run.status == "escalated"
    stored = store.read(session("s1").namespace("context"), f"episode:{run.run_id}")
    assert isinstance(stored, dict)
    assert "status=escalated" in str(stored["content"])
    assert store.query(persona("alice").namespace("knowledge")) == []


def test_resume_to_done_overwrites_summary_and_distills_once() -> None:
    store = InMemoryStore()
    svc = Services(
        memory=store,
        observer=TrajectoryLog(),
        # Exactly one scripted response: a second distill call would raise.
        run_end=reflexive_consolidator(FakeLLM([say(_FACT_LINE)])),
    )
    llm = FakeLLM([call("missing_tool"), say("done!")])
    agent = make_guarded_react_agent(llm, [_NOOP], limit=1, services=svc)
    run = agent.run("do it", session_id="s1", user_id="alice")
    assert run.status == "escalated"

    run.resume("use the noop tool instead")

    assert run.status == "done"
    # Same fixed id ⇒ the final status supersedes the interim "escalated".
    stored = store.read(session("s1").namespace("context"), f"episode:{run.run_id}")
    assert isinstance(stored, dict)
    assert "status=done" in str(stored["content"])
    assert len(store.query(persona("alice").namespace("knowledge"))) == 1


def test_deep_pass_same_engine_supersedes_round_one(tmp_path: Path) -> None:
    store = InMemoryStore()

    # Round 1 (reflexive) wrote the fact provisionally.
    svc = Services(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(FakeLLM([say(_FACT_LINE)])),
    )
    agent = make_react_agent(FakeLLM([say("done")]), [_NOOP], services=svc)
    run = agent.run("task", user_id="alice")

    ns = persona("alice").namespace("knowledge")
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
        scope=persona("alice"),
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
    messages: list[dict[str, object]] = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": f"m{i}"} for i in range(10)]
    state: State = {"messages": messages, "run_id": "r1"}

    window = ctx.assemble(state, InMemoryStore())
    kept = window["messages"]
    assert isinstance(kept, list)
    assert kept[0] == {"role": "system", "content": "sys"}
    assert [m["content"] for m in kept[1:]] == ["m7", "m8", "m9"]
    # Original state untouched (assemble builds a window, not a mutation).
    assert len(messages) == 11


def test_sliding_window_drops_orphaned_tool_results() -> None:
    ctx = SlidingWindowContext(max_messages=2)
    state: State = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "obs"},
            {"role": "assistant", "content": "answer"},
        ]
    }
    window = ctx.assemble(state, InMemoryStore())
    kept = window["messages"]
    assert isinstance(kept, list)
    assert [m["role"] for m in kept] == ["assistant"]  # orphaned tool turn dropped
