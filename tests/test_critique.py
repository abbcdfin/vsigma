"""critic_node + make_reflexion_agent — generate -> validate -> revise."""

from typing import Any, cast

from vsigma import (
    CRITIQUE_MARKER,
    FakeLLM,
    LLMResponse,
    State,
    StaticLLM,
    Validator,
    all_of,
    critic_node,
    llm_judge,
    make_reflexion_agent,
    say,
)


def test_critic_accepts_valid_result() -> None:
    node = critic_node(lambda result: None)
    update = node({"result": "fine", "messages": [], "status": "done"})
    assert update == {"status": "done"}


def test_critic_rejects_with_feedback_message() -> None:
    node = critic_node(lambda result: "too short")
    state: State = {"result": "x", "messages": [{"role": "user", "content": "task"}]}
    update = node(state)

    assert update["status"] == "running"
    messages = update["messages"]
    assert isinstance(messages, list)
    critique = str(messages[-1]["content"])
    assert critique.startswith(CRITIQUE_MARKER)
    assert "too short" in critique


def test_reflexion_agent_revises_until_valid() -> None:
    seen: list[str] = []

    def validator(candidate: str) -> str | None:
        seen.append(candidate)
        return None if "correct" in candidate else "answer must contain 'correct'"

    llm = FakeLLM([say("wrong answer"), say("the correct answer")])
    agent = make_reflexion_agent(llm, [], validator)
    run = agent.run("solve it")

    assert run.status == "done"
    assert run.result == "the correct answer"
    assert seen == ["wrong answer", "the correct answer"]  # validated both rounds


def test_critique_feedback_reaches_the_model() -> None:
    llm = FakeLLM([say("v1"), say("v2")])
    agent = make_reflexion_agent(
        llm, [], lambda c: None if c == "v2" else "try again: say v2"
    )
    run = agent.run("go")

    assert run.status == "done"
    messages = agent.graph.get_state(cast("Any", run.config)).values["messages"]
    critiques = [m for m in messages if str(m.get("content", "")).startswith(CRITIQUE_MARKER)]
    assert len(critiques) == 1
    assert "try again: say v2" in str(critiques[0]["content"])


def test_never_valid_candidate_escalates_not_spins() -> None:
    # The model always "finishes", the validator never accepts: without budget
    # enforcement on the critic's router this would spin until the recursion
    # backstop; it must escalate cleanly instead.
    agent = make_reflexion_agent(StaticLLM("bad"), [], lambda c: "never good", limit=3)
    run = agent.run("impossible")
    assert run.status == "escalated"


def test_resume_after_escalation_can_still_converge() -> None:
    llm = FakeLLM([say("bad"), say("bad"), say("good")])
    agent = make_reflexion_agent(
        llm, [], lambda c: None if c == "good" else "not good yet", limit=2
    )
    run = agent.run("go")
    assert run.status == "escalated"

    run.resume("hint: the answer is good")
    assert run.status == "done"
    assert run.result == "good"


def test_all_of_returns_first_critique_in_order() -> None:
    calls: list[str] = []

    def clause(name: str, critique: str | None) -> Validator:
        def validate(candidate: str) -> str | None:
            calls.append(name)
            return critique

        return validate

    validator = all_of(clause("a", None), clause("b", "b failed"), clause("c", "c failed"))
    assert validator("x") == "b failed"
    assert calls == ["a", "b"]  # short-circuits: c never runs


def test_all_of_passes_when_every_clause_passes() -> None:
    assert all_of(lambda c: None, lambda c: None)("x") is None


def test_llm_judge_pass_and_critique() -> None:
    assert llm_judge(StaticLLM("PASS"), "rubric")("candidate") is None
    critique = llm_judge(StaticLLM("missing citation on claim 2"), "rubric")("candidate")
    assert critique == "missing citation on claim 2"


def test_llm_judge_fails_closed_on_judge_error() -> None:
    class _BrokenLLM:
        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> LLMResponse:
            raise ConnectionError("judge endpoint down")

    critique = llm_judge(_BrokenLLM(), "rubric")("candidate")
    assert critique is not None and "judge error" in critique


def test_layered_gate_deterministic_first_then_judge() -> None:
    # The recommended composition: the judge only sees candidates that already
    # passed the deterministic clauses.
    judge_calls: list[str] = []

    class _CountingJudge:
        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> LLMResponse:
            judge_calls.append("called")
            return say("PASS")

    def format_clause(candidate: str) -> str | None:  # consumer-written clause
        return None if candidate.startswith("{") else "output must be a JSON object"

    validator = all_of(format_clause, llm_judge(_CountingJudge(), "rubric"))
    assert validator("broken") is not None  # rejected before the judge
    assert judge_calls == []
    assert validator('{"answer": 42}') is None  # judge sees only well-formed
    assert judge_calls == ["called"]
