# Proposal: Session Admission — Making Session Visibility a Rule, Not a Convention

**Status:** DRAFT 2026-09-10 — awaiting review.
**Prompted by:** a bumblebee dogfooding review of Phase 4.7. Asked whether Phase 4.7 unblocked
stable-session work, the reviewer first read the risk as a consumer-integration matter and
then escalated it correctly: *"Alice could leak knowledge to Bob even if she is not intended
to."* Reproduced — see §A.
**Relationship to Phase 4.7:** not a regression from it and not in its scope.
[memory-scope-promotion](./memory-scope-promotion.md) §B constrained where automatic
construction *writes*; this is about who may *read* what is already there. But 4.7's own
argument leads here: §A.4 objected to `AGENT` because "`visible_scopes` returns it for every
persona". One rung down, `visible_scopes` returns a session to whoever names it.
**Sibling:** [interaction-model](./interaction-model.md) §C fixes the session/bus boundary; a
bus delivering into `session.send` makes admission a runtime question rather than a
deployment convention — and an `open` session (§B.3) is close to what a broadcast channel
would want.

## A. Problem

### A.1 The documented rule was never implemented

`architecture_design.md` §5 said, until 2026-09-10:

> A session has participants; each turn records its author. `visible_scopes`/`recall` enforce
> the rule on the retrieve path: global/agent → all; **session → its participants**; persona →
> that persona only.

The persona half is real and tested. The session half describes enforcement that does not
exist: there is no participant list, and `grep` over `src/` finds no admission, membership or
authorization check of any kind. The sentence has been corrected; this proposal is how it
becomes true again.

Note a structural detail that shows the claim was never implementable as written:
`visible_scopes(state)` takes **no store**, so it cannot consult a durable participant list.
Only `recall(memory, state, kind)` holds a `MemoryStore`. Any enforcement must live where the
store is reachable.

### A.2 What actually happens

Reproduced on a clean store. Alice runs under `session_id="monitoring"`; Bob, a different
persona, supplies the same id:

```
Bob sees:
  session:monitoring/knowledge   The prod DB password rotates on Fridays.
  session:monitoring/context     episode 89f7edad… (task 89f7edad…)

Bob's rehydrated dialog: [{'role': 'user',      'content': 'remember: the prod password is hunter2'},
                          {'role': 'assistant', 'content': 'Noted: the prod password is hunter2.'}]
turn envelopes Bob can read: [('turn:0', 'alice')]
```

Two mechanisms, independently sufficient:

* `visible_scopes` (`scope.py:68-69`) appends `session(session_id)` on **presence of the id**,
  never on entitlement. Everything at that scope enters `recall`.
* `Session.__init__` (`sessions.py:60`) reads the stored dialog keyed on `session_id` alone,
  so constructing a handle for someone else's session rehydrates their conversation verbatim.

**The last line is the finding.** The turn envelope already records `user_id: 'alice'`
(`sessions.py`, the event-envelope write). Authorship is captured and durable. Nothing reads
it for this purpose. The data an admission check needs is already there.

### A.3 A `session_id` is a bearer capability

Today, holding the id *is* membership. That is a coherent design — a secret-URL model — and
for a single-tenant host it is adequate. It is not what the architecture claimed, and it
means the safety of Phase 4.7's session destination is conditional on the host minting
unguessable, per-principal ids. A library should not rely on a consumer's naming discipline
for a property it advertises.

### A.4 Phase 4.7 widened the blast radius; it did not create it

| what lands in `session:<id>` | since |
|---|---|
| `Session` dialog + turn envelopes | Phase 4.2 (`c304344`, 2026-07-04) |
| mechanical episode summary | pre-`phase-4.6` (verified against the tag) |
| **distilled knowledge** | **Phase 4.7** |

Before 4.7, distilled knowledge went to `persona:alice`, which Bob genuinely could not read —
narrower on *this* axis while far wider on the one 4.7 fixed (every later session of that
persona, and via the `AGENT` fallback every persona). A real trade, not a pure regression. The
shared channel already carried two things; 4.7 added a third.

## B. Design

### B.1 A durable session registry

A new operational namespace `sessions/index`, following `maintenance/state` and
`episodes/index` exactly — outside the scope ladder, `visible_scopes` never yields it, so a
registry entry can never reach a prompt:

```
session_id → { owner, visibility, participants, created_at }
```

`owner` is the `user_id` that first opened the session; `visibility` is `private` or `open`
(§B.3); `participants` is the admitted set, with the owner implicitly in it.

Four fields, one more than `episodes/index` was held to, and the extra one is argued for in
§B.3 rather than assumed. `visibility` is *policy* — who may read — while `participants` is
*state* — who has been admitted. Collapsing them into a magic `participants: ["*"]` would
save a field and cost the distinction, and would make the membership check carry two
meanings.

### B.2 Private by default; sharing is an act

The first principal to open a session owns it. A different `user_id` opening it is **rejected**
unless it has been admitted. This is the same posture as Phase 4.7's destination rule: the
narrow, safe behaviour is the default, and widening is an explicit recorded decision rather
than an inferred one.

Admission is a deliberate call — `session.admit(user_id)` by the owner, or a registry write by
the host — never a side effect of someone showing up with the right id.

Persona memory stays private regardless of session membership; that rule is already enforced
and unchanged.

### B.3 The owner declares the session open or private

`visibility` is set by the session's owner, defaults to `private`, and only the owner may
change it.

Without it, "open" is inexpressible for any session whose readers are not known in advance —
a shared runbook, a team channel, a public demo — because the only way to admit an unbounded
set is to enumerate it. Consumers would then either abandon the registry or invent a
convention (admit a sentinel principal), which is the outcome §B.6 warns about generally.

It also removes the one piece of *inferred* policy left in this design. §B.5 says a session
opened with `user_id=None` is readable by anyone; as an inference from a null owner that is
the same shape as the fallbacks Phase 4.7 removed. With this field the registry records
`visibility="open"` at creation and a reader learns the intent from the entry instead of
deducing it from an absence.

Rules:

* **Default `private`.** An owned session protects itself without anyone opting in.
* **Only the owner may change it.** A non-participant cannot reach a private session to open
  it, and an admitted participant is a guest, not a co-owner.
* **`open` does not widen the persona rule.** An open session shares session scope; it never
  exposes `persona:<id>`. That boundary is enforced and stays enforced.
* **No retroactive effect, in either direction.** Flipping `private → open` is a one-way
  release of everything already in the session — the registry records who did it and when.
  Flipping back stops future reads; it cannot unsend what was read (§C).
* **`participants` still means something on an open session** — a record of who has actually
  used it, useful for audit even when it gates nothing.

### B.4 Where the check goes

Three candidate points, and they are not alternatives so much as a depth question (§E.1):

| point | catches | notes |
|---|---|---|
| `Session.__init__` | dialog rehydration | the loudest and earliest failure; a handle for someone else's session should not construct |
| `Agent.run` / `Session.send` | execution under a session | the seam then writes episodic context under an entitled id |
| `recall` | the retrieve path | where the architecture claims enforcement lives, and the only one of the three with a `MemoryStore` in hand |

`recall` alone is insufficient — it would leave `Session.dialog` readable. `Session.__init__`
alone is insufficient — a caller can pass `session_id` straight to `Agent.run`. The minimum
honest set is probably `Session.__init__` plus `Agent.run`, with `recall` as the backstop that
makes the architecture sentence literally true.

### B.5 Anonymous and single-tenant use must stay easy

Many runs have no `user_id` at all, and most consumers are single-principal. A registry that
makes those cases harder will be worked around. Two consequences:

* A session opened with `user_id=None` is **unowned**, recorded as `visibility="open"`, and
  readable by anyone — the current behaviour, preserved deliberately because there is no
  principal to protect, but now *stated in the registry* rather than inferred from a null
  owner (§B.3).
* Enforcement should be inert for a host that never passes `user_id`. Nothing changes for
  them.

### B.6 Existing sessions have no registry entry

The same shape as Phase 4.7's unindexed episodes, with the opposite answer. There, unknown
ownership failed *closed* because the cost was a skipped maintenance batch. Here, failing
closed would reject every session created before this lands — every existing consumer,
immediately.

The options are trust-on-first-use (an unregistered session is claimed by the next principal
to open it), a grace mode, or an explicit backfill. **Recommendation: trust-on-first-use**,
because it converges on a registered state without a migration step, and record the choice
loudly — it means a pre-existing session is claimable exactly once by whoever gets there
first. §E.2 puts it to review.

## C. Non-goals

- **Roles, groups, or an org model.** Owner plus an admitted set. If richer authorization is
  ever needed it belongs to the host, not to a cognitive-architecture toolkit.
- **Guessability of `session_id`.** The registry makes the id insufficient; it does not make it
  secret. Hosts should still mint unguessable ids.
- **Cross-agent sharing.** One `Services.memory` is one trust domain here.
- **Retroactive protection.** Anything already written to a shared session stays readable by
  whoever the registry ends up admitting.
- **Persona privacy.** Already enforced and tested; untouched.

## D. Graduation criteria

- Alice creates knowledge and dialog in `session:s1`; Bob opening `s1` is **rejected** before
  any rehydration, recall or execution — the §A.2 repro inverted as a test.
- The owner re-opening `s1` across process restarts succeeds, and the dialog still rehydrates.
- After `admit("bob")`, Bob opens `s1`, sees its dialog and session knowledge, and **still
  cannot** see `persona:alice`.
- A session opened with `user_id=None` stays open to all — with `visibility="open"` recorded
  in the registry, not inferred — and a host that never passes `user_id` observes no behaviour
  change at all.
- Alice sets `s1` open; Bob reads its dialog and session knowledge **without** being admitted,
  and **still** cannot see `persona:alice`. Bob cannot set `s1` back to private, and cannot
  open a private session he is not admitted to.
- A `private → open` flip is recorded with its actor and timestamp; flipping back blocks
  subsequent reads.
- Passing an entitled `session_id` directly to `Agent.run` (bypassing `Session`) is subject to
  the same check.
- `sessions/index` is never returned by `visible_scopes` and never appears in `recall` output.

When validated, this graduates into `architecture_design.md` §5, restoring "session → its
participants" as a statement of fact — and the ✎ marker on it can go.

## E. Open questions

1. **How many of §B.3's three enforcement points for v0?** All three is defence in depth and
   three places to keep consistent; `Session.__init__` + `Agent.run` is the minimum that
   actually closes the hole, but leaves the architecture's `recall` claim aspirational.
2. **Trust-on-first-use, grace mode, or backfill** for pre-existing sessions (§B.5)? TOFU is
   recommended; it has a real edge — a pre-existing session is claimable once, by whoever
   opens it first.
3. **What does rejection look like?** An exception is loud and correct for a safety boundary,
   but `Session` construction currently cannot fail, and a raise changes an existing public
   surface. The alternative — an empty session with no rehydration — fails quietly, which is
   the property this whole line of work has been removing.
4. **Does `admit` belong on `Session`, on a CLI verb, or on both?** The maintenance precedent
   says operators get a CLI; the owner admitting a colleague mid-dialog argues for the API.
5. **Should `open` distinguish read from write?** A shared runbook others may read but not
   append to is a plausible want, and `open` as specified admits both. A read/write split is
   a second axis on top of the two in §B.3 — likely over-built for v0, but it is the natural
   next request, and an `open` that silently means read-write is worth naming now.
6. **Interaction with the message bus** ([interaction-model](./interaction-model.md) §C): a bus
   delivers into `session.send` on behalf of an author. Is the author the principal checked
   here, or does the transport carry its own identity?
