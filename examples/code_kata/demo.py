"""Demo: a Reflexion agent solving a code kata (generate -> test -> revise).

Run offline with `python -m examples.code_kata.demo` — a scripted `FakeLLM`
first submits a buggy implementation (crashes on the empty string), the test
validator feeds the real traceback back as critique, and the revision passes.
Set ``VSIGMA_LLM_BASE_URL`` / ``VSIGMA_LLM_API_KEY`` to let a real model take
the critique loop instead.

Also showcases the live tap: the LLM is wrapped in `LoggingLLM` and
`enable_llm_logging()` prints each reply as the loop runs.
"""

from __future__ import annotations

from vsigma import (
    LLM,
    Agent,
    FakeLLM,
    LoggingLLM,
    Run,
    default_llm,
    disable_llm_logging,
    enable_llm_logging,
    make_reflexion_agent,
    say,
)

from .kata import RLE_TASK, RLE_TESTS, make_test_validator

KATA_SYSTEM = (
    "You are a Python programmer. Solve the kata. Reply with only the code, "
    "no explanations. If tests fail you will see the traceback — fix and resubmit."
)

_BUGGY = """\
def rle(s: str) -> str:
    out = []
    current, count = s[0], 1
    for ch in s[1:]:
        if ch == current:
            count += 1
        else:
            out.append(f"{current}{count}")
            current, count = ch, 1
    out.append(f"{current}{count}")
    return "".join(out)
"""

_FIXED = """\
def rle(s: str) -> str:
    if not s:
        return ""
    out = []
    current, count = s[0], 1
    for ch in s[1:]:
        if ch == current:
            count += 1
        else:
            out.append(f"{current}{count}")
            current, count = ch, 1
    out.append(f"{current}{count}")
    return "".join(out)
"""


def _demo_fake_llm() -> FakeLLM:
    # Round 1: buggy (IndexError on rle("")); round 2: fixed after the critique.
    return FakeLLM([say(f"```python\n{_BUGGY}```"), say(f"```python\n{_FIXED}```")])


def build_kata_agent(llm: LLM, tests: str = RLE_TESTS, *, limit: int = 4) -> Agent:
    validator = make_test_validator(tests)
    return make_reflexion_agent(llm, [], validator, limit=limit, system=KATA_SYSTEM)


def run_demo(*, llm: LLM | None = None, task: str = RLE_TASK) -> Run:
    """Build and run the kata agent; returns the `Run` (also printed)."""
    if llm is None:
        llm = default_llm() or _demo_fake_llm()
    enable_llm_logging()  # watch the critique loop live
    try:
        agent = build_kata_agent(LoggingLLM(llm))
        run = agent.run(task, system=KATA_SYSTEM, session_id="kata-session")
    finally:
        disable_llm_logging()
    print(f"status: {run.status}")
    print(f"result:\n{run.result}")
    print("trajectory:")
    for event in run.trajectory:
        print(f"  - {event.node}[{event.attempt}]: {event.summary}")
    return run


if __name__ == "__main__":
    run_demo()
