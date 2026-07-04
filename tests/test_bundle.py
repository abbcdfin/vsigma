"""Construction engine + bundle contract: the end-to-end §D slice.

docs -> distill (constrained LLM output) -> bundle -> validate -> ingest ->
recall — all offline, plus the CLI gate and the offline-constructor path
(a bundle written with no vsigma construction code, as an external agent would).
"""

import json
from pathlib import Path

from vsigma import (
    FakeLLM,
    InMemoryStore,
    SQLiteStore,
    State,
    ingest_bundle,
    persona,
    recall,
    say,
    validate_bundle,
    write_bundle,
)
from vsigma.cli import main as cli_main
from vsigma.construction import distill

_DOCS = [
    ("docs/team.md", "Alice leads the data platform team. She prefers concise reports."),
    ("docs/tools.md", "Deploys go through the `ship` command."),
]

# The constructor LLM obeys the constrained shape: one JSON object per line.
_DISTILLED = "\n".join(
    json.dumps({"content": content, "source_ref": ref})
    for content, ref in [
        ("Alice leads the data platform team.", "docs/team.md"),
        ("Deploys go through the `ship` command.", "docs/tools.md"),
    ]
)


def _make_bundle(path: Path) -> Path:
    records = distill(
        _DOCS,
        FakeLLM([say(_DISTILLED)]),
        scope=persona("alice"),
        constructor="fake-llm/test",
    )
    return write_bundle(path / "bundle", records, constructor="fake-llm/test", sources=_DOCS)


def test_end_to_end_docs_to_recall(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)

    assert validate_bundle(bundle) == []

    store = InMemoryStore()
    count = ingest_bundle(bundle, store)
    assert count == 2

    # The runtime reads what construction wrote — same interface, and the
    # visibility rule applies (serving alice sees it; serving bob does not).
    serving_alice: State = {"user_id": "alice"}
    contents = [str(v["content"]) for _, _, v in recall(store, serving_alice, kind="knowledge")]  # type: ignore[index]
    assert "Alice leads the data platform team." in contents
    serving_bob: State = {"user_id": "bob"}
    assert recall(store, serving_bob, kind="knowledge") == []


def test_distill_rejects_unconstrained_output() -> None:
    chatty = FakeLLM([say("Sure! Here are some facts:\n- Alice leads the team")])
    try:
        distill(_DOCS, chatty, scope=persona("alice"))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "output shape" in str(exc)


def test_validate_catches_gate_failures(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)

    # Corrupt one record: bad kind + a duplicate id.
    records_file = bundle / "records.jsonl"
    lines = records_file.read_text().splitlines()
    bad = json.loads(lines[0])
    bad["kind"] = "gossip"
    records_file.write_text("\n".join([lines[0], lines[1], json.dumps(bad)]) + "\n")

    errors = validate_bundle(bundle)
    assert any("kind" in e for e in errors)
    assert any("duplicate record id" in e for e in errors)

    # A failed gate must not touch the store.
    store = InMemoryStore()
    try:
        ingest_bundle(bundle, store)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert recall(store, {"user_id": "alice"}) == []


def test_validate_unknown_schema_version(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["schema_version"] = "99.0"
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    assert any("schema_version" in e for e in validate_bundle(bundle))


def test_offline_constructor_needs_no_vsigma(tmp_path: Path) -> None:
    # An external agent writes the files by hand from the published schema —
    # no vsigma construction code involved. The gate must accept it.
    bundle = tmp_path / "handmade"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps({"schema_version": "0.1", "constructor": "claude-code/2.0", "sources": []})
    )
    record = {
        "id": "fact-1",
        "scope": "global",
        "kind": "knowledge",
        "content": "The org standardizes on uv for Python tooling.",
        "provenance": {"origin": "injected", "source_ref": "handbook.md#tooling"},
    }
    (bundle / "records.jsonl").write_text(json.dumps(record) + "\n")

    assert validate_bundle(bundle) == []
    store = InMemoryStore()
    assert ingest_bundle(bundle, store) == 1
    assert store.read("global/knowledge", "fact-1") is not None


def test_published_schema_matches_code() -> None:
    # The published JSON Schema (the external-constructor contract) must not
    # drift from the code's validator.
    from vsigma import SCHEMA_VERSION
    from vsigma.records import CONSOLIDATORS, ORIGINS
    from vsigma.scope import KINDS, SCOPE_LEVELS

    schema = json.loads(
        (Path(__file__).parent.parent / "docs/schemas/memory-bundle.schema.json").read_text()
    )
    defs = schema["$defs"]
    assert defs["manifest"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert tuple(defs["record"]["properties"]["kind"]["enum"]) == KINDS
    assert tuple(defs["provenance"]["properties"]["origin"]["enum"]) == ORIGINS
    scope_pattern = defs["record"]["properties"]["scope"]["pattern"]
    for level in SCOPE_LEVELS:
        assert level in scope_pattern
    assert tuple(defs["provenance"]["then"]["properties"]["consolidator"]["enum"]) == (
        CONSOLIDATORS
    )


def test_cli_validate_and_ingest(tmp_path: Path, capsys: object) -> None:
    bundle = _make_bundle(tmp_path)
    db = tmp_path / "memory.db"

    assert cli_main(["memory", "validate", str(bundle)]) == 0
    assert cli_main(["memory", "ingest", str(bundle), "--db", str(db)]) == 0

    store = SQLiteStore(db)
    assert len(store.query("persona:alice/knowledge")) == 2
    store.close()

    assert cli_main(["memory", "validate", str(tmp_path / "nowhere")]) == 1
