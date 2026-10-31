"""The `vsigma` command-line interface.

`vsigma memory validate <bundle>` / `vsigma memory ingest <bundle> --db <file>`
— the bundle gate as a CLI, so external constructors (and operators) can check
and commit memory bundles without writing Python.

`vsigma memory consolidate --db <file> --source <scope> --scope <scope>` — the
maintenance pass (the deep round as a runnable, cron-shaped operation; see
docs/proposals/maintenance-pass.md). Two boundaries, both mandatory and easy to
confuse: `--source` is whose episodes may be *read*, `--scope` is where the
distilled knowledge *lands*. Neither has a default, because a pass that infers
either one can quietly distill every tenant's episodes into a single scope
(docs/proposals/memory-scope-promotion.md §B.3).

`vsigma memory export --db <file>` — read-side inspection: records as JSON lines.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from .bundle import ingest_bundle, validate_bundle
from .llm import default_llm
from .maintenance import parse_scope, parse_source
from .memory_pass import MemoryPass
from .retention_episodes import EpisodeRetention, RetentionPolicy
from .stores import PersistentStore


def _open_store(path: str | Path) -> PersistentStore | None:
    try:
        return PersistentStore(path)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vsigma", description="vsigma toolkit CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    memory = commands.add_parser(
        "memory", help="memory operations (validate / ingest / consolidate / export)"
    )
    actions = memory.add_subparsers(dest="action", required=True)

    validate = actions.add_parser("validate", help="check a bundle against the schema")
    validate.add_argument("bundle", help="path to the bundle directory")

    ingest = actions.add_parser("ingest", help="validate, then commit a bundle to a store")
    ingest.add_argument("bundle", help="path to the bundle directory")
    ingest.add_argument("--db", required=True, help="SQLite store file to ingest into")

    consolidate = actions.add_parser(
        "consolidate",
        help="deep-consolidate unprocessed episodes into knowledge (via the bundle gate)",
    )
    consolidate.add_argument("--db", required=True, help="SQLite store file")
    consolidate.add_argument(
        "--source",
        action="append",
        metavar="SCOPE",
        help="whose episodes this pass may read: persona:<id> | session:<id>. "
        "Repeatable. Required unless --all-sources.",
    )
    consolidate.add_argument(
        "--all-sources",
        action="store_true",
        help="read every tenant's episodes — say so explicitly",
    )
    consolidate.add_argument(
        "--scope",
        required=True,
        help="where distilled knowledge lands: agent | global | persona:<id> | "
        "session:<id>. No default: agent scope is visible to every persona, "
        "so the destination is a decision, not a fallback.",
    )
    consolidate.add_argument(
        "--out", default="./bundles", help="bundle output directory (default: ./bundles)"
    )
    consolidate.add_argument(
        "--limit", type=int, default=20, help="max episodes per invocation (default: 20)"
    )
    consolidate.add_argument(
        "--dry-run",
        action="store_true",
        help="write and print the bundle; do not ingest, do not advance markers",
    )
    consolidate.add_argument("--model", help="override VSIGMA_LLM_MODEL for this pass")
    consolidate.add_argument(
        "--constructor", help="provenance label (default: <model>/deep)"
    )

    export = actions.add_parser(
        "export", help="dump store records as JSON lines (read-only inspection)"
    )
    export.add_argument("--db", required=True, help="SQLite store file")
    export.add_argument("--namespace", help="only this namespace (default: all)")

    maintain_cmd = actions.add_parser(
        "maintain", help="consolidate memory and prune transcripts"
    )
    maintain_cmd.add_argument("--db", required=True, help="SQLite store file")
    maintain_cmd.add_argument(
        "--source",
        action="append",
        metavar="SCOPE",
        help="whose episodes this pass may read: persona:<id> | session:<id>. "
        "Repeatable. Required unless --all-sources.",
    )
    maintain_cmd.add_argument(
        "--all-sources",
        action="store_true",
        help="read every tenant's episodes — say so explicitly",
    )
    maintain_cmd.add_argument(
        "--scope",
        required=True,
        help="where distilled knowledge lands: agent | global | persona:<id> | "
        "session:<id>. No default: agent scope is visible to every persona, "
        "so the destination is a decision, not a fallback.",
    )
    maintain_cmd.add_argument(
        "--out", default="./bundles", help="bundle output directory (default: ./bundles)"
    )
    maintain_cmd.add_argument(
        "--limit", type=int, default=20, help="max episodes per invocation (default: 20)"
    )
    maintain_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="simulate; do not ingest or prune",
    )
    maintain_cmd.add_argument("--model", help="override VSIGMA_LLM_MODEL for this pass")
    maintain_cmd.add_argument(
        "--constructor", help="provenance label (default: <model>/deep)"
    )
    maintain_cmd.add_argument(
        "--max-bytes", type=int, help="max bytes of transcripts to keep (default: 1 GiB)"
    )
    maintain_cmd.add_argument(
        "--keep-last", type=int, help="keep N newest episodes per (owner, task_id)"
    )
    maintain_cmd.add_argument(
        "--max-age", type=int, help="max age of episodes in days"
    )
    maintain_cmd.add_argument(
        "--prune-unconsolidated",
        action="store_true",
        help="prune unconsolidated transcripts if over budget",
    )

    return parser


def _consolidate(args: argparse.Namespace) -> int:
    if args.model:
        os.environ["VSIGMA_LLM_MODEL"] = args.model
    llm = default_llm()
    if llm is None:
        print(
            "error: no LLM configured (set VSIGMA_LLM_BASE_URL / VSIGMA_LLM_API_KEY);"
            " the deep round requires a capable model",
            file=sys.stderr,
        )
        return 1
    if bool(args.source) == bool(args.all_sources):
        print(
            "error: state an ownership boundary — one or more --source "
            "persona:<id>|session:<id>, or --all-sources for store-wide. "
            "Without one, a single pass distills every tenant's episodes into "
            "the one --scope you named.",
            file=sys.stderr,
        )
        return 1
    try:
        scope = parse_scope(args.scope)
        sources = None if args.all_sources else [parse_source(s) for s in args.source]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    constructor = args.constructor or f"{getattr(llm, 'model', 'unknown')}/deep"
    store = _open_store(args.db)
    if store is None:
        return 1
    try:
        pass_runner = MemoryPass(
            store,
            llm,
            sources=sources,
            scope=scope,
            constructor=constructor,
            out_dir=args.out,
            limit=args.limit,
        )
        report = pass_runner.run(dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc} (no markers advanced; rerun retries the batch)", file=sys.stderr)
        return 1
    finally:
        store.close()
    if not report.episodes:
        print("nothing to consolidate")
        return 0
    summary = f"{len(report.episodes)} episode(s) -> {report.records} record(s)"
    if args.dry_run:
        print(f"dry-run: {summary}; not ingested, markers not advanced")
        for bundle in report.bundles:
            print(f"bundle: {bundle}")
            print((Path(bundle) / "records.jsonl").read_text(encoding="utf-8"), end="")
        return 0
    first_bundle = report.bundles[0] if report.bundles else "none"
    print(f"ok: {summary} ingested into {args.db} (bundle: {first_bundle})")
    return 0


def _export(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    if store is None:
        return 1
    try:
        namespaces = [args.namespace] if args.namespace else store.namespaces()
        for namespace in namespaces:
            for key, value in store.query(namespace):
                line = {"namespace": namespace, "key": key, "value": value}
                print(json.dumps(line, ensure_ascii=False, default=str))
    finally:
        store.close()
    return 0


def _maintain(args: argparse.Namespace) -> int:
    if bool(args.source) == bool(args.all_sources):
        print(
            "error: state an ownership boundary — one or more --source "
            "persona:<id>|session:<id>, or --all-sources for store-wide. "
            "Without one, a single pass distills every tenant's episodes into "
            "the one --scope you named.",
            file=sys.stderr,
        )
        return 1
    if args.model:
        os.environ["VSIGMA_LLM_MODEL"] = args.model
    llm = default_llm()
    if llm is None:
        print(
            "error: no LLM configured (set VSIGMA_LLM_BASE_URL / VSIGMA_LLM_API_KEY);"
            " the deep round requires a capable model",
            file=sys.stderr,
        )
        return 1
    try:
        scope = parse_scope(args.scope)
        sources = None if args.all_sources else [parse_source(s) for s in args.source]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    constructor = args.constructor or f"{getattr(llm, 'model', 'unknown')}/deep"
    store = _open_store(args.db)
    if store is None:
        return 1

    try:
        pass_runner = MemoryPass(
            store,
            llm,
            sources=sources,
            scope=scope,
            constructor=constructor,
            out_dir=args.out,
            limit=args.limit,
        )
        mem_report = pass_runner.run(dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc} (no markers advanced; rerun retries the batch)", file=sys.stderr)
        store.close()
        return 1

    try:
        max_age_td = timedelta(days=args.max_age) if args.max_age is not None else None
        ret_kwargs: dict[str, Any] = {}
        if args.max_bytes is not None:
            ret_kwargs["max_bytes"] = args.max_bytes
        if args.keep_last is not None:
            ret_kwargs["keep_last"] = args.keep_last
        if max_age_td is not None:
            ret_kwargs["max_age"] = max_age_td
        if args.prune_unconsolidated:
            ret_kwargs["prune_unconsolidated"] = True

        policy = RetentionPolicy(**ret_kwargs)
        ret_job = EpisodeRetention(
            store,
            policy,
            sources=sources,
            consolidating=True,
        )
        ret_report = ret_job.run(dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    summary_mem = f"{len(mem_report.episodes)} episode(s) -> {mem_report.records} record(s)"
    summary_ret = f"{ret_report.pruned} episode(s) pruned"
    if args.dry_run:
        print(f"dry-run: {summary_mem}; {summary_ret}")
        for bundle in mem_report.bundles:
            print(f"bundle: {bundle}")
            print((Path(bundle) / "records.jsonl").read_text(encoding="utf-8"), end="")
        return 0

    first_bundle = mem_report.bundles[0] if mem_report.bundles else "none"
    print(f"ok: {summary_mem} ingested into {args.db} (bundle: {first_bundle}); {summary_ret}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "memory" and args.action == "validate":
        errors = validate_bundle(args.bundle)
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"ok: {args.bundle} is a valid memory bundle")
        return 0

    if args.command == "memory" and args.action == "ingest":
        store = _open_store(args.db)
        if store is None:
            return 1
        try:
            count = ingest_bundle(args.bundle, store)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            store.close()
        print(f"ok: ingested {count} records into {args.db}")
        return 0

    if args.command == "memory" and args.action == "consolidate":
        return _consolidate(args)

    if args.command == "memory" and args.action == "maintain":
        return _maintain(args)

    if args.command == "memory" and args.action == "export":
        return _export(args)

    return 2  # pragma: no cover — argparse enforces the command set


if __name__ == "__main__":
    raise SystemExit(main())
