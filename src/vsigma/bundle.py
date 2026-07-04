"""The memory bundle — the portable constructor contract (§D).

A bundle is plain files, so an external constructor (Claude Code, Antigravity,
a script, a human) can build memory **completely offline, with no vsigma
installed**:

    bundle/
      manifest.json    # schema_version, constructor identity, source inventory
      records.jsonl    # one record per line (the §D record shape)

The agent proposes; the engine commits: live memory is only ever touched by
`ingest_bundle`, after the deterministic `validate_bundle` gate passes. The
published schema lives at docs/schemas/memory-bundle.schema.json (versioned;
`ingest` refuses versions it does not know).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .memory import MemoryStore
from .records import MemoryRecord, record_errors, record_from_dict

SCHEMA_VERSION = "0.1"

MANIFEST = "manifest.json"
RECORDS = "records.jsonl"


def write_bundle(
    path: str | Path,
    records: list[MemoryRecord],
    *,
    constructor: str,
    sources: list[tuple[str, str]] | None = None,
) -> Path:
    """Write records + manifest as a bundle directory (the staging artifact)."""
    bundle = Path(path)
    bundle.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "constructor": constructor,
        "record_count": len(records),
        "sources": [
            {"ref": ref, "sha256": hashlib.sha256(text.encode()).hexdigest()}
            for ref, text in (sources or [])
        ],
    }
    (bundle / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    lines = [json.dumps(record.to_dict()) for record in records]
    (bundle / RECORDS).write_text("\n".join(lines) + ("\n" if lines else ""))
    return bundle


def validate_bundle(path: str | Path) -> list[str]:
    """The deterministic gate: human-readable errors, empty when valid.
    Stdlib-only, so a constructor can run it in its own loop too."""
    bundle = Path(path)
    errors: list[str] = []
    manifest_path = bundle / MANIFEST
    if not manifest_path.is_file():
        return [f"missing {MANIFEST}"]
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{MANIFEST} is not valid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{MANIFEST} must be a JSON object"]
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"unknown schema_version {manifest.get('schema_version')!r}"
            f" (this vsigma understands {SCHEMA_VERSION!r})"
        )
    if not str(manifest.get("constructor", "")):
        errors.append("manifest.constructor (who built this) must be set")

    records_path = bundle / RECORDS
    if not records_path.is_file():
        errors.append(f"missing {RECORDS}")
        return errors
    seen: set[str] = set()
    for lineno, line in enumerate(records_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"{RECORDS}:{lineno}: not valid JSON")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{RECORDS}:{lineno}: each line must be a JSON object")
            continue
        for problem in record_errors(raw):
            errors.append(f"{RECORDS}:{lineno}: {problem}")
        rid = raw.get("id")
        if isinstance(rid, str) and rid:
            if rid in seen:
                errors.append(f"{RECORDS}:{lineno}: duplicate record id {rid!r}")
            seen.add(rid)
    return errors


def read_bundle(path: str | Path) -> list[MemoryRecord]:
    """Parse a *valid* bundle's records (call `validate_bundle` first)."""
    lines = (Path(path) / RECORDS).read_text().splitlines()
    return [record_from_dict(json.loads(line)) for line in lines if line.strip()]


def ingest_bundle(path: str | Path, store: MemoryStore) -> int:
    """Commit a bundle into live memory — the only path that may do so.
    Validates first; raises ValueError if the gate fails. Returns record count.
    Writes are keyed by record id, so a re-ingest (or a later, higher-confidence
    pass targeting the same ids) supersedes rather than duplicates."""
    errors = validate_bundle(path)
    if errors:
        raise ValueError("bundle failed validation:\n" + "\n".join(errors))
    records = read_bundle(path)
    for record in records:
        store.write(record.namespace, record.id, record.to_dict())
    return len(records)
