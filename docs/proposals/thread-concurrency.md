# Proposal: Thread Concurrency — One Operation per Thread at a Time

**Status:** **GRADUATED** (Phase 4.11 completed 2026-10-29). Every verdict as recommended (§F);
§B's lock was amended at review. A bug fix for released behaviour, released alone as `v0.2.1`
with the `mcp<2` cap. Drafted
2026-10-27; revised 2026-10-29: a late answer can land on a newer question, which no lock can see,
so a resume can name the pause it answers (§A.5, verdict 5). Found while checking this proposal
against the code.
**Prompted by:** an audit of thread-expiry (2026-10-27) that reproduced expiry deleting a thread
during a running turn. Tracing the cause found the same check-then-act pattern in released code
(§A).
**Used by:** [thread-expiry](./thread-expiry.md) and [scope-lifespans](./scope-lifespans.md),
which take this lock before deleting.

## A. Problem

Since 4.8 a session is a LangGraph thread, and every operation on it checks the thread's state,
then acts on it. Nothing stops a second operation on the same thread in between. Under
`durability="exit"`, the templates' default, a running turn writes nothing until it ends, so its
first checkpoint, the only sign of activity in storage, comes too late to warn anyone.

1. **Two turns on one session** (`send` and `send`, `send` and `resume`, or two `resume`s at
   once; a `resume` after the other has finished is problem 5). Both pass the checks in
   `Agent.run` (`SessionPaused`) or `Run.resume` (active run, pending interrupt), both start
   from the same checkpoint, and the later write becomes the thread's latest: the other turn's
   messages are gone, and `keep_latest` deletes them. A `send` can also pass the pause check
   just before another turn pauses, and LangGraph then silently drops that interrupt, which is
   what `SessionPaused` exists to prevent.
2. **`Session.end()` or `Run.end()` during a turn.** They check for a pause, delete the thread,
   and the turn rewrites it when it ends (reproduced 2026-10-27; §G.1): the end is silently
   undone.
3. **`keep_latest` beside a concurrent turn.** Its pause check runs outside its transaction, so a
   pause another turn has just written inside a subgraph can be deleted as a finished namespace.
4. **A web app makes 1 reachable.** Its "busy" guard is per connection. With one app-wide
   checkpointer, a reconnect while a turn still runs in the old socket's threadpool can start a
   second turn on the same session. With a `MemorySaver` per session, as on `v0.1.0`, it could
   not.
5. **A late answer lands on a newer question.** A resume answers whichever pause is pending when
   it arrives (`Command(resume=value)`, `run.py:320`): nothing names the question it answers,
   and `Session.interrupt` and `SessionPaused` expose only the question's value. A run can pause
   more than once: the escalation node fires again if the run still fails, and a model that may
   ask for clarification can ask again after an answer. An answer to Q1 that arrives after the run has
   moved on to Q2 is taken as the answer to Q2, and nothing raises (§G.3).
   - **No lock can see it.** The check is a person reading Q1; the act is a call made minutes
     later. The two resumes never overlap, and every check vsigma makes still passes: the
     session is paused, the run is still the thread's active one (pausing again keeps its
     `run_id`), and a pause is pending. vsigma refuses a resume for a run that is no longer
     active (`test_abandoned_run_refuses_resume`), but not for a pause that is no longer pending.
   - **How it arises:** a double click or a client retry (a web app's "busy" error invites
     one), two tabs or devices on one session, an answer re-sent after a
     reconnect, two people who can answer one escalation.
   - **What it costs:** the agent continues on an answer nobody gave to that question, silently.
     Where a pause is an approval, a stale "yes" approves an action nobody saw.
   - **Older than 4.8:** in `v0.1.0`, a `send` to a paused session resumed whatever was pending.

## B. Design

**Whatever checks and then changes a thread holds that thread's lock for the whole operation.**

- **The lock** is a busy mark per (checkpointer, thread id): for each checkpointer, held weakly,
  a set of busy thread ids, changed under one mutex and emptied as operations finish. Every
  `Agent` over one checkpointer shares it, and it holds only threads in use; an app may build
  one `Agent` per session over one checkpointer. (Amended at review: the first build kept a
  `threading.Lock` for every thread ever run, so the registry grew with every one-shot run.)
- **Who holds it:**
  - `Agent.run`, from the `SessionPaused` check, through registering the episode and recording
    it `running`, to the run-end hook and `keep_latest`. Expiry's recovery sweep relies on
    this: a `running` episode whose lock is free belongs to no live run (scope-lifespans §C.1);
  - `Run.resume`, from its checks through `keep_latest`;
  - `Session.end()` and `Run.end()`;
  - `keep_latest` when called directly; inside a run it uses the run's lock;
  - thread expiry, its sweep of unfinished episodes, and the completion of forget records
    (scope-lifespans §C.1, §C.2). Once forget records exist (scope-lifespans §E step 3), every
    operation on a session thread completes a pending one for it first.
- **Busy means fail fast.** A second operation on a busy thread raises `SessionBusy(thread_id)`
  rather than waiting: a turn can take minutes, and the caller, such as a web app with its own
  "busy" reply, knows better than vsigma whether to retry. Thread expiry skips a busy thread.
- **Boundary:** one process per checkpointer. The lock lives in the process; several processes
  on one database need a lease in the database itself, which is a non-goal here (§D) and is
  documented as the supported boundary.
- **No change to what a turn computes.** The lock only serializes; LangGraph's own writes are
  already ordered within a turn (`invoke` waits for pending checkpoint writes before returning,
  §G.2).

**A resume can name the pause it answers, and is refused if that pause is no longer pending**
(problem 5). The lock makes the comparison and the resume one step; the id supplies what only
the caller knows, which question it saw.

- **The id** is LangGraph's `Interrupt.id`, exposed as `Session.interrupt_id`,
  `Run.interrupt_id` and `SessionPaused.interrupt_id`, beside the existing `interrupt` value.
- **`resume(value, *, interrupt_id=None)`** on `Session` and `Run`. Holding the thread's lock,
  `Run.resume` compares `interrupt_id` with the pending pause's id. If another pause is pending,
  or none, it raises `PauseChanged`, carrying the pause now pending, if any, so the app can show
  the question that is actually open. Like `SessionPaused`, it is a `RuntimeError`.
- **Without `interrupt_id`**, a resume behaves as today, so nothing breaks. The docs tell every
  app that shows a pause to a person to pass it.
- **The id travels with the answer, from what the person saw.** An app that looks the id up
  when the answer arrives gets the newer pause's id and reopens the gap. For a web app, the
  message carrying an answer names the question it answers.
- **One id per execution of the paused node.** LangGraph derives it from the task's namespace
  (§G.5), so a node that pauses again later gets a new id, and two `interrupt()` calls in one
  execution share one. vsigma's escalation node, and a clarification node built like it, call
  `interrupt()` once per execution, so each of their pauses has its own id.

## C. Build order

1. The registry and `SessionBusy`, exported from `vsigma`.
2. `Agent.run` and `Run.resume` take the lock; the `SessionPaused` check moves inside it.
3. `Session.end()`, `Run.end()` and a direct `keep_latest` take it.
4. The pause id: `interrupt_id` on `Session`, `Run` and `SessionPaused`; the `interrupt_id`
   parameter on both `resume`s, compared under the lock; `PauseChanged`, exported from `vsigma`.
5. Tests. Those for the lock hold a turn open with a blocking fake LLM:
   - a second `send` raises `SessionBusy`, and the first turn's messages survive;
   - two `resume`s at once: the second raises `SessionBusy`;
   - `end()` during a turn raises, and after the turn `end()` deletes the thread;
   - a subgraph pause written during `keep_latest` survives;
   - another thread on the same checkpointer is not blocked;
   - two `Agent`s over one checkpointer share the lock.

   Those for the pause id replay §G.3:
   - B's resume with Q1's id raises `PauseChanged` carrying Q2; Q2 stays pending, and a resume
     with Q2's id continues;
   - a resume with an id after the run has finished raises `PauseChanged` carrying no pause;
   - without `interrupt_id`, today's tests pass unchanged.
6. Docs: `upgrading.md` (`SessionBusy`, the one-process boundary, and `interrupt_id`: an app
   that shows a pause to a person passes it), and a consumer note that migrations target
   `v0.2.1` and that a web app's answer message names the question it answers.

## D. Non-goals

- Several processes on one checkpointer: a lease in the database, later.
- Queueing a second turn behind the first. Callers that want it can catch `SessionBusy` and
  retry.
- Async APIs: vsigma is synchronous.
- An id for `abandon=True`. It discards a pause rather than answering it, and a web app that
  drops a pending question on reopen means to drop whatever is pending. Add it when a consumer needs to discard only the
  pause it saw.

## E. Alternatives considered

- **Block until the thread is free.** A turn can take minutes, and a reconnected client would
  hang behind a turn it cannot see. Fail fast, and let the caller decide.
- **Detect activity from storage** (a start marker). Under `"exit"` nothing is written at the
  start, and writing a marker per turn adds a write to every turn for what an in-process lock
  does for free.
- **Leave it to the app.** A per-connection guard shows how easily an app guards the wrong
  unit; the thread is vsigma's concept, and so is its lock.
- **LangGraph's own resume by id**, `Command(resume={id: value})`. With an id that is no longer
  pending it raises nothing, keeps the run paused at the newer question, and drops the answer
  (§G.4): safer than misapplying it, but the caller is never told.
- **A pause token of vsigma's own**, such as a counter in state. LangGraph's id already tells
  apart every pause the templates make; a token would be a second identity to keep in step.
- **Require `interrupt_id`.** Safest by default, but it breaks every caller that resumes today.
  A candidate for a later breaking release, once consumers pass it.

## F. Verdicts (decided 2026-10-29)

1. **The rule** (§B): one operation per thread, held for the whole check-then-act —
   **decided: yes.** Rejected: leaving it to the app, and detecting activity from storage (§E).
2. **Fail fast with `SessionBusy`** — **decided: yes.** Rejected: blocking until the thread is
   free (§E).
3. **One process per checkpointer** as the documented boundary — **decided: yes.** Both
   consumers fit it: the web app runs one server process, and bumblebee's checkpointer is in
   memory, which no second process can share.
   Deferred: a lease in the database, until a consumer scales out to several processes (§D).
4. **The release** — **decided: `v0.2.1` is this bug fix alone**, with the `mcp<2` cap; thread
   expiry moves to a later release, after its own Decision Check Point (`v0.2.3`, since
   `v0.2.2` became the first public release). Consumers that ran two turns on one session at
   once now get `SessionBusy` instead of lost messages. Rejected: holding the
   fix until thread expiry, which awaits 17 verdicts (its 9, scope-lifespans' 8) and the
   auditor's sign-off.
5. **A resume can name the pause it answers** (§A.5, §B) — **decided: yes, optional.**
   `interrupt_id` exposed on `Session`, `Run` and `SessionPaused`, accepted by `resume`,
   compared under the lock, and `PauseChanged` on a mismatch; without it, a resume behaves as
   today. Rejected: requiring it, which breaks every caller that resumes today (a candidate for
   a later breaking release, §E), and leaving it out.

## G. Evidence (LangGraph 1.2.6, langgraph-checkpoint-sqlite 3.1.1; 2026-10-27, 2026-10-29)

1. A session on `SqliteSaver` with one finished turn; a second turn started in another thread and
   held in its LLM call. Expiry's proposed guard (compare the latest checkpoint with the one
   scanned, under `cp.lock`, `BEGIN IMMEDIATE`) saw no change and deleted the thread; `dialog`
   read empty. When the turn ended, the thread was back with all four messages.
2. LangGraph's background executor waits for every pending task, checkpoint writes included,
   when the loop exits (`langgraph/pregel/_executor.py`, `__exit__`), so `keep_latest` after
   `invoke` runs after the turn's last write.
3. 2026-10-29: a guarded ReAct agent (`limit=1`) whose `FakeLLM` fails twice, then answers. Two
   clients saw Q1 (interrupt id `0aae644e…`). A resumed it, and the run paused again at Q2
   (`f728bf36…`). B then resumed with its answer to Q1: accepted, the run finished `done` with
   both answers in its dialog, and nothing raised.
4. 2026-10-29, the same setup: resuming with `Command(resume={<Q1's id>: answer})` while Q2 was
   pending raised nothing; the run stayed paused at Q2, and the answer appeared nowhere in its
   state.
5. `interrupt()` raises `Interrupt.from_ns(value, ns=<the task's checkpoint namespace>)`, whose
   id is a hash of that namespace (`langgraph/types.py`). A later execution of the same node is
   a new task, with a new id (§G.3).
