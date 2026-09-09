"""The construction engine — one procedure, three feeds (§D).

`distill` turns source material (documents *or* trajectories — the feed is a
parameter, not a different subsystem) into provenance-carrying `MemoryRecord`s
via a constructor LLM. The output shape is constrained: the model must emit one
JSON object per line (``{"content": ..., "source_ref": ...}``); anything else
is rejected — the LLM fills the schema, it does not invent structure (§A).

Record ids are content-addressed, so re-distilling the same fact (by any round
or constructor) targets the same id — a later, higher-confidence pass
*supersedes* an earlier record instead of duplicating it (§E), **provided both
land in the same namespace**. A record is stored at `<scope>/<kind>`, and the
scope is chosen by the caller, not derived from the content: the same fact
distilled into a wider scope is a second row, not a replacement. That is a
*promotion*, with a cost — see docs/proposals/memory-scope-promotion.md.

This is the in-process library path; external agents skip it entirely and
produce a bundle directly (see `bundle.py`).
"""

from __future__ import annotations

import hashlib
import json

from .llm import LLM
from .records import Consolidator, MemoryRecord, Origin, Provenance
from .scope import Kind, Scope

#: One source: (ref, text). The ref becomes provenance.source_ref fallback.
Source = tuple[str, str]

_DISTILL_SYSTEM = (
    "You distill source material into atomic memory facts. Reply with ONE JSON "
    'object per line, each exactly {"content": "<one self-contained fact>", '
    '"source_ref": "<the ref of the source it came from>"}. No prose, no '
    "markdown fences, no other keys. Omit anything not grounded in a source. "
    # The durability bar: shape rules alone admit true-but-useless facts
    # ("the run took 34s"); the concrete exclusion list is what small local
    # models actually follow.
    "Record only facts that will remain true and useful in a future, unrelated "
    "session. Never record run ephemera: execution times or durations, "
    "timestamps or dates of the run itself, run/session/request ids, or "
    "transient errors that were already resolved."
)


def record_id(kind: str, content: str) -> str:
    """Content-addressed id: the same fact always lands on the same record."""
    return f"{kind}:{hashlib.sha256(content.encode()).hexdigest()[:12]}"


def distill(
    sources: list[Source],
    llm: LLM,
    *,
    scope: Scope,
    kind: Kind = "knowledge",
    origin: Origin = "injected",
    consolidator: Consolidator | None = None,
    confidence: float = 1.0,
    constructor: str = "",
) -> list[MemoryRecord]:
    """One constructor pass: sources -> validated records. Raises ValueError if
    the model breaks the constrained output shape."""
    body = "\n\n".join(f"[source ref={ref}]\n{text}" for ref, text in sources)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": body},
    ]
    response = llm.complete(messages, [])
    records: list[MemoryRecord] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            fact = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"constructor broke the output shape: {line!r}") from exc
        content = str(fact.get("content", "")).strip()
        if not content:
            raise ValueError(f"constructor emitted a fact without content: {line!r}")
        records.append(
            MemoryRecord(
                id=record_id(kind, content),
                scope=scope.head,
                kind=kind,
                content=content,
                provenance=Provenance(
                    origin=origin,
                    source_ref=str(fact.get("source_ref") or (sources[0][0] if sources else "")),
                    confidence=confidence,
                    consolidator=consolidator,
                    constructor=constructor,
                ),
            )
        )
    return records
