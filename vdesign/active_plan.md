# Active Plan — Phase 4.20: Durability Bar (Each Fact Says Whether It Lasts)

*Design: [`docs/proposals/durability-bar.md`](../docs/proposals/durability-bar.md).*
*Status: BUILDING.*

## Steps

- [x] **1. Distill prompt & engine (`src/vsigma/construction.py`)**
  - Add `"lasting": <true or false>` to `_DISTILL_SYSTEM` output format and the exact sentence:
    `'Set "lasting" to true if the fact will still hold next week, false if it describes a state that may change before then.'`
  - Update module docstring with the new line shape example.
  - In `distill`: skip a fact whose `lasting` is `False`. Keep every other fact (`True`, no label, or any other value). A skipped fact is not an error.
  - Leave `MemoryRecord`, bundle schema, record ids, and `distill` signature unchanged (label is never stored).

- [x] **2. Tests (`tests/test_bundle.py`)**
  - Extend `test_distill_prompt_carries_the_durability_bar` to assert the prompt asks for `lasting`.
  - Add `test_distill_durability_bar_lasting_label`: a fact labelled `false` yields no record, while facts labelled `true`, unlabelled, or other values each yield their record.

- [x] **3. Docs (`docs/architecture_design.md` §8, `CHANGELOG.md`)**
  - In `docs/architecture_design.md` §8, replace the ✎ note after "durability bar" with the rule as built.
  - In `CHANGELOG.md`, add a `## Unreleased` section with the entry under `### Changed`.

- [ ] **4. Verification & Review**
  - `make check` (ruff, mypy, pytest) green.
  - Review: reviewer runs `vdesign/utils/live_memory_scenario.py` five times against a real endpoint.
