"""The memory pass — consolidating ended episodes into durable knowledge.

Design: docs/proposals/maintenance.md. The pass finds ended episodes the deep
round hasn't consolidated yet (or whose size changed since), distills them with
a capable model, commits through the bundle gate, and records `{"at", "constructor",
"bundle", "size"}` markers in `maintenance/state`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .bundle import ingest_bundle, write_bundle
from .consolidation import deep_consolidate, episode_trajectory
from .episodes import (
    FINAL_EPISODE_STATUSES,
    _owned_by,
    episode_owner,
    episode_owners,
    list_episode_statuses,
)
from .llm import LLM
from .memory import MaintainableStore
from .records import MemoryRecord
from .scope import Scope
from .threads import SessionBusy, thread_lock

#: Operational bookkeeping namespace.
MAINTENANCE_NS = "maintenance/state"

_EPISODE_PREFIX = "episode:"
_CONTEXT_SUFFIX = "/context"

_REGISTRY_LOCK = threading.Lock()
_ACTIVE_STORES: set[int] = set()


@contextmanager
def store_guard(store: Any) -> Iterator[bool]:
    """Acquire an in-process single-pass guard on `store`. Yields False if busy."""
    store_id = id(store)
    with _REGISTRY_LOCK:
        if store_id in _ACTIVE_STORES:
            acquired = False
        else:
            _ACTIVE_STORES.add(store_id)
            acquired = True
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        with _REGISTRY_LOCK:
            _ACTIVE_STORES.discard(store_id)


@dataclass
class MemoryPassReport:
    """What one memory pass invocation did."""

    episodes: list[str] = field(default_factory=list)
    records: int = 0
    bundles: list[str] = field(default_factory=list)
    ingested: bool = False
    unowned: int = 0
    busy: bool = False


def unconsolidated_episodes(
    store: MaintainableStore, *, sources: list[Scope] | None
) -> list[str]:
    """Episodes with retained trajectory but no matching ``deep:`` marker yet.

    Only ended episodes (`done`, `failed`, `abandoned`) are eligible. Episodes
    without a status ran outside `Agent` and are matched only by `sources=None`.
    Tombstoned episodes are skipped. An episode is consolidated only while its
    marker's size matches the current size in `store.namespace_sizes()`.
    """
    tombstones = {key for key, _ in store.query("episodes/tombstone")}
    markers = {
        key.removeprefix("deep:"): value
        for key, value in store.query(MAINTENANCE_NS)
        if key.startswith("deep:")
    }
    sizes = store.namespace_sizes()
    statuses = list_episode_statuses(store)
    owners = episode_owners(store) if sources is not None else {}
    episodes = []

    for namespace in store.namespaces():
        if namespace.startswith(_EPISODE_PREFIX) and namespace.endswith(_CONTEXT_SUFFIX):
            run_id = namespace[len(_EPISODE_PREFIX) : -len(_CONTEXT_SUFFIX)]
            if run_id in tombstones:
                continue

            status = statuses.get(run_id)
            if status is None:
                if sources is not None:
                    continue
            elif status not in FINAL_EPISODE_STATUSES:
                continue

            marker = markers.get(run_id)
            if isinstance(marker, dict):
                rec_size = marker.get("size")
                curr_size = sizes.get(namespace, 0)
                if rec_size is not None and rec_size == curr_size:
                    continue

            if sources is not None:
                owner = owners.get(run_id)
                if owner is None or not _owned_by(owner, sources):
                    continue

            episodes.append(run_id)

    return episodes


def _commit_and_mark(
    store: MaintainableStore,
    episodes: list[str],
    records: list[MemoryRecord],
    trajectories: list[tuple[str, str]],
    recorded_sizes: dict[str, int],
    *,
    constructor: str,
    out_dir: str | Path,
) -> tuple[str | None, bool]:
    bundle_str: str | None = None
    ingested = False

    if records:
        stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}"
        bundle = write_bundle(
            Path(out_dir) / f"deep-{stamp}",
            records,
            constructor=constructor,
            sources=trajectories,
        )
        bundle_str = str(bundle)
        ingest_bundle(bundle, store)
        ingested = True

    at = datetime.now().isoformat(timespec="seconds")
    for run_id in episodes:
        store.write(
            MAINTENANCE_NS,
            f"deep:{run_id}",
            {
                "at": at,
                "constructor": constructor,
                "bundle": bundle_str,
                "size": recorded_sizes.get(run_id, 0),
            },
        )

    return bundle_str, ingested


def _consolidate_batch(
    store: MaintainableStore,
    llm: LLM,
    *,
    episodes: list[str],
    scope: Scope,
    constructor: str,
    out_dir: str | Path,
    dry_run: bool = False,
    checkpointer: Any = None,
) -> tuple[list[str], int, str | None, bool]:
    """Consolidate an explicit list of episodes for one scope without acquiring store_guard.

    Returns:
        (consolidated_episodes, records_count, bundle_path_str_or_none, ingested_bool)
    """
    if not episodes:
        return [], 0, None, False

    sizes = store.namespace_sizes()
    recorded_sizes = {
        run_id: sizes.get(f"episode:{run_id}/context", 0) for run_id in episodes
    }

    trajectories = [episode_trajectory(store, run_id) for run_id in episodes]
    records = deep_consolidate(trajectories, llm, scope=scope, constructor=constructor)

    if dry_run:
        if records:
            stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}"
            bundle = write_bundle(
                Path(out_dir) / f"deep-{stamp}",
                records,
                constructor=constructor,
                sources=trajectories,
            )
            return list(episodes), len(records), str(bundle), False
        return list(episodes), 0, None, False

    if scope.level == "session" and checkpointer is not None:
        try:
            with thread_lock(checkpointer, scope.id):
                for run_id in episodes:
                    owner = episode_owner(store, run_id)
                    if owner is None or owner.session_id != scope.id:
                        return [], 0, None, False
                bundle_str, ingested = _commit_and_mark(
                    store,
                    episodes,
                    records,
                    trajectories,
                    recorded_sizes,
                    constructor=constructor,
                    out_dir=out_dir,
                )
                return list(episodes), len(records), bundle_str, ingested
        except SessionBusy:
            return [], 0, None, False

    bundle_str, ingested = _commit_and_mark(
        store,
        episodes,
        records,
        trajectories,
        recorded_sizes,
        constructor=constructor,
        out_dir=out_dir,
    )
    return list(episodes), len(records), bundle_str, ingested


class MemoryPass:
    """The memory pass as an explicit, callable job."""

    def __init__(
        self,
        store: MaintainableStore,
        llm: LLM,
        *,
        sources: list[Scope] | None,
        scope: Scope,
        constructor: str,
        out_dir: str | Path,
        limit: int = 20,
    ) -> None:
        self.store = store
        self.llm = llm
        self.sources = sources
        self.scope = scope
        self.constructor = constructor
        self.out_dir = out_dir
        self.limit = limit

    def run(self, *, dry_run: bool = False) -> MemoryPassReport:
        """Run the memory pass, acquiring store_guard."""
        report = MemoryPassReport()
        with store_guard(self.store) as acquired:
            if not acquired:
                report.busy = True
                return report

            candidates = unconsolidated_episodes(self.store, sources=self.sources)
            batch = candidates[: self.limit]
            if not batch:
                return report

            consolidated, rec_count, bundle_path, ingested = _consolidate_batch(
                self.store,
                self.llm,
                episodes=batch,
                scope=self.scope,
                constructor=self.constructor,
                out_dir=self.out_dir,
                dry_run=dry_run,
            )
            report.episodes = consolidated
            report.records = rec_count
            if bundle_path:
                report.bundles.append(bundle_path)
            report.ingested = ingested
            return report
