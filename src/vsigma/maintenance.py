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

from .bundle import ingest_bundle, write_bundle
from .consolidation import deep_consolidate, episode_trajectory
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


def unconsolidated_episodes(store: SQLiteStore) -> list[str]:
    """Episodes with retained trajectory but no ``deep:`` marker yet, in
    first-write (oldest-first) order."""
    processed = {
        key.removeprefix("deep:")
        for key, _ in store.query(MAINTENANCE_NS)
        if key.startswith("deep:")
    }
    episodes = []
    for namespace in store.namespaces():
        if namespace.startswith(_EPISODE_PREFIX) and namespace.endswith(_CONTEXT_SUFFIX):
            run_id = namespace[len(_EPISODE_PREFIX) : -len(_CONTEXT_SUFFIX)]
            if run_id not in processed:
                episodes.append(run_id)
    return episodes


def run_maintenance(
    store: SQLiteStore,
    llm: LLM,
    *,
    scope: Scope,
    constructor: str,
    out_dir: str | Path,
    limit: int = 20,
    dry_run: bool = False,
) -> MaintenanceReport:
    """One maintenance invocation: batch-distill unprocessed episodes, commit
    via the bundle gate, then advance the markers. Raises ValueError when the
    constructor breaks the output shape or the bundle fails validation — in
    which case no marker advances and the next run retries the same batch."""
    report = MaintenanceReport(episodes=unconsolidated_episodes(store)[:limit])
    if not report.episodes:
        return report

    sources = [episode_trajectory(store, run_id) for run_id in report.episodes]
    records = deep_consolidate(sources, llm, scope=scope, constructor=constructor)
    report.records = len(records)

    if records:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bundle = write_bundle(
            Path(out_dir) / f"deep-{stamp}", records, constructor=constructor
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
