"""The maintenance coordinator and scope parsers.

Design: docs/proposals/maintenance.md. Holds `MAINTENANCE_NS`, CLI scope helpers,
`MaintenanceReport`, and coordinates `agent.maintain()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver

from .episodes import episode_owners
from .expiry import ExpiryPolicy, ExpiryReport, ThreadExpiry
from .memory import MaintainableStore
from .memory_pass import (
    MAINTENANCE_NS,
    MemoryPass,
    MemoryPassReport,
    _consolidate_batch,
    store_guard,
    unconsolidated_episodes,
)
from .retention import (
    _DeltaChannel,
    _keep_latest_in_memory,
    _keep_latest_sqlite,
)
from .retention_episodes import (
    DEFAULT_RETENTION_POLICY,
    EpisodeRetention,
    RetentionPolicy,
    RetentionReport,
)
from .scope import AGENT, GLOBAL, Scope, persona, session
from .threads import SessionBusy, thread_lock

if TYPE_CHECKING:
    from .llm import LLM

__all__ = [
    "MAINTENANCE_NS",
    "MaintenanceReport",
    "MemoryPass",
    "MemoryPassReport",
    "maintain",
    "parse_scope",
    "parse_source",
    "unconsolidated_episodes",
]


@dataclass
class MaintenanceReport:
    """What one `agent.maintain` invocation did."""

    expiry: ExpiryReport | None = None
    memory: MemoryPassReport | None = None
    retention: RetentionReport | None = None


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


_DEFAULT_RETENTION = object()


def maintain(
    *,
    graph: Any | None = None,
    store: Any | None = None,
    checkpointer: Any | None = None,
    expiry: ExpiryPolicy | None = None,
    distiller: LLM | None = None,
    retention: RetentionPolicy | None | object = _DEFAULT_RETENTION,
    limit: int = 20,
    out_dir: str | Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> MaintenanceReport:
    """Execute maintenance jobs across the agent's checkpointer and store."""
    effective_retention: RetentionPolicy | None = (
        DEFAULT_RETENTION_POLICY
        if retention is _DEFAULT_RETENTION
        else (retention if isinstance(retention, RetentionPolicy) else None)
    )
    if graph is not None:
        if checkpointer is None:
            checkpointer = getattr(graph, "checkpointer", None)
        if store is None:
            store = getattr(graph, "memory", None)

    if checkpointer is None and store is None:
        raise ValueError("maintain requires an agent with a checkpointer or a store")

    if expiry is not None and checkpointer is None:
        raise ValueError("expiry requires an agent with a checkpointer")

    if checkpointer is not None:
        if not isinstance(checkpointer, (InMemorySaver, _SqliteSaver)):
            raise NotImplementedError(
                f"checkpointer {type(checkpointer).__name__} is not supported"
            )
        if graph is not None:
            channels = getattr(graph, "channels", {})
            if isinstance(channels, dict):
                for name, ch in channels.items():
                    if _DeltaChannel is not None and isinstance(ch, _DeltaChannel):
                        raise ValueError(f"keep_latest does not support DeltaChannel: {name}")

    if distiller is not None:
        if store is None or not isinstance(store, MaintainableStore):
            raise ValueError("distiller requires a MaintainableStore")
        if out_dir is None:
            store_path = getattr(store, "path", None)
            if not store_path:
                raise ValueError("distiller with no out_dir requires a store with a path")
            effective_out_dir: str | Path = Path(f"{store_path}.bundles")
        else:
            effective_out_dir = out_dir

    unsupported_store = False
    if store is not None and not isinstance(store, MaintainableStore):
        unsupported_store = True

    failures: list[Exception] = []
    report = MaintenanceReport()

    # Job 1: Thread Expiry & Recovery Sweep
    if checkpointer is not None:
        try:
            exp_runner = ThreadExpiry(checkpointer, policy=expiry, store=store)
            report.expiry = exp_runner.run(now=now, dry_run=dry_run)
        except Exception as exc:
            failures.append(exc)

    # Job 2: Memory Pass with Owner Routing
    if distiller is not None and store is not None and isinstance(store, MaintainableStore):
        try:
            with store_guard(store) as acquired:
                if not acquired:
                    report.memory = MemoryPassReport(busy=True)
                else:
                    mem_report = MemoryPassReport()
                    report.memory = mem_report

                    candidates = unconsolidated_episodes(store, sources=None)
                    owners = episode_owners(store)

                    unowned_count = 0
                    owned_candidates: list[str] = []
                    for r in candidates:
                        owner = owners.get(r)
                        if owner is None or (not owner.user_id and not owner.session_id):
                            unowned_count += 1
                        else:
                            owned_candidates.append(r)
                    mem_report.unowned = unowned_count

                    remaining = limit
                    tried: set[str] = set()
                    blocked: set[tuple[str | None, str | None]] = set()

                    def get_owner_key(run_id: str) -> tuple[str | None, str | None]:
                        o = owners[run_id]
                        return (o.user_id, o.session_id)

                    while remaining > 0:
                        available = [
                            r
                            for r in owned_candidates
                            if r not in tried and get_owner_key(r) not in blocked
                        ]
                        if not available:
                            break
                        picked = available[:remaining]
                        tried.update(picked)

                        batches: dict[tuple[str | None, str | None], list[str]] = {}
                        for r in picked:
                            batches.setdefault(get_owner_key(r), []).append(r)

                        for o_key, batch in batches.items():
                            u_id, s_id = o_key
                            if u_id:
                                target_scope = persona(u_id)
                            else:
                                assert s_id is not None
                                target_scope = session(s_id)
                            try:
                                rec_count, b_path, ing = _consolidate_batch(
                                    store,
                                    distiller,
                                    episodes=batch,
                                    scope=target_scope,
                                    constructor="memory-pass",
                                    out_dir=effective_out_dir,
                                    only_owner=True,
                                    dry_run=dry_run,
                                )
                                remaining -= len(batch)
                                mem_report.episodes.extend(batch)
                                mem_report.records += rec_count
                                if b_path:
                                    mem_report.bundles.append(b_path)
                                if ing:
                                    mem_report.ingested = True
                            except Exception as exc:
                                blocked.add(o_key)
                                failures.append(exc)
        except Exception as exc:
            failures.append(exc)

    # Job 3: Old Checkpoints Pruning
    ret_report = RetentionReport()
    if (
        checkpointer is not None
        and effective_retention is not None
        and effective_retention.checkpoints == "latest"
    ):
        try:
            exp_helper = ThreadExpiry(checkpointer)
            for thread_info in exp_helper.threads(now=now):
                tid = thread_info.thread_id
                try:
                    with thread_lock(checkpointer, tid):
                        exists, _latest_run_id, is_paused = exp_helper._thread_state_locked(tid)
                        if not exists:
                            continue
                        if is_paused:
                            ret_report.skipped_paused += 1
                            continue

                        if isinstance(checkpointer, InMemorySaver):
                            root_cps = checkpointer.storage.get(tid, {}).get("", {})
                            other_ns = any(ns != "" for ns in checkpointer.storage.get(tid, {}))
                            has_old = len(root_cps) > 1 or other_ns
                            if has_old:
                                ret_report.checkpoints_pruned += 1
                                if not dry_run:
                                    _keep_latest_in_memory(checkpointer, tid, is_paused=False)
                        elif isinstance(checkpointer, _SqliteSaver):
                            with checkpointer.lock:
                                cursor = checkpointer.conn.cursor()
                                cursor.execute(
                                    "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ? AND "
                                    "(checkpoint_ns != '' OR checkpoint_id != ("
                                    "SELECT MAX(checkpoint_id) FROM checkpoints "
                                    "WHERE thread_id = ? AND checkpoint_ns = ''))",
                                    (tid, tid),
                                )
                                cnt = cursor.fetchone()[0]
                            if cnt > 0:
                                ret_report.checkpoints_pruned += 1
                                if not dry_run:
                                    _keep_latest_sqlite(checkpointer, tid, is_paused=False)
                        else:
                            ret_report.checkpoints_pruned += 1
                            if not dry_run:
                                checkpointer.prune([tid], strategy="keep_latest")
                except SessionBusy:
                    ret_report.skipped_busy += 1
        except Exception as exc:
            failures.append(exc)

    # Job 4: Transcript Retention
    if effective_retention is not None:
        if unsupported_store:
            ret_report.unsupported_store = True
            report.retention = ret_report
        elif store is not None and isinstance(store, MaintainableStore):
            try:
                ret_job = EpisodeRetention(
                    store,
                    effective_retention,
                    sources=None,
                    consolidating=(distiller is not None),
                )
                sub_report = ret_job.run(now=now, dry_run=dry_run)
                ret_report.pruned = sub_report.pruned
                ret_report.pruned_unconsolidated = sub_report.pruned_unconsolidated
                ret_report.skipped_parked = sub_report.skipped_parked
                ret_report.skipped_unconsolidated = sub_report.skipped_unconsolidated
                ret_report.skipped_prior_context = sub_report.skipped_prior_context
                ret_report.skipped_no_status = sub_report.skipped_no_status
                ret_report.over_budget = sub_report.over_budget
                report.retention = ret_report
            except Exception as exc:
                failures.append(exc)
        elif checkpointer is not None and effective_retention.checkpoints == "latest":
            report.retention = ret_report

    if failures:
        raise ExceptionGroup("maintain encountered errors", failures)

    return report
