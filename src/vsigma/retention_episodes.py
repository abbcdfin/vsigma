"""Episode retention — pruning trajectory without losing episodes.

Design: docs/proposals/episode-retention.md. Enforces the episode rung's lifespan
for transcripts, pruning capture rows while preserving conclusions, summaries,
index ownership, and writing attributable content-hashed tombstones.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from .episodes import (
    FINAL_EPISODE_STATUSES,
    _owned_by,
    episode_owners,
    list_episode_statuses,
)
from .memory import MaintainableStore, _dump_json
from .memory_pass import MAINTENANCE_NS
from .scope import Scope

_EPISODE_PREFIX = "episode:"
_CONTEXT_SUFFIX = "/context"
TOMBSTONE_NS = "episodes/tombstone"


@dataclass(frozen=True)
class RetentionPolicy:
    """Declarative bounds on what memory keeps."""

    max_bytes: int | None = 1024**3
    keep_last: int | None = None
    max_age: timedelta | None = None
    checkpoints: Literal["all", "latest"] = "latest"
    prune_unconsolidated: bool = False

    def __post_init__(self) -> None:
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError(f"max_bytes must be >= 1, got {self.max_bytes}")
        if self.keep_last is not None and self.keep_last < 1:
            raise ValueError(f"keep_last must be >= 1, got {self.keep_last}")
        if self.max_age is not None and self.max_age <= timedelta(0):
            raise ValueError(f"max_age must be positive, got {self.max_age}")


DEFAULT_RETENTION_POLICY = RetentionPolicy()


@dataclass
class RetentionReport:
    """What one `EpisodeRetention` run did."""

    pruned: int = 0
    pruned_unconsolidated: int = 0
    skipped_parked: int = 0
    skipped_unconsolidated: int = 0
    skipped_prior_context: int = 0
    skipped_no_status: int = 0
    skipped_busy: int = 0
    skipped_paused: int = 0
    over_budget: int = 0
    checkpoints_pruned: int = 0
    unsupported_store: bool = False


def _capture_rows(store: MaintainableStore, run_id: str) -> list[tuple[str, object]]:
    """Capture rows in an episode transcript (keys not starting with 'episode:')."""
    ns = f"{_EPISODE_PREFIX}{run_id}{_CONTEXT_SUFFIX}"
    return [(k, v) for k, v in store.query(ns) if not k.startswith("episode:")]


def _hash_capture_rows(rows: list[tuple[str, object]]) -> str:
    """Canonical ASCII sha256 hash of capture rows in first-write order."""
    hasher = hashlib.sha256()
    for k, v in rows:
        norm_v = json.loads(_dump_json(v)[0])
        chunk = json.dumps({"k": k, "v": norm_v}, sort_keys=True, ensure_ascii=True)
        hasher.update(chunk.encode("ascii"))
    return hasher.hexdigest()


def _is_consolidation_in_use(store: MaintainableStore, consolidating_arg: bool) -> bool:
    if consolidating_arg:
        return True
    for k, _ in store.query(MAINTENANCE_NS):
        if k.startswith("deep:"):
            return True
    for _, v in store.query(TOMBSTONE_NS):
        if isinstance(v, dict) and v.get("disposition") == "pruned":
            return True
    return False


class EpisodeRetention:
    """The transcript pruning background job."""

    def __init__(
        self,
        store: MaintainableStore,
        policy: RetentionPolicy,
        *,
        sources: list[Scope] | None,
        consolidating: bool = False,
    ) -> None:
        if not isinstance(store, MaintainableStore):
            raise ValueError(
                f"EpisodeRetention requires a MaintainableStore, got {type(store).__name__}"
            )
        self.store = store
        self.policy = policy
        self.sources = sources
        self.consolidating = consolidating

    def run(self, *, now: datetime | None = None, dry_run: bool = False) -> RetentionReport:
        report = RetentionReport()

        if now is None:
            now_dt = datetime.now()
        elif now.tzinfo is not None:
            now_dt = now.astimezone().replace(tzinfo=None)
        else:
            now_dt = now

        # 1. Crash recovery: complete interrupted prunes
        tombstones: dict[str, Any] = dict(self.store.query(TOMBSTONE_NS))
        for run_id, _t_val in tombstones.items():
            c_rows = _capture_rows(self.store, run_id)
            has_marker = self.store.read(MAINTENANCE_NS, f"deep:{run_id}") is not None
            if c_rows or has_marker:
                if not dry_run:
                    if c_rows:
                        self.store.delete(
                            f"{_EPISODE_PREFIX}{run_id}{_CONTEXT_SUFFIX}",
                            [k for k, _ in c_rows],
                        )
                    if has_marker:
                        self.store.delete(MAINTENANCE_NS, [f"deep:{run_id}"])

        consolidation_in_use = _is_consolidation_in_use(self.store, self.consolidating)
        sizes = self.store.namespace_sizes()
        statuses = list_episode_statuses(self.store)
        owners = episode_owners(self.store)
        markers = {
            k.removeprefix("deep:"): v
            for k, v in self.store.query(MAINTENANCE_NS)
            if k.startswith("deep:")
        }

        # Track session order: list of run_ids per session in index order
        session_order: dict[str, list[str]] = {}
        for run_id, owner in owners.items():
            if owner.session_id:
                session_order.setdefault(owner.session_id, []).append(run_id)

        # Build list of all indexed unpruned episodes in index order
        indexed_runs: list[str] = [
            run_id for run_id in owners.keys() if run_id not in tombstones
        ]

        # Group by (owner, task_id) where owner is (user_id, session_id)
        groups: dict[tuple[tuple[str | None, str | None], str], list[str]] = {}
        for run_id in indexed_runs:
            owner = owners[run_id]
            key = ((owner.user_id, owner.session_id), owner.task_id)
            groups.setdefault(key, []).append(run_id)

        # Helper to check if an episode is consolidated
        def _check_consolidated(run_id: str) -> bool:
            marker = markers.get(run_id)
            if isinstance(marker, dict):
                rec_size = marker.get("size")
                curr_size = sizes.get(f"{_EPISODE_PREFIX}{run_id}{_CONTEXT_SUFFIX}", 0)
                return rec_size is not None and rec_size == curr_size
            return False

        # Identify candidate runs flagged by keep_last or max_age
        selected_runs: set[str] = set()

        if self.policy.keep_last is not None:
            for group_runs in groups.values():
                if len(group_runs) > self.policy.keep_last:
                    # Excess older episodes break keep_last
                    for r in group_runs[: -self.policy.keep_last]:
                        selected_runs.add(r)

        if self.policy.max_age is not None:
            for run_id in indexed_runs:
                owner = owners[run_id]
                try:
                    created_dt = datetime.fromisoformat(owner.created_at)
                    if created_dt.tzinfo is not None:
                        created_dt = created_dt.astimezone().replace(tzinfo=None)
                    if (now_dt - created_dt) > self.policy.max_age:
                        selected_runs.add(run_id)
                except Exception:
                    pass

        # Helper to check eligibility for candidate runs
        def _evaluate_eligibility(run_id: str) -> bool | None:
            """Return True/False if consolidated/unconsolidated, or None if ineligible."""
            # 1. Ended status
            status = statuses.get(run_id)
            if status is None:
                report.skipped_no_status += 1
                return None
            if status not in FINAL_EPISODE_STATUSES:
                report.skipped_parked += 1
                return None

            # 2. Consolidated check
            is_cons = _check_consolidated(run_id)
            if not is_cons:
                owner = owners.get(run_id)
                has_owner = owner is not None and (
                    owner.user_id is not None or owner.session_id is not None
                )
                if (
                    consolidation_in_use
                    and has_owner
                    and not self.policy.prune_unconsolidated
                ):
                    report.skipped_unconsolidated += 1
                    return None

            # 3. Prior context check
            if consolidation_in_use and not self.policy.prune_unconsolidated:
                owner = owners.get(run_id)
                if owner and owner.session_id:
                    s_runs = session_order.get(owner.session_id, [])
                    if run_id in s_runs:
                        idx = s_runs.index(run_id)
                        next_3 = s_runs[idx + 1 : idx + 4]
                        for later_r in next_3:
                            if later_r in tombstones:
                                # Pruned episodes count toward the 3 but never block
                                continue
                            if not _check_consolidated(later_r):
                                report.skipped_prior_context += 1
                                return None

            # 4. Sources boundary
            if self.sources is not None:
                owner = owners.get(run_id)
                if owner is None or not _owned_by(owner, self.sources):
                    return None

            return is_cons

        to_prune: list[tuple[str, bool]] = []  # (run_id, is_consolidated)

        # First, evaluate eligibility of explicitly selected runs (keep_last, max_age)
        for run_id in indexed_runs:
            if run_id in selected_runs:
                res = _evaluate_eligibility(run_id)
                if res is not None:
                    to_prune.append((run_id, res))

        # Next, evaluate max_bytes budget
        # Transcript budget counts all episode:*/context namespaces
        total_transcript_bytes = sum(
            size
            for ns, size in sizes.items()
            if ns.startswith(_EPISODE_PREFIX) and ns.endswith(_CONTEXT_SUFFIX)
        )

        pruned_set = {r for r, _ in to_prune}
        for run_id, _ in to_prune:
            rows = _capture_rows(self.store, run_id)
            freed = sum(_dump_json(v)[1] for _, v in rows)
            total_transcript_bytes -= freed

        if self.policy.max_bytes is not None and total_transcript_bytes > self.policy.max_bytes:
            # Over budget: consolidated first, oldest created_at first (index order breaks ties)
            remaining_runs = [r for r in indexed_runs if r not in pruned_set]

            # Partition into consolidated and unconsolidated
            evaluated_remaining: list[tuple[str, bool]] = []
            for r in remaining_runs:
                res = _evaluate_eligibility(r)
                if res is not None:
                    evaluated_remaining.append((r, res))

            # Sort: consolidated (True) before unconsolidated (False), then by created_at
            def _sort_key(item: tuple[str, bool]) -> tuple[int, datetime, int]:
                r, is_cons = item
                owner = owners[r]
                try:
                    c_dt = datetime.fromisoformat(owner.created_at)
                    if c_dt.tzinfo is not None:
                        c_dt = c_dt.astimezone().replace(tzinfo=None)
                except Exception:
                    c_dt = datetime.min
                # is_cons: 0 for True (comes first), 1 for False
                idx = indexed_runs.index(r)
                return (0 if is_cons else 1, c_dt, idx)

            evaluated_remaining.sort(key=_sort_key)

            for r, is_cons in evaluated_remaining:
                if total_transcript_bytes <= self.policy.max_bytes:
                    break
                rows = _capture_rows(self.store, r)
                freed = sum(_dump_json(v)[1] for _, v in rows)
                total_transcript_bytes -= freed
                to_prune.append((r, is_cons))

            if total_transcript_bytes > self.policy.max_bytes:
                report.over_budget = total_transcript_bytes - self.policy.max_bytes

        # Execute pruning
        for run_id, is_cons in to_prune:
            rows = _capture_rows(self.store, run_id)
            c_hash = _hash_capture_rows(rows)
            disposition = "pruned" if is_cons else "pruned_unconsolidated"

            if not dry_run:
                self.store.write(
                    TOMBSTONE_NS,
                    run_id,
                    {
                        "disposition": disposition,
                        "pruned_at": now_dt.isoformat(),
                        "content_sha256": c_hash,
                    },
                )
                if rows:
                    self.store.delete(
                        f"{_EPISODE_PREFIX}{run_id}{_CONTEXT_SUFFIX}",
                        [k for k, _ in rows],
                    )
                self.store.delete(MAINTENANCE_NS, [f"deep:{run_id}"])

            if is_cons:
                report.pruned += 1
            else:
                report.pruned_unconsolidated += 1

        return report
