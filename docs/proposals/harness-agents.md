# Proposal: Headmaster — Reusable Harness Drivers and `Agent` Integration

**Status:** DRAFT 2026-11-13, awaiting its verdicts (§F). Waits: §E.1, the tool bridge, for a
later phase.
**Prompted by:** some frontier labs sell a plan for their coding agent and no direct API for
the model. An app can reach those models only through the agent program.

## A. Problem

1. **vsigma can only drive a model through an API.** Every template is a LangGraph flow around
   `LLM.complete(messages, tools)` (`llm.py:61`). A coding harness (Claude Code and its like)
   offers no such call: it keeps its own conversation, calls tools in its own loop, and answers
   with the finished turn.
2. **What an app builds against is not the flow.** It is `Agent`, `Session` and `Run`: sessions
   with an owner, one operation per session at a time, expiry, `run_context()` in tools,
   episodes and `maintain()`. None of that depends on who runs the model loop.
3. **Some apps own their runtime.** They need the vendor driver and nothing else: an `Agent`
   would be a second owner of their sessions and state.

So there are two things to build: a public driver for a harness, usable on its own, and a
template that puts it behind `Agent`.

## B. Design

### B.1 The shape

```python
harness = ClaudeCodeHarness(folder=FOLDER, mcp_servers=SERVERS)   # signed in on this machine
agent = make_harness_agent(harness, system=PROMPT,
                           checkpointer=saver, interceptor=Interceptor(memory=store))

session = agent.session(session_id, user_id=user_id, run_context=services)
print(session.send("where is order 17?").result)
session.end()
```

- **`make_harness_agent`** is a template beside `make_react_agent` and returns the same `Agent`.
  `run`, `session`, `send`, `end`, ownership, `SessionBusy`, expiry, episodes and `maintain()`
  behave as they do today.
- **Inside it is a graph of one node.** The node runs one turn on the harness. The graph is
  there for the thread: `Agent`, ownership, locking, expiry and pruning all work on a LangGraph
  checkpoint (`run.py`, `sessions.py`, `expiry.py`), and one node reuses them unchanged.

A driver is the object for one vendor's harness, in `vsigma.harnesses`. It is asyncio code,
and a turn is a handle its caller can watch and stop:

```python
class Harness(Protocol):
    async def turn(self, session: str | None, message: str, *,
                   system: str | None) -> HarnessTurn: ...
    async def close(self, session: str | None = None) -> None: ...
    async def drop(self, session: str) -> None: ...

class HarnessTurn(Protocol):
    def events(self) -> AsyncIterator[HarnessEvent]: ...
    async def wait(self) -> Turn: ...
    async def stop(self) -> None: ...

@dataclass(frozen=True)
class HarnessEvent:
    kind: Literal["started", "progress", "finished"]
    session: str | None                 # the harness's own id, once it has one
    message: dict[str, object] | None   # progress: one message, shaped as in `Turn.messages`
    turn: Turn | None                   # finished: how the turn ended

@dataclass(frozen=True)
class Turn:
    session: str | None
    status: TurnStatus                  # how the turn ended (§B.8)
    text: str                           # the final answer, or a diagnostic
    denied: list[str]                   # tools the harness refused during the turn
    messages: list[dict[str, object]]   # the turn in OpenAI message shape, tool calls included
```

`session=None` starts a conversation, and its id goes to every later turn. §B.8 is the
contract, and the probe (§C) found nothing against this shape. `FakeHarness` keeps the same
contract with scripted turns, so tests stay offline.

### B.2 One turn

vsigma's `Agent.run`, nodes, `Session.end` and expiry are synchronous, so the template drives
the driver through an `AsyncBridge` (`bridge.py`), the loop thread vsigma already uses for MCP.

The node reads the new user message and the harness's session id from the thread's state,
starts the turn and waits for it. A `completed` turn gives the messages, the answer as `result`,
`status="done"` and the id. Any other status raises, so the run fails as any node failure does
and the episode is marked `failed`. On a failure or the time limit, the node stops the handle
and waits for its bounded cleanup before the session's lock is released.

The node is wrapped by the `Interceptor` like any node, so the turn lands in the episode's
context as a react step does. Only the new message is sent: the harness holds the conversation,
and the thread holds the record of it, which `Session.dialog`, the app and `maintain()` read.

### B.3 Tools

A harness can only call a tool it knows itself: one of its own, or one on an MCP server it is
connected to. So tools are equipped on the harness: the driver is given the MCP servers to
connect to and the names of the harness's own tools to allow.

- **The harness calls them directly.** vsigma is not in the call, and reads it afterwards from
  the turn's messages, so the episode still records it.
- **The harness's own tools are off unless named.** A shell or file tool on the daemon's host
  is reachable by whoever is chatting.
- **A tool cannot tell whose work it is doing.** It runs outside the app's process, so
  `run_context()` does not exist there, and the setup is the same for every session of the
  agent. That suits an agent serving one user.
- **`make_harness_agent` takes no `tools`.** A `Tool` is a Python function in the app's process
  (`tools.py:53`), which a harness cannot call without the bridge of §E.1.

### B.4 Two records of one session

A session has vsigma's thread and the harness's transcript. Four rules keep them together:

1. **The harness's id lives in the thread's state**, so it is found and deleted with the thread.
2. **Deleting a thread drops the harness's session**: its process is stopped and its transcript
   deleted. That covers `Session.end` (`sessions.py:163`), `Run.end` (`run.py:600`), forgetting
   and expiry (`expiry.py:231`, `:272`).
3. **A session's process is kept between turns, and may be gone at any of them.** The driver
   closes one left idle past its limit, and the next turn resumes the conversation by id. A
   restart while idle is that case. A restart during a turn leaves its side effects uncertain,
   and the turn is not sent again.
4. **A turn has a time limit.** When it passes, the turn is stopped as `stop` would, ends as
   `timed_out` and the run fails. The transcript is kept: only deleting the thread drops it.

A turn whose transcript is gone (another machine, a cleaned folder) raises `HarnessSessionLost`.

### B.5 What a harness agent does not take

Each of these raises when the agent is built or the turn starts; none is ignored.

- **A context manager other than `PassthroughContext`.** The harness assembles its own window,
  so `SlidingWindowContext` and `RecallContext` have nothing to shape. Recalled memory therefore
  does not reach a harness agent yet (§E.2).
- **A different system prompt on a later turn.** The harness fixes it at the session's first.
  The template keeps what it started with in the thread's state and checks it.

What stays, with a difference:

- **`maintain()`**: expiry and pruning by default; distilling takes `distiller=`, an `LLM`, as
  today. An app with no API passes none and distils nothing.
- **Pauses**: a harness agent never pauses, so `resume` has nothing to resume. A permission the
  configuration does not allow is denied. The denial goes to the model, which normally carries
  on, and the turn's status is however the turn ends.
- **Observability**: one `TrajectoryEvent` per turn, with the turn's tool calls in the episode.
  The driver's progress events (§B.8) are not forwarded to vsigma's observers yet (§E.2).

### B.6 A harness session sees only the app's setup

The harness is a program with a setup of its own on the host: project instructions, user memory,
hooks, plugins, other MCP servers, the working folder. None of it may reach a served session,
and one session may not learn what another said. Each driver has to show this.

For Claude Code the probe (§C) showed these do it: `--system-prompt`, `--tools ""`,
`--mcp-config` with `--strict-mcp-config`, `--setting-sources ""`, `--disable-slash-commands`,
`--permission-prompts none`, and the setting `autoMemoryEnabled: false`. Without that setting a
memory folder, kept per working folder, is read into every session started there.

### B.7 Plan terms

A driver starts the vendor's unmodified program under the login already on the machine. It
never reads, stores or forwards a credential.

Claude Code's [terms](https://code.claude.com/docs/en/legal-and-compliance) let a user sign in
"to the unmodified Claude Code binary with their own Claude subscription", forbid a developer to
"collect, store, or intermediate Claude.ai credentials or session tokens", and say plan limits
"assume ordinary, individual usage". They do not address scripted use. Serving other people from
one plan is outside what they describe, and the docs for this feature say so.

### B.8 A harness on its own

This is the driver's contract, for a direct consumer and for the template alike. A direct
consumer builds no `Agent`, graph, memory store or checkpointer, and keeps its own identities,
scheduling and records. The driver owns the vendor's arguments, the decoding of its messages,
its session ids, and the process it starts.

- **The module stands alone.** It imports nothing from the graph, memory or checkpoint side,
  and its types need no `Session` or `Run`.
- **Configuration is the caller's.** It is given when the driver is built, and the system
  prompt with each turn; the driver stores none of it. The caller keeps which configuration
  belongs to which session and checks it before resuming.

  ```python
  ClaudeCodeHarness(
      folder: Path,                        # where the program runs
      mcp_servers: Mapping[str, object] = {},  # as in the program's own MCP configuration
      allowed_tools: Sequence[str] = (),   # tools the model may call without asking
      own_tools: Sequence[str] = (),       # the harness's own tools to switch on
      host_settings: Sequence[str] = (),   # which of the host's settings to load
      model: str | None = None,
      turn_timeout: float = 600.0,
      idle_timeout: float | None = None,   # close a session's process left idle this long
  )
  ```
- **Its defaults are the isolated ones of §B.6.** A caller that wants the harness to work in a
  real folder with its own tools and instructions names them.
- **It serves any role and supplies no terminal.** It talks to the program over structured
  pipes. A caller that hands a conversation to a person does that itself: it closes the
  session, which confirms the process has ended, and opens the vendor's program on the same
  session id; a later turn resumes by id. Claude Code keeps the conversation through that
  round trip (§C).

**A turn.**

- A turn runs in its session's process, which `turn` starts, or resumes by id, when none is
  running. `turn` returns its handle once the message is with the program. A failure before
  that is raised there, and a process it created is cleaned up, also when the call is
  cancelled.
- The handle's events are ordered: `started`, any number of `progress`, and exactly one
  `finished` carrying the `Turn`. They carry the session id once known. Each `progress` is one
  message: the model's text, its tool calls, or a tool's result. `Turn.messages` is those
  messages in order.
- `finished` stays observable when the process ends before anyone reads events. `wait` returns
  the same `Turn` and may be awaited more than once. Cancelling a waiter or closing the event
  iterator ends the watching only; it is not `stop`.
- Malformed output, a closed stream and a nonzero exit are never a successful answer.
- A second turn on a session whose turn is still running is refused. A stopped turn is over
  only when the program has reported its end, so its late events and refusals cannot reach
  the next turn.

**How a turn ends.**

| `TurnStatus` | Meaning |
|---|---|
| `completed` | The harness reported the turn finished, with an answer. |
| `failed` | The harness reported an error, or its process ended without finishing the turn. |
| `cancelled` | The caller's `stop` ended the turn. The session's process may still be running. |
| `timed_out` | The turn's time limit ended it, in the same way. |
| `outcome_unknown` | The driver could confirm neither that the turn ended nor that the process stopped. Whatever the backend reported is kept in the diagnostic. |

`completed` says the harness finished its turn. Whether the work is acceptable is the caller's
judgment, and a tool's outside effects may be uncertain under any other status. The driver
never sends a turn again by itself.

A tool the harness refuses does not end the turn: the model is told, carries on, and the `Turn`
names the tool in `denied`, which is empty when nothing was refused. It is a diagnostic and
changes no status. Claude Code never ends a turn waiting for an answer (§C), so there is no
status for that.

**Stop, close and drop.**

| Operation | Effect |
|---|---|
| `stop()` on a turn | Interrupts the turn. The process and the conversation are kept. |
| `close(session)` | Shuts down the session's process and its process group. The conversation is kept. |
| `drop(session)` | Closes the session and deletes its transcript. |

- **`stop`** is harmless to call twice. When the program does not report the turn's end within
  a bounded time, the driver closes the session instead: the turn is `cancelled` if the
  process is confirmed gone, and `outcome_unknown` if not.
- **`close`** asks the program to end, then kills its process group. It works with no turn
  running, does nothing when no process is, and closes every session when none is named: an
  app calls it on exit, on restart, and before a person takes a conversation over.
- **A session whose process could not be confirmed stopped is blocked.** `close` raises, and
  the session takes no turn until a later `close` succeeds.
- **`drop`** reports a cleanup that failed.
- The driver supervises the whole process tree it started, and reads the program's output
  whether or not anyone reads its events. It starts the program in a process group of its
  own: a killed program leaves its tool servers running until the group is killed (§C).
- A transcript that is missing or unusable raises `HarnessSessionLost`. A new conversation is
  never started in its place.
- An operation a driver does not support raises. Each driver documents what it supports; a way
  to declare capabilities comes with the second backend.

A direct consumer installs vsigma's dependencies and follows its releases. That is accepted
until it costs a consumer something; the first rule keeps moving the module out cheap.

### B.9 Claude Code as the driver uses it

This is the vendor's interface, as the probe saw it on Claude Code 2.1.323 (§C). It can change
with a version, so the driver ignores events and fields it does not know, and
`vdesign/utils/harness_probe.py` is rerun against a new one.

**Starting the program.**

| Purpose | Options |
|---|---|
| No terminal, events as JSON lines | `-p --output-format stream-json --verbose` |
| Kept alive, one JSON line per message | `--input-format stream-json` |
| A new conversation, or one continued | `--session-id <uuid>`, or `--resume <uuid>` |
| The system prompt | `--system-prompt <text>` |
| Its own tools | `--tools ""` for none, or their names |
| MCP servers, and only those given | `--mcp-config <file> --strict-mcp-config` |
| Tools the model may call | `--allowedTools=<names>` |
| Nobody to ask for permission | `--permission-prompts none` |
| Nothing of the host's setup | `--setting-sources ""`, `--disable-slash-commands`, `--settings '{"autoMemoryEnabled": false}'` |

**What the driver writes** to the program's input:

| Purpose | A JSON line |
|---|---|
| A user message | `{"type": "user", "message": {"role": "user", "content": "<text>"}}` |
| Stop the running turn | `{"type": "control_request", "request_id": "<id>", "request": {"subtype": "interrupt"}}` |

A process started for one turn takes the message as plain text on its input instead.

**What the program writes**, one JSON object a line. Every one carries `session_id`.

| `type`, `subtype` | What the driver takes from it | Becomes |
|---|---|---|
| `system`, `init` | First in every turn: `tools`, `mcp_servers` with their status, `model`, `claude_code_version` | `started` |
| `assistant` | `message.content`: blocks of `text`, or `tool_use` with `id`, `name` and `input` | `progress` |
| `user` | `message.content`: blocks of `tool_result` with `tool_use_id`, `content` and `is_error` | `progress` |
| `result` | Last in a turn: `subtype`, `is_error`, `terminal_reason`, `result` (the answer), `permission_denials`, `usage`, `total_cost_usd`, `duration_ms` | `finished` |
| `control_response` | The answer to a control request, by `request_id` | |
| `system`, `permission_denied` | A refused tool call, also listed in the result | |
| `rate_limit_event` | The plan's usage | |

**How a turn was seen to end.**

| What happened | What the program reports | Status |
|---|---|---|
| An answer | `result`: `success`, `terminal_reason` `completed` | `completed` |
| A tool was refused | The same, with the tool under `permission_denials` | `completed`, with `denied` |
| The interrupt request | `control_response`, then `result`: `error_during_execution`, `is_error`, `terminal_reason` `aborted_tools`; the process stays | `cancelled` or `timed_out` |
| The program terminated | Exit 143, its tool servers ended; no result to rely on | by who asked |
| The program killed | Exit by signal, its tool servers still running | by who asked |
| `--resume` of an unknown id | Exit 1, "No conversation found with session ID" | raises `HarnessSessionLost` |
| `--session-id` of an existing id | Exit 1, "Session ID … is already in use", no events | raises at start-up |
| No message given | Exit 1, "Input must be provided…" | raises at start-up |

**Where a conversation lives.** One file, `<session id>.jsonl`, in a folder of the user's
Claude Code directory named after the working folder it began in. Any folder can resume it
by id, and `drop` deletes that file.

**Seen once, unexplained.** One of about fifty turns produced no output for two minutes and
was killed; the same turn then ran normally four times. The time limit of §B.4 is what
covers it.

## C. Build order

0. **Probe**: done 2026-11-13 on Claude Code 2.1.323. `vdesign/utils/harness_probe.py` reruns
   it, and shows the message formats the driver needs. What it found:
   - **Start-up.** A one-word turn takes 5 to 9 s when the program starts for it, and 1 to 2 s
     in a process kept alive after its first turn (§F.2).
   - **A turn.** The message goes in on stdin, one JSON line per message when the process is
     kept alive. Events come out as JSON lines, a tool call and its result before the answer.
   - **Resume.** A new process continues a conversation by id, also after the program was
     terminated or killed in the middle of a tool call. An unknown id exits with "No
     conversation found"; starting an id that already exists is refused.
   - **Isolation.** With the options of §B.6, a fact told to one session is unknown to
     another, and instruction files in the working folder are not followed.
   - **Refusal.** A tool that is connected but not allowed is refused at once; the turn still
     completes, and its result lists the refusal. The model is shown a connected tool whether
     it is allowed or not.
   - **Stop.** An interrupt request ends a running turn at once and leaves the process
     usable. Terminating the program ends its tool servers too; killing it does not.
   - **Storage.** One file per conversation in the user's Claude Code folder. Deleting it is
     `drop`.
   - **A person takes over.** By hand: a conversation opened in the interactive program after
     a headless turn, then continued headless, kept both parts. The interactive program asks
     once to trust the folder, and records what is typed in the user's prompt history.
   - **For the driver.** Pass the message on stdin, never as an argument: an option that takes
     a list swallows it, and arguments show in the process list. Start the program without
     the environment variables of a Claude Code session the caller may itself be running in.
1. **The driver**: `Harness`, its events, `Turn`, `FakeHarness` and `ClaudeCodeHarness`, usable
   without an `Agent`. Offline tests: ordered progress and an early finish; each status; a
   cancelled waiter; a refused second turn; stop racing completion; a stopped turn's late
   events and refusals kept out of the next turn; `close` with and without a running turn,
   and its escalation to the process group; a session blocked after an unconfirmed stop; a
   process closed when idle and resumed; cleanup when start-up is cancelled; `drop`; a lost
   transcript; a turn driven from synchronous code through `AsyncBridge`. A live scenario,
   `vdesign/utils/live_harness_scenario.py`: two isolated sessions, an MCP tool call, a turn
   stopped and the same process used again, a session closed and resumed, then both dropped.
2. **The template**: `make_harness_agent` on that driver. Tests: two turns with their tool
   calls in the episode; ownership and `SessionBusy` as for a react agent; each refusal of
   §B.5; a turn that does not complete fails the run and frees the lock; every thread deletion
   drops the harness's session. The live scenario runs once more through `Agent`.

At DONE the README's "vsigma builds on LangGraph as its state-machine runtime" becomes: an app
builds against vsigma, and underneath is a LangGraph flow over a model API or a coding harness.
`architecture_design.md` §2 gains the template and a section for the driver's contract.

## D. Alternatives

- **A harness as an `LLM`.** `complete` must hand tool calls back to vsigma, and a harness runs
  them itself. Forcing it means prompting for JSON and parsing the reply, and resending the
  history each call: the harness's two strengths are lost.
- **A community wrapper that serves an OpenAI-style API from the agent.** Those surveyed return
  text only or get tool calls by the same parsing; one claims more and was not verified. The
  ones that reuse the login token break the terms in §B.7.
- **A second `Agent` without LangGraph.** About 1,500 lines of sessions, locking and expiry
  rewritten to behave the same.
- **A separate package or repository for drivers.** It would spare a direct consumer vsigma's
  dependencies and releases, at the price of another package to maintain before that coupling
  has hurt anyone.
- **A terminal mode in the driver.** A terminal session has no turn boundary, no structured
  result and no message list, so it cannot meet §B.8.

## E. Later

### E.1 The tool bridge

It lets a harness agent take the app's `Tool` list, so an existing agent's tools are reused and
a tool knows its session. It waits for a later phase, after §C has run.

The bridge is an MCP server that vsigma runs on loopback inside the app's process and adds to
the harness's servers. It lists the app's tools, and a call to one runs `tool.run(arguments)`.

- **A call runs with the run's context.** `run_context()` works in a tool as under `react_node`;
  an exception becomes error text for the model; `NoRunContext` stops the turn. Duplicate names
  are refused as there, and a `return_direct` tool is refused: a harness goes on after a result.
- **The endpoint is closed to anyone else.** vsigma issues a secret per session, the harness
  process is given it with the address, and a call without it, or outside a turn in flight, is
  refused. The tools act for one user, and any local process can reach a loopback port.
- `mcp`, already required, brings the server. MCP is the tool channel coding harnesses share,
  so the bridge is written once. The vendor's SDK, with tools called in process, is the
  alternative: a dependency per vendor, and the bridge written again for each.

### E.2 On a consumer's need

- Persona and session memory placed in the system prompt when a session starts.
- A permission question answered by the caller, and through `resume` in the template.
- The driver's progress forwarded to vsigma's observers, and the harness's token and cost report.
- A harness as `distiller`, for an app with no API at all.
- A lost transcript rebuilt from the thread.
- A second vendor's driver.

## F. Verdicts

1. **The harness's own tools**: off unless the caller names them (proposed).
2. **Process**: kept alive for the session, and restarted and resumed by id when it is gone
   (proposed, on the probe's 1 to 2 s against 5 to 9 s a turn). The alternative is a process
   per turn: simpler, and slower by that much on every turn.
3. **First driver**: Claude Code.
4. **Turn control**: the handle of §B.1, with events and `stop` (proposed).
5. **Order**: the driver before the template, as in §C (proposed).
