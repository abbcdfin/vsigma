# Proposal: Durability Bar — Each Fact Says Whether It Lasts

**Status:** **ACCEPTED** 2026-11-03 as Phase 4.20; ships in `v0.4.1`, a compatible release.
**Amends:** the durability bar in `_DISTILL_SYSTEM` (`construction.py`), added 2026-07-16 with
[thinking-models](./thinking-models.md) §C. Bootstrap and both consolidation rounds share that
prompt.

## A. Problem

The bar asks for facts "that will remain true and useful in a future, unrelated session" and
lists run ephemera to leave out. A tool's report on something that changes passes it.

Found by the first real-LLM run (2026-11-03, Qwen3.6-27B with thinking off,
`vdesign/utils/live_memory_scenario.py`):
- Alice gives her carrier and warehouse code, then asks for order 1001's status.
- `maintain` writes her persona records at confidence 0.9, and two of them are the order's
  status: "Order 1001 is packed at warehouse WH-42" and "Order 1001 is awaiting carrier pickup".
- Persona memory is injected into every later session of hers. A later question about the order
  can be answered from the old status, maybe without calling the tool.

Naming such status in the exclusion list does not help (§E).

## B. Design

1. **Each distilled fact carries `"lasting": true | false`:** whether it will still hold next
   week. The prompt's output format asks for it, beside `content` and `source_ref`.
2. **`distill` drops a fact labelled `false`.** A fact with no label is kept, so a model that
   ignores the key distills as before.
3. The exclusion list stays as it is.

The label is never stored: records, bundles and `distill`'s signature are unchanged, so the
change is compatible.

## C. Limits

- The label is the model's judgment. A past event can pass as lasting ("Order 1001 was packed at
  warehouse WH-42"): true, though of little use.
- The run-end round leaked a status once in five runs, into session memory. That is provisional,
  seen only by the same session, whose dialog already holds the tool's answer.
- Records stored before the change stay. The evidence is one model.

## D. Graduation

- Unit tests, with a `FakeLLM` reply: a fact labelled `false` yields no record, one labelled
  `true` or unlabelled yields its record, and the drift pin asserts that the prompt asks for
  `lasting`.
- `live_memory_scenario.py` against a real endpoint, five runs: persona memory never states the
  order's current status, keeps Alice's carrier and warehouse code, and every other check passes.

## E. Evidence (2026-11-03, the scenario's own check)

| Prompt | Runs where persona memory stated the order's current status |
|---|---|
| current | 3 of 4 |
| one more exclusion item, three wordings | 6 of 6 |
| the `lasting` label (scratch spike) | 0 of 5 |
