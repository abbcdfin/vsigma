# Proposal: Durability Bar — Each Fact Says Whether It Lasts

**Status:** **GRADUATED** 2026-11-12 as Phase 4.20, released in `v0.4.1` (2026-11-12).
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

1. **Each distilled fact carries `"lasting": true | false`.** The prompt's output format asks
   for it, beside `content` and `source_ref`, and says what each value means: `true` only for a
   standing fact that will still hold next week (who the user is, what they prefer, a rule, how
   work is done), `false` for the state of a particular thing at the time of the run (the status
   of an order, task or system).
2. **`distill` drops a fact labelled `false`.** A fact with no label is kept, so a model that
   ignores the key distills as before.
3. The exclusion list stays as it is.

The label is never stored: records, bundles and `distill`'s signature are unchanged, so the
change is compatible.

## C. Limits

- The label is the model's judgment, and its wording decides the result (§E): asking only
  "will it still hold next week" let a status through in about a third of runs.
- A past event can pass as lasting ("Order 1001 was packed at warehouse WH-42"): true, though of
  little use.
- Records stored before the change stay. The evidence is one model.

## D. Graduation

- Unit tests, with a `FakeLLM` reply: a fact labelled `false` yields no record, one labelled
  `true` or unlabelled yields its record, and the drift pin asserts that the prompt asks for
  `lasting`.
- `live_memory_scenario.py` against a real endpoint, five runs: persona memory never states the
  order's current status, keeps Alice's carrier and warehouse code, and every other check passes.

## E. Evidence (the scenario's own check)

Runs where persona memory stated the order's current status:

| Prompt | Full scenario | Maintenance call replayed |
|---|---|---|
| no label (2026-11-03) | 3 of 4 | |
| no label, one more exclusion item, three wordings | 6 of 6 | |
| label, "true if the fact will still hold next week" | 0 of 5 (2026-11-03), 4 of 11 (2026-11-12) | 4 of 20 |
| label, the same with examples of changing things | | 3 of 20 |
| label, "what a tool returned is the state at the time" | | 4 of 20 |
| label, "true only for a standing fact", as in §B | 0 of 11 | 0 of 40 |

In every leak the model had labelled the status `true`; no fact came back unlabelled. The §B
wording kept Alice's carrier and warehouse code in all 51 runs; five of the full runs were on
the built code, with every check passing. On a document source (a team
handbook with two notes about today), 16 replays kept every standing fact and dropped only the
two notes. A wording that named the user alone ("a fact about the user or how to work for
them") also stopped the leak, but dropped every handbook fact in 2 of 24 document replays.

The replays are kept as `vdesign/utils/replay_distill.py`, which calls `distill` itself on a
session case and a handbook case and counts leaks and losses per prompt. Its first run: the
built prompt leaked in 0 of 20 session replays and the earlier wording in 8 of 20. On the
handbook the built prompt lost one standing rule in 1 of 23 replays, so a loss is rare but not
impossible. Measure a new wording with it before changing the prompt.
