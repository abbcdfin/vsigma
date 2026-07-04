"""Record shape (provenance) round-trips + the SQLite first-party store."""

from pathlib import Path

import pytest

from vsigma import (
    MemoryRecord,
    Provenance,
    SQLiteStore,
    persona,
    record_from_dict,
)
from vsigma.records import record_errors

_RECORD = MemoryRecord(
    id="fact-1",
    scope=persona("alice").head,
    kind="knowledge",
    content="Alice prefers concise answers.",
    provenance=Provenance(
        origin="injected", source_ref="docs/onboarding.md#L12", constructor="claude-code/1.0"
    ),
)


def test_record_dict_round_trip() -> None:
    raw = _RECORD.to_dict()
    assert record_from_dict(raw) == _RECORD
    assert _RECORD.namespace == "persona:alice/knowledge"


def test_record_validation_catches_defects() -> None:
    raw = _RECORD.to_dict()
    raw["kind"] = "gossip"
    raw["scope"] = "universe:42"
    del raw["provenance"]
    errors = record_errors(raw)
    assert any("kind" in e for e in errors)
    assert any("scope level" in e for e in errors)
    assert any("provenance" in e for e in errors)
    with pytest.raises(ValueError):
        record_from_dict(raw)


def test_consolidated_records_require_consolidator() -> None:
    raw = _RECORD.to_dict()
    prov = dict(raw["provenance"])  # type: ignore[call-overload]
    prov["origin"] = "consolidated"
    prov["consolidator"] = None
    raw["provenance"] = prov
    assert any("consolidator" in e for e in record_errors(raw))
    prov["consolidator"] = "reflexive"
    assert record_errors(raw) == []


def test_sqlite_store_round_trip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory.db")
    store.write("ns", "k1", {"a": 1})
    store.write("ns", "k2", "value")
    store.write("ns", "k1", {"a": 2})  # upsert

    assert store.read("ns", "k1") == {"a": 2}
    assert store.read("ns", "missing") is None
    assert dict(store.query("ns")) == {"k1": {"a": 2}, "k2": "value"}
    assert store.query("other") == []


def test_sqlite_store_backs_a_live_agent(tmp_path: Path) -> None:
    # Backend swap is config, not code: the template runs unchanged on SQLite.
    from vsigma import FakeLLM, Services, TrajectoryLog, episode, make_react_agent, say

    store = SQLiteStore(tmp_path / "agent.db")
    agent = make_react_agent(
        FakeLLM([say("ok")]), [], services=Services(memory=store, observer=TrajectoryLog())
    )
    run = agent.run("hello", user_id="alice")

    assert run.status == "done"
    assert store.query(episode(run.run_id).namespace("context"))  # seam wrote via SQLite
    store.close()


def test_sqlite_store_persists_across_reconnect(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    first = SQLiteStore(db)
    first.write(_RECORD.namespace, _RECORD.id, _RECORD.to_dict())
    first.close()

    second = SQLiteStore(db)
    stored = second.read(_RECORD.namespace, _RECORD.id)
    assert isinstance(stored, dict)
    assert record_from_dict(stored) == _RECORD
    second.close()
