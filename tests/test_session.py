"""Session handle — multi-round dialog (interaction-model proposal §B)."""

from dataclasses import dataclass, field
from pathlib import Path

from vsigma import (
    FakeLLM,
    InMemoryStore,
    LLMResponse,
    Services,
    SQLiteStore,
    StaticLLM,
    Tool,
    TrajectoryLog,
    call,
    make_guarded_react_agent,
    make_react_agent,
    say,
)
from vsigma.scope import session as session_scope


@dataclass
class _SpyLLM:
    responses: list[LLMResponse]
    seen: list[list[dict[str, object]]] = field(default_factory=list)
    _cursor: int = 0

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        self.seen.append(list(messages))  # copy: react_node appends to this list after the call
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
    assert contents[0] == "be brief"  # system prompt still first, not duplicated
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

    roles = [m["role"] for m in spy.seen[2]]
    assert "tool" not in roles  # tool traffic is episode-local, not dialog
    assert all(not m.get("tool_calls") for m in spy.seen[2])


def test_rehydrates_across_process_restarts(tmp_path: Path) -> None:
    db = str(tmp_path / "session.db")

    store = SQLiteStore(db)
    svc = Services(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("Noted: 42.")]), [], services=svc)
    agent.session("s1").send("Remember the number 42.")
    store.close()

    # "New process": fresh store handle, fresh agent, same session id.
    store2 = SQLiteStore(db)
    svc2 = Services(memory=store2, observer=TrajectoryLog())
    spy = _SpyLLM([say("It was 42.")])
    agent2 = make_react_agent(spy, [], services=svc2)
    run = agent2.session("s1").send("What was the number?")

    assert run.status == "done"
    contents = [str(m["content"]) for m in spy.seen[0]]
    assert "Remember the number 42." in contents and "Noted: 42." in contents


def test_send_resumes_an_escalated_run() -> None:
    llm = FakeLLM([call("missing_tool"), say("Done with your guidance.")])
    agent = make_guarded_react_agent(llm, [], limit=1)
    session = agent.session()

    run1 = session.send("do the thing")
    assert run1.status == "escalated"

    run2 = session.send("here is what you needed")  # routes to resume, same run
    assert run2 is run1
    assert run2.status == "done"
    assert len(session.runs) == 1


def test_channel_is_metadata_never_sent_to_model() -> None:
    store = InMemoryStore()
    svc = Services(memory=store, observer=TrajectoryLog())
    spy = _SpyLLM([say("ok")])
    agent = make_react_agent(spy, [], services=svc)
    session = agent.session("s1", user_id="alice")

    session.send("hello", channel="slack")

    turn = store.read(session_scope("s1").namespace("context"), "turn:0")
    assert isinstance(turn, dict)
    assert turn["channel"] == "slack" and turn["user_id"] == "alice"
    assert all("channel" not in m for m in spy.seen[0])  # model never sees it


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
