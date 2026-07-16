"""The `vsigma` command-line interface.

`vsigma memory validate <bundle>` / `vsigma memory ingest <bundle> --db <file>`
— the bundle gate as a CLI, so external constructors (and operators) can check
and commit memory bundles without writing Python.

`vsigma memory consolidate --db <file>` — the maintenance pass (the deep round
as a runnable, cron-shaped operation; see docs/proposals/maintenance-pass.md).

`vsigma memory export --db <file>` — read-side inspection: records as JSON lines.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .bundle import ingest_bundle, validate_bundle
from .llm import default_llm
from .maintenance import parse_scope, run_maintenance
from .stores import SQLiteStore


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
        "--scope",
        default="agent",
        help="where distilled knowledge lands: agent | global | persona:<id> | "
        "session:<id> (default: agent)",
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
    try:
        scope = parse_scope(args.scope)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    constructor = args.constructor or f"{getattr(llm, 'model', 'unknown')}/deep"
    store = SQLiteStore(args.db)
    try:
        report = run_maintenance(
            store,
            llm,
            scope=scope,
            constructor=constructor,
            out_dir=args.out,
            limit=args.limit,
            dry_run=args.dry_run,
        )
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
        if report.bundle:
            print(f"bundle: {report.bundle}")
            print((Path(report.bundle) / "records.jsonl").read_text(encoding="utf-8"), end="")
        return 0
    print(f"ok: {summary} ingested into {args.db} (bundle: {report.bundle or 'none'})")
    return 0


def _export(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.db)
    try:
        namespaces = [args.namespace] if args.namespace else store.namespaces()
        for namespace in namespaces:
            for key, value in store.query(namespace):
                line = {"namespace": namespace, "key": key, "value": value}
                print(json.dumps(line, ensure_ascii=False, default=str))
    finally:
        store.close()
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
        store = SQLiteStore(args.db)
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

    if args.command == "memory" and args.action == "export":
        return _export(args)

    return 2  # pragma: no cover — argparse enforces the command set


if __name__ == "__main__":
    raise SystemExit(main())
