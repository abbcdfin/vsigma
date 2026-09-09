"""The maintenance pass — the deep round as a runnable operation.

Design: docs/proposals/maintenance-pass.md. The pass finds episodes the deep
round hasn't processed yet (marker records in the operational
``maintenance/state`` namespace — outside the scope ladder, so never visible
to `recall`), distills them in one batch with a capable model, and commits
through the bundle gate like any constructor. The invariant: **markers advance
only after a successful ingest** — any failure leaves the whole batch to be
retried, and re-ingest is idempotent (content-addressed ids).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .bundle import ingest_bundle, write_bundle
from .consolidation import deep_consolidate, episode_trajectory
from .episodes import EpisodeOwner, episode_owners
from .llm import LLM
from .scope import AGENT, GLOBAL, Scope, persona, session
from .stores import SQLiteStore

#: Operational bookkeeping namespace. Not a scope-ladder namespace:
#: `visible_scopes()` never yields it, so markers cannot reach a prompt.
MAINTENANCE_NS = "maintenance/state"

_EPISODE_PREFIX = "episode:"
_CONTEXT_SUFFIX = "/context"


@dataclass
class MaintenanceReport:
    """What one `run_maintenance` invocation did."""

    episodes: list[str] = field(default_factory=list)
    records: int = 0
    bundle: str | None = None
    ingested: bool = False


def parse_scope(text: str) -> Scope:
    """Parse the CLI scope argument: ``agent``, ``global``, ``persona:<id>``
    or ``session:<id>``."""
    if text == "agent":
        return AGENT
    if text == "global":
        return GLOBAL
    level, sep, scope_id = text.partition(":")
    factories = {"persona": persona, "session": session}
    if sep and scope_id and level in factories:
        return factories[level](scope_id)
    raise ValueError(
        f"invalid scope {text!r}: use agent, global, persona:<id> or session:<id>"
    )


def parse_source(text: str) -> Scope:
    """Parse a ``--source`` argument. Only the two ownership rungs are sources:
    ``persona:<id>`` and ``session:<id>``."""
    level, sep, scope_id = text.partition(":")
    factories = {"persona": persona, "session": session}
    if sep and scope_id and level in factories:
        return factories[level](scope_id)
    raise ValueError(
        f"invalid source {text!r}: use persona:<id> or session:<id>. "
        "(agent and global are destinations, not owners.)"
    )


def _owned_by(owner: EpisodeOwner, sources: list[Scope]) -> bool:
    return any(
        (scope.level == "session" and owner.session_id == scope.id)
        or (scope.level == "persona" and owner.user_id == scope.id)
        for scope in sources
    )


def unconsolidated_episodes(
    store: SQLiteStore, *, sources: list[Scope] | None
) -> list[str]:
    """Episodes with retained trajectory but no ``deep:`` marker yet, in
    first-write (oldest-first) order.

    `sources` is the **ownership boundary** — which tenants' episodes this pass
    may read — and is deliberately not optional. Pass `None` for store-wide
    (the CLI spells that ``--all-sources``); pass a list of `persona:`/`session:`
    scopes to restrict. Absence of a stated boundary is not evidence that no
    boundary exists, so there is no inferred single-tenant default.

    Do not confuse it with `run_maintenance`'s `scope`, which is where distilled
    knowledge *lands*. One says what may be read, the other what is written.

    An episode with no `episodes/index` entry has **unknown** ownership — it ran
    before the index existed, or outside `Agent.run` — and is matched only by
    `None`, never by a specific source. Unknown ownership fails closed.
    """
    processed = {
        key.removeprefix("deep:")
        for key, _ in store.query(MAINTENANCE_NS)
        if key.startswith("deep:")
    }
    owners = episode_owners(store) if sources is not None else {}
    episodes = []
    for namespace in store.namespaces():
        if namespace.startswith(_EPISODE_PREFIX) and namespace.endswith(_CONTEXT_SUFFIX):
            run_id = namespace[len(_EPISODE_PREFIX) : -len(_CONTEXT_SUFFIX)]
            if run_id in processed:
                continue
            if sources is not None:
                owner = owners.get(run_id)
                if owner is None or not _owned_by(owner, sources):
                    continue
            episodes.append(run_id)
    return episodes


def run_maintenance(
    store: SQLiteStore,
    llm: LLM,
    *,
    sources: list[Scope] | None,
    scope: Scope,
    constructor: str,
    out_dir: str | Path,
    limit: int = 20,
    dry_run: bool = False,
) -> MaintenanceReport:
    """One maintenance invocation: batch-distill unprocessed episodes, commit
    via the bundle gate, then advance the markers. Raises ValueError when the
    constructor breaks the output shape or the bundle fails validation — in
    which case no marker advances and the next run retries the same batch.

    `sources` bounds what may be *read* (see `unconsolidated_episodes`); `scope`
    is where the distilled records *land*. Both are required: one pass over
    every tenant's episodes, distilled into one tenant's scope, is the leak this
    separation exists to prevent."""
    report = MaintenanceReport(
        episodes=unconsolidated_episodes(store, sources=sources)[:limit]
    )
    if not report.episodes:
        return report

    # Note the two senses of "source": `sources` above is the ownership boundary
    # (which episodes may be read); these are the distill inputs read from them.
    trajectories = [episode_trajectory(store, run_id) for run_id in report.episodes]
    records = deep_consolidate(trajectories, llm, scope=scope, constructor=constructor)
    report.records = len(records)

    if records:
        # The timestamp is for humans; the uuid is what makes the path unique.
        # Second precision alone collides between two passes in the same second
        # (write_bundle's mkdir is exist_ok and overwrites both files), and a
        # collision is silent *and* wrong: each batch's deep:<run_id> markers
        # keep pointing at that path, so one batch's markers end up citing the
        # other's manifest as the evidence of what it read.
        stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}"
        # Declare what was read. `write_bundle` hashes each trajectory into the
        # manifest, so a bundle says which episodes it came from and a reviewer
        # can tell whether it read what it was allowed to.
        bundle = write_bundle(
            Path(out_dir) / f"deep-{stamp}",
            records,
            constructor=constructor,
            sources=trajectories,
        )
        report.bundle = str(bundle)
        if dry_run:
            return report
        ingest_bundle(bundle, store)
        report.ingested = True
    elif dry_run:
        return report
    # A zero-record batch was still processed — mark it, or it is retried forever.

    at = datetime.now().isoformat(timespec="seconds")
    for run_id in report.episodes:
        store.write(
            MAINTENANCE_NS,
            f"deep:{run_id}",
            {"at": at, "constructor": constructor, "bundle": report.bundle},
        )
    return report
