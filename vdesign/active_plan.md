# Active Plan — Phase 3: Critique loop & worked example

> JIT breakdown for the phase in flight. Durable roadmap: [`plan.md`](./plan.md).
> Previous board (Phase 4, all done) is recorded in plan.md; started 2026-07-04
> after Phase 4.1 (`LoggingLLM`).

**Ground rules:** every slice lands green (`make check`), offline-testable (FakeLLM),
no new runtime deps. Scoping decision (recorded): the worked example is a **single-file
code kata**, not a repo-navigating mini-SWE-bench — the validator is "run the tests",
the one naturally deterministic critic. Convergence target is "passes the tests",
not "is correct" — state this limitation in the example.

## Slice 1 — `critic_node` + reflexion template `[x]`
- `critique.py`: `Validator = Callable[[str], str | None]` (None = accepted,
  str = deterministic critique); `critic_node(validator)` — pass → `status: done`,
  fail → append `[critique] …` feedback message, `status: running` (Reflexion)
- `make_reflexion_agent(llm, tools, validator, *, limit, …)` — generate → validate →
  revise; **both** routers are `break_on_attempts` (act's `done` → critic; critic's
  fail → act while budget lasts, then human-interrupt): a candidate that always
  "finishes" but never validates must still escalate, not spin
- Backstop: one attempt ≈ act + critic = 2 super-steps → `_backstop(2 * limit)`
- **Tests:** pass/fail node behavior; end-to-end wrong→right (validator called twice,
  critique reached the model); never-valid → escalates; resume still works

## Slice 2 — Worked example: code kata `[x]`
- `examples/code_kata/`: `make_test_validator(tests)` — run candidate + asserts in a
  `sys.executable` subprocess (timeout-bounded), return failure tail as the critique;
  `extract_code` strips markdown fences
- `demo.py` (`python -m examples.code_kata.demo`): offline `FakeLLM` scripted
  buggy → fixed (`rle` kata); real model via `VSIGMA_LLM_*`; showcases
  `LoggingLLM` + `enable_llm_logging()` (watch the critique loop live)
- **Tests:** validator green/red; demo runs offline end-to-end

## Wrap-up `[x]`
- Exports; `getting_started.md` critique section; §6 ⚙ note in
  `architecture_design.md`; plan.md Phase 3 checkboxes; checkpoint → tag `phase-3`
