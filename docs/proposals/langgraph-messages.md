# Proposal: LangGraph Messages — `add_messages` Replaces vsigma's Append Reducer

**Status:** **GRADUATED (Phase 4.9 completed 2026-10-25)**; supersedes §B.3 and §G.11 of
[thread-native-runtime](./thread-native-runtime.md).
**Prompted by:** the Phase 4.8 implementation review. vsigma's `append_messages`
(`state.py:26`) concatenates, plus a guard against nodes that hand history back. The guard had
two designs in one day, content then identity, and both leak (§A.1).
**Thesis:** 4.8 decided vsigma stores nothing LangGraph already stores. The `messages` channel
is the one place vsigma still keeps its own version of a LangGraph mechanism. Use LangGraph's.

## A. Problem

1. **Concatenation cannot tell an echo from a new message, and the guard cannot either.**
   Nothing about a message dict survives a checkpoint round-trip and identifies it.
   - The content guard (§B.3) crashed correct runs on a repeated tool-call pair. It missed
     echoes behind `RecallContext`. For a subgraph node on a first turn, it duplicated the
     user's message silently.
   - The identity guard (§G.11) loses track after a resume. A subgraph node that pauses inside
     reloads its copy of the parent's messages as new objects. After resume the history
     duplicates, with no error: `['go', 'go', 'answer=42']` (§H.3).
2. **Subgraph composition is planned.** `architecture_design.md` §4 settles "subgraph
   composition (child graph as a node; same run)" for Phase 5. Under `append_messages` that is
   exactly the case that either raises or silently duplicates.
3. **Nothing can be removed.** A session thread can only grow. Under `"exit"` durability every
   turn's checkpoint stores the full list (thread-native-runtime §H.6: linear per turn,
   quadratic in total). Any later compaction needs a way to remove messages from the thread.
4. **Node authors carry a rule** ("build new message dicts, never return one read from state")
   only because the reducer cannot recognise an echo.

## B. Design

### B.1 `messages` uses `add_messages`

- `ReActState.messages: Annotated[list[AnyMessage], add_messages]`. State holds LangChain
  messages, each with an id; `add_messages` assigns one to any message that lacks it.
- Nodes may keep returning OpenAI-format dicts, because `add_messages` converts them (§H.1).
  Only code that reads messages changes.
- A returned message whose id is already in the history replaces it instead of being appended.
  Echoed history, trimmed windows and subgraph output therefore merge (§H.2, §H.3). This holds
  across a pause, because ids are stored in the checkpoint.
- `append_messages`, its guard and its seven tests are deleted. "Return only what you add"
  stays as a convention; breaking it no longer corrupts or crashes anything.
- `langchain-core` becomes a declared dependency, because `state.py` imports it at module
  level. LangGraph already requires it, so nothing new is installed. The claim in
  `langchain.py` that vsigma does not hard-depend on it no longer holds.
- Semantics change: the history is no longer immutable. A node that returns a message with an
  existing id and new content edits it, and `RemoveMessage(id=...)` deletes one (§H.4). vsigma
  itself does neither in this proposal.

### B.2 Boundaries stay OpenAI dicts

Where messages leave the graph state, they are converted with `langchain_core`'s
`convert_to_openai_messages`. The conversion is lossless for every shape vsigma builds (§H.1).

- **LLM protocol unchanged:** `LLM.complete(messages: list[dict], tools)`. `FakeLLM`,
  `StaticLLM`, `OpenAICompatibleLLM`, `LoggingLLM` and consumers' own `LLM`s do not change.
  - The two nodes that send state messages to the model convert first: `react.py:30-38` and
    `planning.py:23-32`.
  - Construction, estimation and the critique judge build their own dicts and do not change.
- **`wrap` drops what the node was handed; capture rows stay OpenAI dicts.** `wrap`
  (`interceptor.py:83-88`) removes from the node's update every message object that was in
  the window it passed in. It returns that cleaned update and writes the capture row with the
  remaining messages converted to OpenAI dicts. Consolidation, `episode_trajectory` and the
  input row are unchanged.
  - Identity is exact here, unlike in §G.11 of thread-native-runtime. Assembly and the node
    run in one call, with no checkpoint in between.
  - *Amended 2026-10-25, when the board was written.* The first version filtered only the
    capture row, by message id. A node that echoes its window would then save
    `RecallContext`'s injected message, which has no id, into the thread. Verified: without
    the filter the history is `['go', '[vsigma:recall] fact', 'x']`; with it, `['go', 'x']`.
    4.8's guard raised in this case, so dropping it unfiltered would be a regression.
- **`Session.dialog` keeps returning dicts.** It converts before calling `dialog_turns`.

### B.3 Readers inside the graph see LangChain messages

They use `m.type == "tool"` and `m.content` instead of `m.get("role")` and `m["content"]`:

- `context.py:56`, where `SlidingWindowContext` drops a leading tool message;
- `context.py:96-109`, where `RecallContext` filters its marker and injects a
  `SystemMessage`;
- `estimation.py:24-28`, which reads the latest user text;
- the `ContextManager` protocol, because the window now holds LangChain messages, so custom
  context managers change;
- `Run.state["messages"]`, which returns LangChain messages;
- the tests: 18 lines in 7 files read `messages` from state.

## C. Consumers

- **A web app** with its own graph:
  - A context manager that reads roles and injects a system message changes with B.3. It also
    needs 4.8's `assemble` signature change.
  - A clarify node that returns cumulative history must change under 4.8. Under this proposal
    it works as it is, because the echoed messages merge by id. Returning only its two new
    messages stays the cleaner form.
  - Code that reads `session.dialog` is unchanged (B.2).
  - Scripts that read `run.state` messages as dicts must read LangChain messages.
  - Code that builds dicts for `llm.complete` directly is unchanged.
  - A second loop that already uses LangChain messages with a LangChain chat model then shares
    one message type with vsigma's.
- **bumblebee:** it uses only `make_guarded_react_agent` with `SlidingWindowContext`
  (`core/agent.py:349-361`) and reads no messages. No change beyond 4.8's.

**Timing:** neither consumer has migrated to 4.8, and nothing is pushed. Landing this before
the 4.8 consumer notice means one migration; landing it after means two.

## D. Build order

The type change is atomic, because state cannot hold both kinds of message at once.

1. **The switch:** B.1–B.3 and the tests, in one slice. The gate proves it.
2. **Docs:**
   - `upgrading.md`: this replaces the 4.8 guard and subgraph notes;
   - `architecture_design.md` §3;
   - `thread-native-runtime` §B.3 and §G.11, marked superseded.

Removal is enabled but nothing uses it yet (F).

## E. Alternatives considered

- **Keep `append_messages` with the identity guard (the status quo, §G.11).** It costs
  nothing now. It keeps the silent duplication after a paused subgraph, blocks Phase 5's
  subgraph composition, offers no removal, and keeps the rule for node authors.
- **vsigma's own ids on dicts:** the reducer assigns an `id` key and merges by it. This fixes
  subgraphs, because ids are stored in the checkpoint, and keeps dicts at +37 B per message
  (§H.5). It has three costs:
  - it is a second implementation of `add_messages`: merging now, removal later;
  - the `id` must be stripped before the server sees the messages (unverified whether
    OpenAI-compatible servers reject unknown message keys);
  - LangGraph's prebuilt nodes, such as `ToolNode`, still could not run on vsigma state.

## F. Non-goals

- **Compaction** (summarise, then `RemoveMessage`). This proposal makes it possible; building
  it belongs in a separate proposal.
- **Moving `state["system"]` into `messages`.** Ids would allow a replaceable `SystemMessage`,
  but §G.5's field already records which prompt the model saw.
- **LangChain messages or chat models in the `LLM` protocol.**

## G. Verdicts (decided 2026-10-25)

1. **Adopt `add_messages`** — **decided: adopt.** Both alternatives in E are rejected for the
   reasons given there.
2. **Sequencing** — **decided: hold the 4.8 consumer rollout until this lands**, so
   the web app and bumblebee migrate once. Rejected: ship 4.8 now and migrate them twice.
3. **Boundaries** — **decided: the `LLM` protocol, capture rows and `Session.dialog` stay
   OpenAI dicts.** Rejected: LangChain messages throughout, which would change every `LLM`
   implementation and every reader of memory for no gain inside the graph.
4. **Checkpoint size** — **accepted: +168 B per message (§H.5).** That is ×1.11 with
   2,000-character messages and ×4.3 with 10-character ones. Under `"exit"` durability every
   turn's checkpoint stores the full list, so this multiplies the growth in
   thread-native-runtime §H.6 until compaction exists.

## H. Evidence (LangGraph 1.2.6, langchain-core 1.4.8, scratch scripts, 2026-10-25)

1. **Round trip.** Ten OpenAI dicts went through `add_messages` and back through
   `convert_to_openai_messages`, and all came back equal. They cover every shape vsigma builds:
   `RecallContext`'s system message, a user message, an assistant message with two tool calls,
   their results, a tool call and result with an empty id (`llm.py:192`), a critique message,
   a plan and a plain answer. They were stored as `SystemMessage`, `HumanMessage`, `AIMessage`
   and `ToolMessage`, each with an id.
2. **Echo.** Under `add_messages`, a node that returns `[*state["messages"], new]` leaves
   `['go', 'x']`.
3. **Subgraph node sharing `messages`.** A plain subgraph gives `['go', 'from sub']`. One that
   pauses inside gives `['go', 'answer=42']` after resume. The same paused case under the
   identity guard at `bed12b0` gives `['go', 'go', 'answer=42']`, with no error.
4. **Removal.** After three turns on an `InMemorySaver` thread, `update_state` with
   `RemoveMessage` for the first four of six messages leaves `['three', 'x']`.
5. **Size.** Measured with `JsonPlusSerializer`, which is what `InMemorySaver` stores, over
   100 messages:
   - LangChain messages cost +168 B each over dicts, whatever the content size: ×4.27 with
     10-character contents, ×1.11 with 2,000-character ones;
   - by type, the extra is 153–187 B. The serializer stores each message as its module path,
     class name and loader name (about 60 B), plus every field of the model, even empty
     defaults such as `additional_kwargs`, `response_metadata` and `name` (about 60 B), plus
     the 36-character id. `AIMessage` adds `invalid_tool_calls` and `usage_metadata`, and
     `ToolMessage` adds `artifact` and `status`;
   - dicts with a uuid hex `id` field cost +37 B each.
