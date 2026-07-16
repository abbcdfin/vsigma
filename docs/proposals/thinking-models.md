# Proposal: Thinking Models at the LLM Seam

**Status:** ACCEPTED 2026-07-16 — implemented same day; graduation per §D.
**Prompted by:** co-developing with bumblebee against local thinking models (Qwen3-class,
DeepSeek-R1-class): reasoning is invisible for debugging, and consolidation overthinks —
sometimes past the timeout, sometimes past the output contract.
**Decision folded in from review:** run-end distillation stays *on*, but with the model's
thinking strictly disabled (§B.4 option 2) — distillation is a constrained-output task
that gains nothing from deliberation, its failures are already contained (malformed pass
dropped, 0.3 confidence, deep round supersedes), and keeping it preserves same-day
learning between maintenance passes. Naming: the run-end LLM instance is the
**distiller** — "constructor" stays reserved for the provenance identity in the bundle
contract, where it names *who built the records*, not which client object made the call.

## A. Problem

`OpenAICompatibleLLM.complete` reads exactly two things from the reply:
`message["content"]` and `message["tool_calls"]`. Thinking models deliver their reasoning
through channels we don't handle, producing three distinct failures:

1. **Reasoning delivered as a separate field is silently dropped.** DeepSeek, vLLM and
   SGLang return `message.reasoning_content`; Ollama and several proxies return
   `message.reasoning`. The client never reads either, so `LoggingLLM` never logs it and
   no trajectory ever sees it. When a local model loops or derails, the one artifact that
   explains *why* is discarded before anyone can look at it — the debugging gap bumblebee
   hit.
2. **Reasoning delivered inline corrupts `text`.** Models serving raw chat templates (and
   older Ollama) emit `<think>…</think>` inside `content`. That block flows unfiltered
   into `LLMResponse.text`, which means: final answers carry pages of deliberation,
   mechanical summaries store it, and `distill`'s line-by-line JSON parse dies with
   "constructor broke the output shape" — a hard failure caused entirely by transport.
3. **No knob to turn thinking off where it hurts.** Distillation is a constrained-output
   task ("fill this JSON schema from these sources"); extended reasoning adds latency and
   overrun risk, not quality. Every provider has a switch — but each spells it
   differently, and our client offers no way to pass any of them.

All three live at the seam. Nothing above it (react, consolidation, templates) should
need to know thinking models exist.

## B. Design

### 1. `LLMResponse.reasoning` — a side channel, never a message

```python
@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""   # NEW: the model's deliberation, if any surfaced
```

`OpenAICompatibleLLM` populates it from, in order:

- `message["reasoning_content"]` or `message["reasoning"]` when present;
- otherwise any inline `<think>…</think>` blocks, which are **stripped out of
  `content`** and moved here (unterminated blocks — truncation mid-thought — strip to
  end of text).

The contract this buys: **`text` is always clean.** Final answers, summaries and
distilled output never contain think-tags regardless of how the provider serialized
them. That alone fixes failure 2 without touching `distill`.

`reasoning` is deliberately a *side channel*: it is never appended to the message
history on subsequent turns (providers expect reasoning dropped from context; echoing
it back degrades or errors), and `say()`/`call()`/`FakeLLM` are unchanged — tests that
want it construct `LLMResponse(reasoning=…)` directly.

### 2. `LoggingLLM` surfaces it — the debug visibility

The live tap gains one line: when `response.reasoning` is non-empty, the INFO reply log
includes a clipped `[thought: …]` segment (same `_clip` budget as text), and DEBUG logs
the full reasoning. Debugging a derailed local model becomes
`enable_llm_logging(logging.DEBUG, file=…)` — no new API.

### 3. `extra_body` + `max_tokens` — the off switch, as configuration

```python
@dataclass
class OpenAICompatibleLLM:
    model: str
    base_url: str
    api_key: str
    timeout: float = 60.0
    max_tokens: int | None = None          # NEW: hard bound on a runaway turn
    extra_body: dict[str, object] = ...    # NEW: merged into the request payload
```

`extra_body` is merged into the JSON payload last, so any provider-specific thinking
switch is reachable without a client release:

| provider | switch via `extra_body` |
|---|---|
| vLLM / SGLang (Qwen3) | `{"chat_template_kwargs": {"enable_thinking": false}}` |
| Ollama | `{"think": false}` |
| OpenAI-style reasoning tiers | `{"reasoning_effort": "low"}` |

We deliberately **pass through rather than normalize**: inventing a vsigma-level
`thinking=False` flag means chasing every provider's spelling forever; a dict field
means the operator writes what their server documents. `default_llm()` reads two new
optional env vars — `VSIGMA_LLM_MAX_TOKENS` and `VSIGMA_LLM_EXTRA_BODY` (a JSON
object) — keeping "swap models is configuration" true for thinking config too.

### 4. The consolidation ladder: three run-end options, documented as a set

`reflexive_consolidator(llm)` and `run_maintenance(…, llm=…)` already take their own
LLM, so what runs at run-end is a choice, not a fate. The options, in order of run-end
cost — to be presented as a ladder in getting_started's consolidation section:

| option | run-end hook | learns between maintenance passes? | overthinking risk |
|---|---|---|---|
| 1. mechanical only | `reflexive_consolidator(None)` | no — summary + trajectory staged for the deep round | none (no LLM call) |
| 2. **distill, thinking off** | `reflexive_consolidator(distiller)` | yes — provisional 0.3-confidence facts, superseded by the deep round | none (thinking disabled) |
| 3. distill, model as-is | `reflexive_consolidator(actor_llm)` | yes | full (what bumblebee hit) |

Every option keeps the same safety floor: the raw trajectory and mechanical summary are
always written (no LLM involved), a malformed distill is dropped without breaking the
run, and the maintenance pass re-derives everything properly at 0.9 confidence with
content-addressed supersede.

**Option 2 is the accepted default for local thinking models** — the *distiller* is the
same endpoint under a stricter discipline:

```python
actor = OpenAICompatibleLLM(model="qwen3:32b", base_url=URL, api_key=KEY)
distiller = OpenAICompatibleLLM(
    model="qwen3:32b", base_url=URL, api_key=KEY,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    max_tokens=1024,
)
services = Services(memory=store, run_end=reflexive_consolidator(distiller))
```

Rationale: distillation is schema-filling over material already in the window —
deliberation adds latency and overrun risk, not quality — while keeping the pass on
preserves same-day learning that option 1 defers to the next maintenance run. This is a
pattern, not a mechanism — nothing above the seam changes; the `--model` flag and
`VSIGMA_LLM_EXTRA_BODY` compose the same way for the deep round's CLI.

## C. Non-goals / deferred

- **Reasoning in the episodic trajectory** — persisting reasoning per step would grow
  episode storage by the largest payload the model produces, and the debugging need is
  served by the live tap (§B.2). If a consumer later wants post-hoc reasoning audit,
  that's an opt-in observer, not a default write. Deferred until asked for.
- **A normalized `thinking=` abstraction** — see §B.3; passthrough is the design, not a
  stopgap.
- **Streaming** — orthogonal; the client is request/response by design for now.
- **Distill prompt durability bar** (dropping run ephemera like execution times from
  distilled facts) — related bumblebee finding, but a construction-engine concern, not a
  seam concern; separate change to `_DISTILL_SYSTEM` and the deep-pass prompt.
  *Landed separately 2026-07-16:* `_DISTILL_SYSTEM` now demands facts useful in a
  future, unrelated session and excludes run ephemera by name (both rounds share the
  prompt, so reflexive and deep get the bar together); drift-pinned by test.

## D. Graduation criteria

- Against a live local thinking model: `enable_llm_logging(DEBUG)` shows the model's
  reasoning for every turn, in both the separate-field and inline-`<think>` serializations.
- A `<think>`-emitting model produces clean final answers, clean episode summaries, and a
  `distill` pass that fails only on *genuine* shape violations.
- Consolidation configured per §B.4 completes on the same endpoint that previously
  overthought past the timeout — demonstrated on bumblebee's setup.

When validated, this graduates into architecture_design's LLM-seam section; the
trajectory-audit observer and the distill durability bar return as their own items.
