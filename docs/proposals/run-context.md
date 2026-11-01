# Proposal: Run Context — One Agent, Many Sessions

**Status:** **ACCEPTED** 2026-11-01 as Phase 4.15, for `v0.3.1`. Waits: all of it, to be built.
**Unblocks:** `agent.maintain()` for an app whose sessions each hold their own objects
([maintenance](./maintenance.md) §B.1).

## A. Problem

1. **A tool cannot tell whose work it is doing.** It receives only what the model put in the call:
   `tool.run(call.arguments)` (`react.py:75`). The session is in the state, but the tool never sees
   it.
2. **So an app builds one `Agent` per session.** An app whose tools need a user's credential, or a
   per-session object, closes over them when it builds the tools. It cannot put them in the
   arguments: the model must never see a credential.
3. **That shape cannot use `maintain`.** It requires that its agent be the only live `Agent` holding
   the store. Per-session agents sharing a persistent store break that rule whenever a second
   session is open.

An app's service should hold one long-lived `Agent` with one persistent checkpointer and store.
What varies per session must reach the running code some other way than the model.

## B. Design

### B.1 The shape

The app passes one object per session, and any code running inside the graph reads it back:

```python
@dataclass
class SessionServices:
    credential: str
    trace: Trace

def query(args):
    services = run_context()
    rows = backend.query(args["order"], credential=services.credential)
    services.trace.record(rows)
    return rows

agent = make_react_agent(llm, [tool(query)], checkpointer=saver, interceptor=Interceptor(memory=store))

session = agent.session(session_id, user_id=user_id, run_context=SessionServices(credential, trace))
session.send("show me order 17")
```

- **`agent.run(task, run_context=...)` and `agent.session(id, run_context=...)`** are the only new
  arguments. A `Session` or `Run` keeps the object it was given and uses it for `send` and `resume`.
- **`run_context()`** returns it, in tools, nodes, routers, validators and `ContextManager`s.
- **The object is the app's.** It is any Python object, and vsigma never inspects it. Per-session
  state stays in the app and is lost on restart, as it is today.
- **A renewed credential** takes a new handle: `agent.session(id, run_context=renewed)`. The object
  is not stored, so a resume always runs with the object its handle holds.

vsigma adds no mechanism of its own: this is LangGraph's `context` argument (`langgraph>=1.2.4`,
already required), passed by `_stream_run` and read through `get_runtime()`. A throwaway prototype
changes 58 lines, and all 352 tests pass on it. On it, 80 sessions and 400 turns ran on one `Agent`
from 8 threads, with `SqliteSaver`, `PersistentStore` and `maintain()` running throughout: no session
saw another's object, and no secret reached either file. The object also reached routers,
`ContextManager`s and the step after a resume, and LangGraph carries it into subgraphs. Writing it to
state or memory is the one way it reaches the disk, and that part is the app's.

### B.2 Failing closed

Tenant isolation now depends on this lookup, so a missing object must stop the run, never fall back
or reach the model.

- **`run_context()` raises `NoRunContext`** when the run has no object, outside a run, and on a thread
  the code started itself, which does not inherit the run.
- **`react_node` re-raises it.** Every other tool exception becomes text for the model
  (`react.py:76`). This one would tell the model about a programming error and let the turn go on.
- **A session that ran with an object cannot continue without one.** vsigma records `has_context` in
  the checkpoint metadata, never the object, and `send` and `resume` raise before they start if it
  is set and they hold none. Without this check a resume consumes the pause and then fails at the
  first read: the question is gone, and the turn cannot be resumed or finished. Only a session's first
  turn is not checked, and it fails at its first read.

### B.3 What one agent changes for the app

- **A turn can meet `SessionBusy` from `maintain`.** Old-checkpoint pruning holds each thread's lock
  briefly, and a turn arriving then raises `SessionBusy`, as for any concurrent operation
  (`threads.py:62`). In the run above, with `maintain` looping, this happened 32 times over 400 turns,
  and every turn went through on retry. This is already true for any app calling `maintain` with
  sessions open: the app retries (§F.3).
- **`TrajectoryLog` grows without bound.** Its one list now lives as long as the service
  (`observability.py:39`). A long-lived agent passes its own observer or none, as its docstring
  already says.
- **`run_end` hooks run outside the graph** (`run.py`, `Run._record`), so they cannot read the object.
- **One `LLM` and one set of tools per agent.** Per-session prompt text goes through a
  `ContextManager`, which can read the object.

## C. Build order

One phase, compatible, released as `v0.3.1`:

1. `run_context=` on `Agent.run` and `Agent.session`, passed through `Session` and `Run` to
   `_stream_run`; `run_context()` and `NoRunContext`, exported.
2. `react_node` re-raises; `has_context` in the metadata, checked by `send` and `resume`.
3. Docs: `getting_started.md` on the one-agent shape, and `upgrading.md`.

**Graduation:** a trial migration of a real multi-user consumer in a scratch copy reaches one
`Agent` with one persistent checkpointer and store, its offline suite passes, and `maintain()` runs
with sessions open. Tests cover §B.2 case by case, including that a refused resume leaves the session
paused.

## D. Alternatives

- **One `Agent` per session, with `maintain` on a separate one:** the check forbids it, and removing
  the check brings back the cross-agent hazards it exists for.
- **Pass the session to the tool as a new argument:** changes `ToolFunc` for every tool, and reaches
  only tools, not routers or `ContextManager`s.
- **Put the object in graph state:** state is checkpointed, so a credential would reach the disk.
- **Declare the type with `context_schema`, checked on entry:** also catches a first turn, but adds an
  argument to all four templates for a case that already fails closed.
- **A `contextvars` variable of vsigma's own:** repeats what LangGraph already carries.

## E. Non-goals

- **Several processes.** Thread locks and the one-agent check are per process, and neither changes.
- **Moving the one-agent check to `Agent` construction.** Decide once a consumer has made the switch.
- **Session admission.** `session_id` stays a bearer capability
  ([session-admission](./session-admission.md)). With one agent, an app must keep deriving the id from
  the authenticated user, never take it raw from a client.
- **A per-session `LLM` or tool list.**

## F. Verdicts

1. **Build this as one phase, released as `v0.3.1`.** **decided 2026-11-01: yes.** Compatible, so it
   does not start `v0.4.0` or its open-source groundwork.
2. **The name `run_context`.** **decided 2026-11-01: yes.** LangGraph's plain `context` would clash
   with `Interceptor(context=...)`, the context manager, in the same API.
3. **`SessionBusy` from `maintain`: the app retries, or vsigma waits briefly?** **decided 2026-11-01:
   the app retries for now.** It already has to handle `SessionBusy`, and a wait needs a timeout and a
   way to tell maintenance from a running turn. Revisit if a real deployment sees it often.
