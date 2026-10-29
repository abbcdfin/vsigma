# Proposal: Thread-Native Runtime — Session = Thread, Append-Only Messages, Optional Memory

**Status:** **GRADUATED 2026-10-25 (Phase 4.8 completed)**, revision 2. Review folded in:
- `Services` and `wrap` are kept; §F.1 records the dropped alternative.
- `Services.memory` becomes optional (§B.5).
- `Services` is renamed `Interceptor` (§B.8).
- Verdicts §G.1, §G.3 and §G.5 are decided.
- The web-app consumer is not migrated in this phase; it gets migration notes (§D).

§G.7 stays a follow-on. The implementation review (2026-10-25) added §G.8–§G.11. §B.3 and
§G.11 are superseded by Phase 4.9 ([langgraph-messages](./langgraph-messages.md), built
2026-10-25).
**Prompted by:** the question "what does vsigma add over LangGraph?", asked while assessing
a web-app consumer's dogfood (174 offline tests green against HEAD). What that app uses is
the LLM client, the loop building blocks and the `FakeLLM` test kit. What hurts it is the runtime
layer: a hard-coded `MemorySaver`, dialog and paused runs lost on reconnect, a workaround to
keep `send()` from re-entering a budget escalation, and a trajectory nobody reads. Governed
memory is unused there today, but is kept: it must stay one argument away.
**Thesis:** keep the convenience layer, but make it store **nothing LangGraph already stores**.
The conversation lives in the checkpointer's thread, and the session handle is a view of it.
`wrap` stays as the way nodes are written, and as the home of context management.

## A. Problem

vsigma inverts LangGraph's model. In LangGraph a thread is the conversation and each `invoke`
appends to it (an *episode* is a run on the thread). In vsigma an episode *is* a thread
(`run.py:122`, `thread_id = run_id`), and a session is a store key holding the dialog that
`Session.send` copies into each new thread as `history` (`sessions.py:91`). Around every node,
`wrap` (`seam.py:87`) assembles context, writes the node's output to the store, and emits an
event. Four burdens follow:

1. **The conversation is stored three times.** It lives in the checkpoint, the dialog key
   (rewritten whole each turn, `sessions.py:107-110`) and one capture row per node firing.
2. **Storage grows quadratically.** `messages` is a plain overwrite channel (`state.py:42`), so
   every node returns the full list (`react.py:30`, `critique.py:41`, `resilience.py:48`,
   `planning.py:23`), and `wrap` persists each copy (`seam.py:92`).
3. **The context trim destroys history.** The node sees the trimmed window and returns it, and
   the overwrite channel saves the trim. The deep pass therefore reads only the tail, and
   `episode_trajectory` also caps at `REFLEXIVE_WINDOW` steps (`consolidation.py:86`).
4. **Session state is process-local.** `Session.runs` is in memory (`sessions.py:59`), and
   `send` resumes *any* escalation (`sessions.py:84`). The web app reimplements the check to
   avoid re-entering a spent budget.

`wrap` itself is not the burden. Its two harms, points 2 and 3, both come from the overwrite
channel, and B.3 removes them without touching `wrap` (§H.8). Points 1 and 4 come from the
session design.

### A.1 The decision this reverses

`architecture_design.md:80-82`: *"Runs are ephemeral; sessions are durable namespaces … We
reject 'session = one long thread' (checkpoint growth, process pinning)."* Both reasons are
answered here, but only one cleanly:

- **Process pinning.** This was never avoided: a paused run sits on a `MemorySaver` thread
  in one process (the web app's reconnect-affinity constraint). With a persistent saver and a
  handle that holds no state (B.1), any process can continue a thread. The pinning disappears.
- **Checkpoint growth.** This remains, and the thread-per-run design had it worse.
  - Each checkpoint stores the full `messages` list. Under `durability="exit"` that is one
    full copy per turn (§H.6), so an unpruned thread grows quadratically over the session's
    life.
  - Today every run's thread is seeded with the whole dialog, and checkpoints are written
    per *step*. That is quadratic too, with a larger constant.
  - Growth is bounded only by `prune(keep_latest)` on a saver that implements it, or by
    `delete_thread` when the session ends (B.7). The proposal moves the pile to where
    LangGraph's tools can reach it, but does not remove it.

## B. Design

### B.1 Session = thread; episode = a tagged run on it

- `thread_id = session_id`. A one-off `agent.run(task)` is a session with a fresh id.
- Each `send` is one episode. Its `run_id` stays a fresh `uuid4`, never recycled, and the rules
  in `episodes.py` are unchanged. It is passed as `config["metadata"]["run_id"]` along with
  `task_id`, `user_id` and `channel`. Every checkpoint of that run carries the tag (§H.1).
- A turn's input also sets the per-turn state fields: `run_id`, `task_id`, `session_id`,
  `user_id`, `system`, `attempt_count: 0` and `status: "running"`. These are overwrite
  channels, so `wrap` keeps finding the current episode's `run_id` in state, and a resume
  keeps the paused episode's.
- **The handle holds no state.** `Session(agent, id, user_id=…)` reads everything from
  `graph.get_state`, so a handle recreated in another process sees the same dialog and pause.
  Two things are derived from the thread's messages, not stored:
  - `dialog`: computed by the existing `_dialog_turns`;
  - `paused` and `interrupt`: taken from the snapshot's `interrupts`.
- **The system prompt lives in its own `system` state field, not in `messages`** (§G.5).
  - `Agent` sets it on every turn.
  - `react_node` and `plan_node` put it in front of the window they send to the model.
  - A prompt change therefore reaches an existing session at its next turn, and each turn's
    checkpoint records which prompt was used.
  - A consumer's own model-calling node must prepend it too.
  - `SlidingWindowContext`'s first-message pin is no longer needed for the prompt, and stays
    harmless.

### B.2 `send` and `resume` are separate verbs

- `send(text)` starts a new turn, which is a new episode.
- `resume(value)` sends `Command(resume=value)` to the paused episode. It reuses that episode's
  `run_id`, read from the paused checkpoint's metadata.
- Both return a `Run`; the name is unchanged and still means one episode.
  - `Run.interrupt` exposes the interrupt payload, so the app can dispatch on its `kind`.
  - `human_interrupt_node`'s payload gains `"kind": "escalation"` beside its `reason`.
- `send` while paused is a real decision, not a detail. LangGraph **silently abandons** a
  pending interrupt when new input arrives on the thread (§H.3). See §G.1.

The web app's workaround becomes a dispatch on `kind`: a clarification is resumed, an
escalation is answered with `send`.

### B.3 `messages` is append-only (SUPERSEDED by Phase 4.9)

> **SUPERSEDED:** LangGraph's `add_messages` replaced `append_messages` in Phase 4.9; see [langgraph-messages](./langgraph-messages.md).

- `ReActState.messages: Annotated[list[dict[str, object]], append_messages]`. Each node returns
  only the messages it added, and LangGraph appends them.
- `append_messages` is vsigma's own reducer: concatenation plus a **guard that fails loudly**
  when a node returns history back. A node migrated wrongly would otherwise double the history
  silently. The guard compares object identity (§G.11). §H.4 verified the first version, which
  compared content.
- Not LangChain's `add_messages`. It converts messages to LangChain objects, while vsigma's
  `LLM` speaks OpenAI-format dicts.
- Four vsigma nodes change: `react_node`, `critic_node`, `human_interrupt_node` and `plan_node`.
- Under `wrap` this does two things at once (§H.8):
  - the context step becomes view-only, because the trimmed window never flows back;
  - each capture row holds only that node's new messages, so capture grows linearly.

### B.4 Context stays in `Services`, applied by `wrap`

- No code change: `wrap` assembles `Services.context` before the node (`seam.py:88`). After
  B.3 the trim affects only what the node sees. The `ContextManager` protocol is unchanged, so
  a consumer's own context manager and its `add_wrapped` calls stay as they are.
- `wrap` applies context to *every* wrapped node, including ones that never call the model
  (`critic_node`, `human_interrupt_node`). After B.3 this is harmless; the remaining cost is
  wasted work, e.g. `RecallContext` reading the store each time the critic runs.
  - The cheap fix, if that cost ever shows: a per-node switch,
    `add_wrapped(sg, "critic", critic_node(v), svc, context=False)`.
  - Not built until a real cost shows.
- `RECALL_MARKER` stripping is no longer needed to stop injections stacking in saved state.
  It stays harmless.

### B.5 `Services.memory` becomes optional

`memory` is required today because the conversation is stored in it. After B.1 the
conversation lives in the thread, and every remaining store write has only governed memory
as its reader:

| Write | Its reader |
|---|---|
| Capture rows (`seam.py:92`) and the input row (B.6) | consolidation (`episode_trajectory`) |
| `episodes/index` (`run.py:94`) | ownership and scope checks, the maintenance pass |
| Knowledge records | `RecallContext`, maintenance |

So `Services.memory: MemoryStore | None = None`. With `None`:
- `wrap` skips the capture write;
- `Agent` writes no `episodes/index` entry and no input row;
- the conversation is unaffected;
- configuring `run_end` or `RecallContext` without a store raises at construction.

**The trade-off.** Episodes run with `memory=None` leave no transcript, so they can't be
consolidated later. Optional means the app decides, not that off is recommended:
- An app that will never consolidate (a test harness, a script) loses nothing.
- An app that may want governed memory later should turn it on from day one, with a
  persistent store. After B.3, capture is linear, so leaving it on is cheap.

**Rejected alternative:** keep `memory` required, with an `InMemoryStore` default (what the
templates' `_services` does today). It looks simpler, but in a long-running process it grows
unseen and is lost on restart. That is the web app's situation today: an `InMemoryStore` per
connection with no `run_end` and no recall, so every row is written and never read.

### B.6 The episode transcript

- **The capture rows are the transcript.** After B.3, each row holds only what its node added,
  so reading an episode's rows in order (`MemoryStore.query`'s insertion-order guarantee)
  gives the episode's messages, each stored once.
- **The input row.** The turn's own messages come from the input, not from a node, so no node
  captures them.
  - `Agent` writes them as the episode's first row (`input:<uuid4>`), beside `record_episode`
    and before `graph.invoke`.
  - That is the user's message, plus the seeded dialog until slice 2 retires seeding. The
    system prompt is in its own field (B.1), not here.
  - Like the capture rows, it is written only when `memory` is set.
- **`episode_trajectory`** concatenates the rows' messages in order, and the "keep only the
  last cumulative copy" masking goes away.
- **The `REFLEXIVE_WINDOW` cap moves to the reflexive caller.** It is that round's designed
  bound (architecture §9: "≤ 1 call over a capped trajectory window").
  - `reflexive_consolidator` reads the input row plus the last `REFLEXIVE_WINDOW` rows, so the
    task is never cut off.
  - The deep pass (`run_maintenance`) reads the whole transcript.
  - That ends the shared cap that bounded the offline pass (episode-retention §B.3).
- **Session context.** After B.1 an episode's rows hold only its own turn. Today each run is
  seeded with the whole dialog, so distillation sees what came before. A turn such as "yes,
  do that" distils badly alone.
  - So `episode_trajectory` prepends a bounded tail of the same session's earlier
    transcripts, found through `episodes/index`, and marks it as prior context.
  - The bound is a build-time choice, by turns now and by tokens once token-budget-context
    lands.
- No run-end transcript and no `message_offset`: revision 1's B.5 is withdrawn (§F.1).

### B.7 Checkpoint growth belongs to the checkpointer

- `durability="exit"` writes one checkpoint per turn (or per pause) instead of one per step:
  6 → 2 for two turns (§H.5).
  - The pause survives this mode (§H.2).
  - The cost: a crash mid-turn loses that turn's progress.
  - It becomes an `Agent` parameter, `durability="exit"` by default (§G.3).
- Ending a session is `checkpointer.delete_thread(session_id)`.
- `BaseCheckpointSaver` declares `prune(keep_latest)` and `delete_for_runs`, but the base
  methods raise `NotImplementedError`. `InMemorySaver` implements neither, only
  `delete_thread` (§H.7).
- Surviving a reconnect needs a persistent saver. `langgraph-checkpoint-sqlite` is not a
  dependency today, and whether its saver implements `prune` is **unverified**. This is the
  spike's first question: without `prune`, a long-lived session is bounded only by ending it
  (A.1). *Answered 2026-10-26:* it does not; only `RedisSaver` does. `SqliteSaver` works
  across processes, pauses included, once opened with `check_same_thread=False`
  ([thread-retention](./thread-retention.md) §H).
- The app chooses the checkpointer. vsigma never constructs one outside the templates' dev
  default.

### B.8 Naming: `Services` becomes `Interceptor`

The object has three sides, and each gets one word:
- what it holds: strategies;
- what happens at runtime: interception;
- how it is attached: wrapping.

`Services` named only what it holds, after the dependency-injection parameter object.

| Concept | Name |
|---|---|
| The object | `Interceptor` (was `Services`). In the POSA2 Interceptor pattern, a framework calls registered behaviour at defined points, which is what this object supplies |
| Attaching it to a node | `wrap(node, interceptor)` and `add_wrapped(sg, name, node, interceptor)`, unchanged |
| Where it acts | **interception points**: before the node (context), after it (capture, event), and when the run stops (`run_end`). This replaces "the seam" |
| The module | `interceptor.py` (was `seam.py`) |

```python
interceptor = Interceptor(memory=store, context=SlidingWindowContext(), observer=log)
add_wrapped(sg, "act", react_node(llm, tools), interceptor)
agent = Agent(graph, interceptor)
```

**Why the verb stays `wrap`.** It is exact about the mechanism: `wrap` returns a wrapper
function around the node. An `intercept` verb would turn `add_wrapped` into the awkward
`add_intercepted` and break callers for no gain. What the wrapped node does at runtime is
already said by the class name.

**Only the node-level "seam" is renamed.** The **LLM seam** (`llm.py:1`, `llm.py:248`,
architecture §11, the thinking-models proposal) is a different place, the `LLM` protocol, and
keeps its name. A blanket find-and-replace would be wrong.

**Considered and rejected:**
- `Seam`: strictly the *place*, not what plugs into it, and it needed explaining.
- `Harness`: overused in agent tooling.
- `Cover`: plain English, but says nothing about the process.
- `Adapter`: vsigma uses it for interface conversion (tools, MCP, §G.7).
- `Plugin`: vsigma uses it for tool modules (agent-authored-tools).
- `Middleware`: the familiar name for the same pattern, but it invites confusion with
  LangChain v1's model-call middleware.
- `Weave`: matches the docs' verb ("woven around every node"), but is an unusual noun.
- `Envelope`: already the event envelope.
- `Wrapper`: architecture line 23 defines vsigma as "never a wrapper that hides it".

The rename ships in slice 3. The rest of this proposal uses today's name, `Services`.

## C. What stays vsigma's

- **`Services` and `wrap`:** the node-authoring convenience, and the one place context policy
  lives.
- **`episodes/index`: an episode record that outlives the checkpoints.**
  - Pruning or deleting a thread removes the `run_id` tags.
  - A governed-memory record still cites `source_ref=trajectory:<run_id>`.
  - Scope checks still need that episode's owner.
  - The index is unchanged.
- **Governed memory, entire:** records and provenance, the scope ladder, distillation, both
  consolidation rounds, the bundle gate and the maintenance CLI. It is reached through
  `Services(memory=store, run_end=reflexive_consolidator(llm))`.
- The building blocks, routers, the `LLM` client and its repairs, `FakeLLM`, MCP, the bridge.

## D. Build order (thin slices, each shippable)

**The web app is not migrated in this phase** (decided 2026-10-24). Each slice adds its
consumer-facing changes to `docs/upgrading.md`, which is written for the web app and bumblebee
alike. The web-app lines below are the content of those notes.

1. **Append-only `messages`.** Covers the reducer and its guard, the four nodes, the input row,
   `episode_trajectory` concatenating rows, and the cap moving to the reflexive caller.
   - On its own this ends the quadratic growth, the history loss and the deep pass's
     truncation, with `wrap` unchanged.
   - Here the input row still carries the seeded dialog, because thread-per-run remains until
     slice 2.
   - The web app: its clarify node returns `[question, answer]`.
2. **Session = thread.** This covers:
   - the `send`/`resume` split;
   - the stateless handle;
   - run tags in metadata;
   - `durability`;
   - the `system` state field;
   - the session-context prepend in `episode_trajectory`.

   The dialog key and the `turn:` records are retired.
   - **Pre-4.8 persisted dialogs are not migrated** (decided 2026-10-25: no persisted
     sessions are worth keeping). Old keys are left unread, and the session's first 4.8 turn
     starts an empty thread. `docs/upgrading.md` says so.
   - The web app: its resume check becomes a `kind` dispatch.
3. **Optional memory and the `Interceptor` rename.**
   - `Services.memory` defaults to `None`.
   - The construction checks raise for `run_end` and `RecallContext` without a store.
   - The templates stop defaulting an `InMemoryStore`.
   - `Services` becomes `Interceptor`, and `seam.py` becomes `interceptor.py` (B.8). The two
     breaking changes ship together, so the web app absorbs one break, not two.
   - The web app:
     - Either drop its per-connection `InMemoryStore`, or keep memory on with a persistent
       store if governed memory is planned.
     - Its `Services` references become `Interceptor`. Its `add_wrapped` calls keep their
       shape.
4. **Persistent checkpointer (the web app's choice).** Replace its hardcoded `MemorySaver()`
   to get reconnect survival.

vsigma blast radius:
- Modules: `state`, `react`, `critique`, `resilience`, `planning`, `seam` (renamed
  `interceptor`; capture becomes conditional), `run`, `sessions`, `consolidation`, `agents`,
  `__init__`.
- Docstrings only, for the "seam" wording: `context`, `memory`, `observability`, `episodes`.
- Nine test files.
- Docs: `README.md`, `getting_started.md`, and `architecture_design.md` §§3, 4 and 10.

## E. Interactions

- **Graduated decisions this amends** (on acceptance):
  - Architecture §3, the seam: amended, not superseded.
    - It is retitled for the interceptor and its interception points (B.8).
    - Its context step becomes view-only.
    - Its capture holds only each node's new messages.
    - Memory is optional.
  - §4, sessions as memory namespaces: becomes sessions as threads. It reverses the explicit
    rejection at `architecture_design.md:82`, argued in A.1.
  - §10: the "persisted window" wording goes.
  - The single-verb "grain unification" (interaction-model §B, `architecture_design.md:119`):
    reversed by B.2, deliberately.
- **session-dialog-compaction** (DRAFT): its premise, a dialog replayed into each new run, is
  gone. What remains is bounding the *view* (context) and the *thread* (B.7). Supersede it on
  acceptance.
- **episode-retention** (DRAFT):
  - Its §D non-goal (cumulative `messages`) and its `REFLEXIVE_WINDOW` prerequisite are both
    resolved by slice 1.
  - Its checkpoint non-goal becomes reachable through B.7.
  - What it still governs shrinks to linear capture rows plus the tombstones.
- **session-admission** (DRAFT): the author record moves from the retired `turn:` envelope to
  the `episodes/index` entry, which already holds `user_id`, and to the checkpoint metadata.
  `sessions/index` is unaffected.
- **token-budget-context** (DRAFT): unaffected. It plugs into the same `ContextManager` in
  `Services`, and it can bound B.6's session-context prepend.

## F. Non-goals

Replacing the `LLM` client with LangChain models; adopting `add_messages`; semantic recall;
multi-agent work. Unwrapped nodes, including `create_react_agent`'s, run under `Agent` without
context or capture; adopting them is not a goal.

### F.1 Considered and dropped: retiring `wrap` (revision 1)

Revision 1 removed `Services`, `wrap` and `add_wrapped`. In their place it put:
- a `context=` parameter on the model-calling nodes;
- a run-end transcript slice, located by a `message_offset` in the episode index;
- trajectory events taken from `graph.stream(stream_mode="updates")`.

**Dropped** in review, 2026-10-24, for two reasons:
- `wrap` is the convenience that keeps node code to reasoning and action, and it is where
  vsigma's context management lives.
- Its two harms come from the overwrite channel, which B.3 removes without touching `wrap`
  (§H.8).

The capture it already does becomes the transcript (B.6), so the run-end machinery had nothing
left to do.

## G. Verdicts

1. **`send` while paused** — **decided 2026-10-24: raise `SessionPaused`**. The app opts in
   with `send(text, abandon=True)`, or calls `resume()`.
   - Rejected: silently abandoning (LangGraph's behaviour).
   - Rejected: auto-resume (today's behaviour). It treats every pause as a clarification, so
     a budget escalation gets "resumed" with the user's next request as feedback. That was
     the web app's spin.
   - Rejected: auto-resuming only pauses that declare they await a reply. It is not worth the
     extra mechanism.
2. ~~Removal style~~ — **settled in revision 2:** `Services` and `wrap` are kept (§F.1).
3. **Durability default** — **decided 2026-10-24: `"exit"`**, overridable per `Agent`.
   - Each checkpoint stores the full current `messages` (§H.9), so per-step durability stores
     one full copy of the history per step that changes it. `"exit"` stores one per turn.
   - Accepted cost: a crash mid-turn loses that turn's progress.
4. ~~Transcript timing~~ — **moot in revision 2:** capture is incremental per node, and there is
   no run-end transcript (B.6).
5. **System prompt** — **decided 2026-10-24: its own `system` state field, set every turn**
   (B.1).
   - Rejected: frozen into the thread on the first turn. Changing it later would need a
     replace mechanism, because `messages` is append-only.
   - Rejected: injected at view time. The thread would not record which prompt the model saw.
6. ~~Context binding~~ — **settled in revision 2:** context stays in `Services`, applied by
   `wrap`, with the protocol unchanged (B.4).
7. **Storage engine under governed memory** (follow-on: decide after the persistent-checkpointer
   spike, not in this proposal's slices). A persistent checkpointer does not replace
   `MemoryStore`. It takes only what was conversation: the dialog key and the `turn:` records.
   What remains is cross-thread by nature and must outlive thread pruning: the
   `episodes/index`, the capture rows (the episode transcripts) and knowledge records.
   - **Keep the `MemoryStore` protocol as governed memory's interface, and add a thin adapter
     so a LangGraph `BaseStore` can back it (recommended).**
     - `scope.py`, records, consolidation and maintenance are unchanged.
     - The adapter must preserve `query`'s insertion-order guarantee, which B.6 relies on.
     - This gains `delete` (which episode-retention needs), TTL and the optional vector index
       (the parked semantic-recall substrate).
     - The app can then point the checkpointer and memory at one database.
   - Keep vsigma's own `SQLiteStore` only: simple and owned, but a second database with no
     delete.
   - Replace the protocol with `BaseStore` throughout: every governed-memory module would
     change, for no gain over the adapter.

   Unverified: whether a SQLite `BaseStore` ships in `langgraph-checkpoint-sqlite` (it could not
   be installed here, as §B.7 notes). *Checked 2026-10-26, version 3.1.1:* `SqliteStore` ships,
   with `delete` and TTL. Its `search` orders by last update, so rewriting an item moves it to
   the end, and returns 10 items by default. The adapter would have to supply `query`'s
   first-write order itself.
8. **Compatibility aliases** — **decided 2026-10-25, implementation review: none.**
   `Interceptor` is the only name. `Services`, `seam.py`, the templates' `services=` and
   `Agent.services` are removed. One thing gets one name unless there is a specific reason for
   two, and there is none here: the web app must change code for slice 1 regardless, and
   `docs/upgrading.md` carries the rename.
9. **Where observer events come from** — **decided 2026-10-25, implementation review: the
   run's LangGraph stream.** `Agent.run` and `Run.resume` drive
   `graph.stream(stream_mode=["updates", "values"])` and emit one `TrajectoryEvent` per node
   update. `wrap` keeps context assembly and capture. Public names and event fields are
   unchanged.
   - Gained: one emission point, and every node is observed, wrapped or not.
   - Accepted cost: a hand-built graph run with a raw `graph.invoke` still gets context and
     capture from `wrap`, but no events. No consumer runs that way.
   - Capture rows stay in `wrap`. They are distillation's source and are read long after the
     stream is gone.
10. **The default observer** — **decided 2026-10-25, implementation review: none.**
    `Interceptor.observer: Observer | None = None`, the same pattern as `memory`. With no
    observer, no events are built or kept.
    - Why: the events serve debugging and inspection only. Their one reader is
      `Run.trajectory`, and nothing in vsigma acts on them. Defaulting to `TrajectoryLog` kept
      every event of every run in process memory whether or not anyone read it. In a
      long-running server that list grows without bound.
    - `TrajectoryLog` stays as the opt-in receiver for tests and debugging. Whoever passes one
      owns its growth. `LoggingLLM` already works this way: silent by default.
    - `Run.trajectory` raises when no `TrajectoryLog` recorded the run. It no longer returns
      `[]`, which would hide a missing receiver.
11. **What the `messages` guard compares** — **decided 2026-10-25, implementation review:
    object identity (SUPERSEDED by Phase 4.9).** `append_messages` raises when any message object a node returns is
    already in the saved history. This replaces the content comparison that §B.3 first
    specified.
    - Why: the guard's one job is to catch a node handing back messages it was given. Such a
      node returns the very objects LangGraph passed it, even after a checkpoint round-trip: a
      node that ran after a resume echoed the same objects. Equal content only approximates
      this, and it failed both ways:
      - False positive: a correct node that repeats a message pair crashed the run with
        `ValueError` before the loop-breaker could escalate. A model that retries a failing
        tool call with the same call id produces that pair. `llm.py:192` stores `""` when the
        server omits the id.
      - Miss: `RecallContext` puts its message at the front of the window, so a node that
        echoes the window does not start with the end of the history.
    - One echoed object is enough to raise; the ≥ 2 threshold is gone. A genuine repeat is a
      fresh dict and passes at any length.
    - Rule for node authors: build new message dicts, and never return a message object read
      from state.
    - A compiled subgraph that shares the `messages` key must not be added as a node directly.
      LangGraph returns the subgraph's whole `messages` list as that node's update, including
      the parent's messages. If the subgraph runs straight through, they are the same objects
      and the guard raises. If it pauses inside, resume reloads them as new objects, the guard
      sees nothing, and the history silently duplicates. Call the subgraph from a node and
      return only the messages it added. The content guard also duplicated silently, on a
      first turn. [langgraph-messages](./langgraph-messages.md) proposes the fix.
    - Accepted cost: if a context manager rebuilds message dicts instead of slicing or
      filtering the list, as `SlidingWindowContext` and `RecallContext` do, the guard cannot
      see an echo of those messages.
    - Rejected: dropping the guard (plain `operator.add`). Correct nodes never need it, but a
      wrong node would then duplicate history silently. Fixing today's nodes does not cover
      bumblebee or nodes written later.
    - Rejected: dropping echoed objects instead of raising. It makes subgraph nodes work, but
      `wrap` saves the node's raw update as the capture row (`interceptor.py:87`). An old-style
      node would then fill the capture rows with repeated history and nothing would fail.
    - Checked against `4fa94f5` with the identity check swapped in: 176 of 177 tests pass. The
      exception is `test_append_messages_allows_one_repeated_message`. It models a legitimate
      repeat by passing the history's own object back, which is now an echo by definition, so
      it should use a fresh dict.

## H. Evidence (LangGraph 1.2.6, scratch scripts, 2026-10-24)

1. Two invokes on one thread with `metadata.run_id` 1111 and 2222: messages accumulate across
   turns, and every checkpoint carries its run's tag. `saver.list(cfg, filter={"run_id": …})`
   returns one episode's checkpoints. `saver.list(None, filter={"user_id": …})` finds a user's
   threads across all threads.
2. `stream(stream_mode="updates", durability="exit")` yields `[act]`, then `[__interrupt__]`.
   The paused snapshot has `next=("clarify",)` and `interrupts=[{"kind": "clarification", …}]`.
   `Command(resume="A123")` completes the run and appends both messages.
3. New input on a paused thread: `interrupts` becomes `[]` and the clarify node never runs. The
   pause is dropped without error.
4. The guard reducer raises `node returned 3 messages already in history` when a node returns
   `messages[-3:] + [new]`.
5. Checkpoints after two single-node turns: 6 with the default durability, 2 with `"exit"`.
6. Four `"exit"` turns on one `InMemorySaver` thread keep four `messages` blobs, of 60, 119,
   178 and 237 bytes. Each is a full copy, so growth is linear per turn and quadratic in total.
7. `InMemorySaver.delete_for_runs` raises `NotImplementedError`. Of the retention methods,
   `langgraph/checkpoint/memory` defines only `delete_thread`.
8. Today's `wrap` and `add_wrapped`, unchanged, ran over a state whose `messages` uses an append
   reducer, with `SlidingWindowContext(max_messages=3)`. Over four turns on one thread:
   - the node saw at most 3 messages;
   - the thread kept all 8;
   - the episode's capture row held only the node's one new message.
9. Two turns of three node steps each, append reducer:
   - Per-step durability: 10 checkpoints and 8 `messages` blobs (27 → 239 bytes, 1064 total).
     Each blob is the full list at that moment.
   - `"exit"`: 2 checkpoints and 2 blobs (120 and 239 bytes, 359 total).
