"""RecallContext v0 — the read-side loop: memory -> window -> prompt."""

from dataclasses import dataclass, field

from vsigma import (
    GLOBAL,
    InMemoryStore,
    LLMResponse,
    MemoryRecord,
    Provenance,
    RecallContext,
    Services,
    SlidingWindowContext,
    State,
    TrajectoryLog,
    make_react_agent,
    persona,
    say,
)
from vsigma.context import RECALL_MARKER


def _knowledge(store: InMemoryStore, scope_head: str, content: str, confidence: float) -> None:
    record = MemoryRecord(
        id=f"k:{content[:8]}",
        scope=scope_head,
        kind="knowledge",
        content=content,
        provenance=Provenance(origin="injected", source_ref="test.md", confidence=confidence),
    )
    store.write(record.namespace, record.id, record.to_dict())


def _state(**extra: object) -> State:
    base: State = {"messages": [{"role": "system", "content": "sys"}], "run_id": "r1"}
    base.update(extra)
    return base


def test_injects_knowledge_after_system_prompt() -> None:
    store = InMemoryStore()
    _knowledge(store, GLOBAL.head, "Deploys go through `ship`.", 1.0)

    window = RecallContext().assemble(_state(), store)
    messages = window["messages"]
    assert isinstance(messages, list)
    assert messages[0]["content"] == "sys"  # pinned system stays first
    assert str(messages[1]["content"]).startswith(RECALL_MARKER)
    assert "Deploys go through `ship`." in str(messages[1]["content"])


def test_respects_visibility_and_confidence_ranking() -> None:
    store = InMemoryStore()
    _knowledge(store, persona("alice").head, "alice fact", 0.4)
    _knowledge(store, persona("bob").head, "bob secret", 1.0)
    _knowledge(store, GLOBAL.head, "strong fact", 0.9)
    store.write(persona("alice").namespace("knowledge"), "plain", "plain string fact")

    ctx = RecallContext(max_facts=2, min_confidence=0.3)
    window = ctx.assemble(_state(user_id="alice"), store)
    block = str(window["messages"][1]["content"])  # type: ignore[index]

    assert "bob secret" not in block  # visibility rule holds in the prompt
    assert "strong fact" in block and "plain string fact" in block  # top-2 by confidence
    assert "alice fact" not in block  # bounded out by max_facts


def test_no_duplication_across_steps() -> None:
    # The seam persists node output; a second assemble must REPLACE the
    # recalled block, not stack another one.
    store = InMemoryStore()
    _knowledge(store, GLOBAL.head, "fact one", 1.0)
    ctx = RecallContext()

    window1 = ctx.assemble(_state(), store)
    _knowledge(store, GLOBAL.head, "fact two", 1.0)  # memory grew mid-episode
    window2 = ctx.assemble({**window1}, store)  # node echoed the window back

    messages = window2["messages"]
    assert isinstance(messages, list)
    marked = [m for m in messages if str(m.get("content", "")).startswith(RECALL_MARKER)]
    assert len(marked) == 1  # replaced, not stacked
    assert "fact two" in str(marked[0]["content"])  # and refreshed


def test_composes_with_sliding_window() -> None:
    store = InMemoryStore()
    _knowledge(store, GLOBAL.head, "the fact", 1.0)
    ctx = RecallContext(inner=SlidingWindowContext(max_messages=2))

    state = _state()
    messages = state["messages"]
    assert isinstance(messages, list)
    messages += [{"role": "user", "content": f"m{i}"} for i in range(6)]

    window = ctx.assemble(state, store)
    kept = window["messages"]
    assert isinstance(kept, list)
    expected_block = f"{RECALL_MARKER} Relevant knowledge:\n- the fact"
    assert [m.get("content") for m in kept[:2]] == ["sys", expected_block]
    assert [m["content"] for m in kept[2:]] == ["m4", "m5"]  # compacted first


@dataclass
class _SpyLLM:
    """Records what the model was actually shown."""

    response: LLMResponse
    seen: list[list[dict[str, object]]] = field(default_factory=list)

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        self.seen.append(messages)
        return self.response


def test_end_to_end_ingested_memory_reaches_the_prompt() -> None:
    # The full read-side loop on a live agent: store -> RecallContext -> LLM.
    store = InMemoryStore()
    _knowledge(store, persona("alice").head, "Alice prefers concise answers.", 0.9)

    spy = _SpyLLM(say("ok"))
    svc = Services(memory=store, observer=TrajectoryLog(), context=RecallContext())
    agent = make_react_agent(spy, [], services=svc, system="be helpful")
    run = agent.run("hello", user_id="alice")

    assert run.status == "done"
    prompt = spy.seen[0]
    assert prompt[0]["content"] == "be helpful"
    assert "Alice prefers concise answers." in str(prompt[1]["content"])


def test_default_remains_passthrough_no_silent_injection() -> None:
    # Without opting in, nothing is injected even when memory has knowledge.
    store = InMemoryStore()
    _knowledge(store, GLOBAL.head, "a fact", 1.0)
    spy = _SpyLLM(say("ok"))
    svc = Services(memory=store, observer=TrajectoryLog())  # default context
    agent = make_react_agent(spy, [], services=svc)
    agent.run("hello")

    assert not any(RECALL_MARKER in str(m.get("content", "")) for m in spy.seen[0])
