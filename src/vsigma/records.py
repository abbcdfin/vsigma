"""Memory records — the §D record shape, with mandatory provenance.

``record = (scope, kind, content, provenance{origin, consolidator?, source_ref,
confidence, constructor, version})``. Provenance is not optional: injected
knowledge must trace to a source span, consolidated knowledge to a trajectory —
so a doc rebuild can replace injected records without touching consolidated
ones, retrieval can weight by origin/confidence, and bad consolidations can be
pruned without disturbing the curated base.

Records round-trip through plain dicts — the JSONL line format of the memory
bundle (see `bundle.py`) and the storable value for any `MemoryStore`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, cast, get_args

from .scope import KINDS, SCOPE_LEVELS, Kind

Origin = Literal["injected", "consolidated"]
Consolidator = Literal["reflexive", "deep"]

ORIGINS: tuple[str, ...] = get_args(Origin)
CONSOLIDATORS: tuple[str, ...] = get_args(Consolidator)


@dataclass(frozen=True)
class Provenance:
    """Where a record came from — mandatory on every record."""

    origin: Origin
    source_ref: str  # doc span (injected) or trajectory ref (consolidated)
    confidence: float = 1.0
    consolidator: Consolidator | None = None  # set when origin == consolidated
    constructor: str = ""  # which model/agent (+ version) produced it
    version: str = "0"


@dataclass(frozen=True)
class MemoryRecord:
    """One memory fact, addressed by (scope, kind) per the running-context model."""

    id: str
    scope: str  # scope head, e.g. "global" or "persona:alice" (see Scope.head)
    kind: Kind
    content: str
    provenance: Provenance

    @property
    def namespace(self) -> str:
        return f"{self.scope}/{self.kind}"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _scope_level(scope: str) -> str:
    return scope.split(":", 1)[0]


def record_errors(raw: dict[str, object]) -> list[str]:
    """Deterministic validation of one raw record dict; returns human-readable
    errors (empty = valid). Shared by `from_dict` and the bundle gate."""
    errors: list[str] = []
    for field_name in ("id", "scope", "kind", "content"):
        value = raw.get(field_name)
        if not isinstance(value, str) or not value:
            errors.append(f"record field '{field_name}' must be a non-empty string")
    scope = raw.get("scope")
    if isinstance(scope, str) and scope and _scope_level(scope) not in SCOPE_LEVELS:
        errors.append(f"unknown scope level '{_scope_level(scope)}'")
    if raw.get("kind") not in KINDS:
        errors.append(f"kind must be one of {list(KINDS)}")
    prov = raw.get("provenance")
    if not isinstance(prov, dict):
        errors.append("record is missing its provenance object")
        return errors
    if prov.get("origin") not in ORIGINS:
        errors.append(f"provenance.origin must be one of {list(ORIGINS)}")
    source_ref = prov.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        errors.append("provenance.source_ref must be a non-empty string")
    if prov.get("origin") == "consolidated" and prov.get("consolidator") not in CONSOLIDATORS:
        errors.append(
            f"consolidated records need provenance.consolidator in {list(CONSOLIDATORS)}"
        )
    confidence = prov.get("confidence", 1.0)
    if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
        errors.append("provenance.confidence must be a number in [0, 1]")
    return errors


def record_from_dict(raw: dict[str, object]) -> MemoryRecord:
    """Parse (and validate) a raw dict into a `MemoryRecord`; raises ValueError."""
    errors = record_errors(raw)
    if errors:
        raise ValueError("; ".join(errors))
    prov = cast("dict[str, object]", raw["provenance"])
    return MemoryRecord(
        id=cast("str", raw["id"]),
        scope=cast("str", raw["scope"]),
        kind=cast("Kind", raw["kind"]),
        content=cast("str", raw["content"]),
        provenance=Provenance(
            origin=cast("Origin", prov["origin"]),
            source_ref=cast("str", prov["source_ref"]),
            confidence=float(cast("float", prov.get("confidence", 1.0))),
            consolidator=cast("Consolidator | None", prov.get("consolidator")),
            constructor=str(prov.get("constructor", "")),
            version=str(prov.get("version", "0")),
        ),
    )
