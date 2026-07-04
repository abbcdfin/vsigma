"""The `vsigma` command-line interface.

`vsigma memory validate <bundle>` / `vsigma memory ingest <bundle> --db <file>`
— the bundle gate as a CLI, so external constructors (and operators) can check
and commit memory bundles without writing Python.
"""

from __future__ import annotations

import argparse
import sys

from .bundle import ingest_bundle, validate_bundle
from .stores import SQLiteStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vsigma", description="vsigma toolkit CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    memory = commands.add_parser("memory", help="memory bundle gate (validate / ingest)")
    actions = memory.add_subparsers(dest="action", required=True)

    validate = actions.add_parser("validate", help="check a bundle against the schema")
    validate.add_argument("bundle", help="path to the bundle directory")

    ingest = actions.add_parser("ingest", help="validate, then commit a bundle to a store")
    ingest.add_argument("bundle", help="path to the bundle directory")
    ingest.add_argument("--db", required=True, help="SQLite store file to ingest into")

    return parser


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

    return 2  # pragma: no cover — argparse enforces the command set


if __name__ == "__main__":
    raise SystemExit(main())
