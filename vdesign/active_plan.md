# Active Plan — Phase 4.20: Durability Bar (Each Fact Says Whether It Lasts)

*Design: [`docs/proposals/durability-bar.md`](../docs/proposals/durability-bar.md).*
*Status: BUILDING.*

## Steps

- [x] **1. Distill prompt & engine (`src/vsigma/construction.py`)**
  - Add `"lasting": <true or false>` to `_DISTILL_SYSTEM` output format and the refined sentence:
    `'Set "lasting" to true only for a standing fact that will still hold next week: who the user is, what they prefer, a rule, or how work is done. Set it to false for the state of a particular thing at the time of the run, such as the status of an order, task or system.'`
  - Update module docstring with the new line shape example.
  - In `distill`: skip a fact whose `lasting` is `False`. Keep every other fact (`True`, no label, or any other value). A skipped fact is not an error.
  - Leave `MemoryRecord`, bundle schema, record ids, and `distill` signature unchanged (label is never stored).

- [x] **2. Tests (`tests/test_bundle.py`)**
  - Extend `test_distill_prompt_carries_the_durability_bar` to assert the prompt asks for `lasting`, `standing fact`, and `at the time of the run`.
  - Add `test_distill_durability_bar_lasting_label`: a fact labelled `false` yields no record, while facts labelled `true`, unlabelled, or other values each yield their record.

- [x] **3. Docs (`docs/architecture_design.md` §8, `CHANGELOG.md`)**
  - In `docs/architecture_design.md` §8, describe what the label means in the new terms (standing fact vs state of a particular thing at the time of the run).
  - In `CHANGELOG.md`, add a user-facing entry in `## Unreleased` under `### Changed`.

- [ ] **4. Verification & Review**
  - `make check` (ruff, mypy, pytest) green.
  - Review: reviewer runs `vdesign/utils/live_memory_scenario.py` five times against a real endpoint.
