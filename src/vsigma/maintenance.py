"""The maintenance coordinator and scope parsers.

Design: docs/proposals/maintenance.md. Holds `MAINTENANCE_NS`, CLI scope helpers,
`MaintenanceReport`, and coordinates `agent.maintain()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
from .retention import prune_checkpoints, validate_checkpointer_support
from .retention_episodes import (
    DEFAULT_RETENTION_POLICY,
    EpisodeRetention,
    RetentionPolicy,
    RetentionReport,
)
from .run import _AGENTS, _AGENTS_LOCK, Agent
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


def maintain(
    agent: Agent,
    *,
    expiry: ExpiryPolicy | None = None,
    distiller: LLM | None = None,
    retention: RetentionPolicy | None = DEFAULT_RETENTION_POLICY,
    limit: int = 20,
    out_dir: str | Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> MaintenanceReport:
    """Execute maintenance jobs across the agent's checkpointer and store."""
    if now is not None and now.tzinfo is None:
        now = now.astimezone()

    store = agent.memory
    checkpointer = getattr(agent.graph, "checkpointer", None)

    if checkpointer is None and store is None:
        raise ValueError("maintain requires an agent with a checkpointer or a store")

    if store is not None:
        with _AGENTS_LOCK:
            others = list(_AGENTS)
        if any(a is not agent and a.memory is store for a in others):
            raise ValueError(
                "maintain requires an agent that is the only live Agent using its store"
            )

    if expiry is not None and checkpointer is None:
        raise ValueError("expiry requires an agent with a checkpointer")

    if checkpointer is not None and not isinstance(checkpointer, (InMemorySaver, _SqliteSaver)):
        raise NotImplementedError(
            f"checkpointer {type(checkpointer).__name__} is not supported"
        )

    if retention is not None and not isinstance(retention, RetentionPolicy):
        raise TypeError(
            f"retention must be a RetentionPolicy or None, got {type(retention).__name__}"
        )

    if (
        checkpointer is not None
        and retention is not None
        and retention.checkpoints == "latest"
    ):
        validate_checkpointer_support(agent.graph)

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
    if checkpointer is not None and (expiry is not None or store is not None):
        try:
            exp_runner = ThreadExpiry(checkpointer, policy=expiry, store=store)
            report.expiry = exp_runner.run(now=now, dry_run=dry_run)
        except Exception as exc:
            failures.append(exc)

    # Job 2: Memory Pass with Destination Routing
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

                    def get_destination(run_id: str) -> Scope | None:
                        o = owners.get(run_id)
                        if o is None:
                            return None
                        if o.user_id:
                            return persona(o.user_id)
                        if o.session_id:
                            return session(o.session_id)
                        return None

                    unowned_count = 0
                    owned_candidates: list[str] = []
                    for r in candidates:
                        dest = get_destination(r)
                        if dest is None:
                            unowned_count += 1
                        else:
                            owned_candidates.append(r)
                    mem_report.unowned = unowned_count

                    remaining = limit
                    tried: set[str] = set()
                    blocked: set[Scope] = set()

                    while remaining > 0:
                        available = [
                            r
                            for r in owned_candidates
                            if r not in tried and get_destination(r) not in blocked
                        ]
                        if not available:
                            break
                        picked = available[:remaining]
                        tried.update(picked)

                        batches: dict[Scope, list[str]] = {}
                        for r in picked:
                            d = get_destination(r)
                            assert d is not None
                            batches.setdefault(d, []).append(r)

                        for target_scope, batch in batches.items():
                            try:
                                consolidated, rec_count, b_path, ing = (
                                    _consolidate_batch(
                                        store,
                                        distiller,
                                        episodes=batch,
                                        scope=target_scope,
                                        constructor="memory-pass",
                                        out_dir=effective_out_dir,
                                        only_owner=True,
                                        dry_run=dry_run,
                                        checkpointer=checkpointer,
                                    )
                                )
                                if not consolidated and not dry_run:
                                    blocked.add(target_scope)
                                remaining -= len(consolidated)
                                mem_report.episodes.extend(consolidated)
                                mem_report.records += rec_count
                                if b_path:
                                    mem_report.bundles.append(b_path)
                                if ing:
                                    mem_report.ingested = True
                            except Exception as exc:
                                blocked.add(target_scope)
                                failures.append(exc)
        except Exception as exc:
            failures.append(exc)

    # Job 3: Old Checkpoints Pruning
    ret_report = RetentionReport()
    if (
        checkpointer is not None
        and retention is not None
        and retention.checkpoints == "latest"
    ):
        try:
            exp_helper = ThreadExpiry(checkpointer)
            for thread_info in exp_helper.threads(now=now):
                tid = thread_info.thread_id
                try:
                    with thread_lock(checkpointer, tid) as locked:
                        exists, _latest_run_id, is_paused = exp_helper._thread_state_locked(tid)
                        if not exists:
                            continue
                        if is_paused:
                            ret_report.skipped_paused += 1
                            continue

                        has_old = False
                        if isinstance(checkpointer, InMemorySaver):
                            root_cps = checkpointer.storage.get(tid, {}).get("", {})
                            other_ns = any(ns != "" for ns in checkpointer.storage.get(tid, {}))
                            has_old = len(root_cps) > 1 or other_ns
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
                            has_old = cnt > 0

                        if has_old:
                            ret_report.checkpoints_pruned += 1
                            if not dry_run:
                                prune_checkpoints(agent.graph, locked)
                except SessionBusy:
                    ret_report.skipped_busy += 1
        except Exception as exc:
            failures.append(exc)

    # Job 4: Transcript Retention
    if retention is not None:
        if unsupported_store:
            ret_report.unsupported_store = True
            report.retention = ret_report
        elif store is not None and isinstance(store, MaintainableStore):
            try:
                ret_job = EpisodeRetention(
                    store,
                    retention,
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
        elif checkpointer is not None and retention.checkpoints == "latest":
            report.retention = ret_report

    if failures:
        raise ExceptionGroup("maintain encountered errors", failures)

    return report
