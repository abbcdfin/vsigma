"""Maintenance pass (docs/proposals/maintenance-pass.md §B/§C)."""

import json
from pathlib import Path

import pytest

from vsigma import (
    AGENT,
    FakeLLM,
    Services,
    SQLiteStore,
    TrajectoryLog,
    make_react_agent,
    persona,
    run_maintenance,
    say,
    unconsolidated_episodes,
)
from vsigma.cli import main
from vsigma.maintenance import MAINTENANCE_NS, parse_scope

_FACT = "The upload endpoint requires a session cookie."
_FACT_LINE = json.dumps({"content": _FACT, "source_ref": "trajectory:r1"})


def _store_with_episodes(db: Path, tasks: list[str]) -> SQLiteStore:
    store = SQLiteStore(db)
    svc = Services(memory=store, observer=TrajectoryLog())
    for task in tasks:
        make_react_agent(FakeLLM([say("done")]), [], services=svc).run(task)
    return store


def test_consolidate_ingests_and_advances_markers(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "a.db", ["task one", "task two"])
    assert len(unconsolidated_episodes(store)) == 2

    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )

    assert len(report.episodes) == 2 and report.records == 1 and report.ingested
    [(_, value)] = store.query(AGENT.namespace("knowledge"))
    assert isinstance(value, dict) and value["content"] == _FACT
    markers = [k for k, _ in store.query(MAINTENANCE_NS)]
    assert len(markers) == 2 and all(k.startswith("deep:") for k in markers)
    assert unconsolidated_episodes(store) == []

    # Second invocation: nothing to do — the cursor held.
    idle = run_maintenance(
        store, FakeLLM([]), scope=AGENT, constructor="capable/deep", out_dir=tmp_path
    )
    assert idle.episodes == [] and not idle.ingested


def test_dry_run_writes_bundle_but_commits_nothing(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "b.db", ["task"])

    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
        dry_run=True,
    )

    assert report.bundle is not None and not report.ingested
    assert (Path(report.bundle) / "records.jsonl").is_file()  # reviewable artifact
    assert store.query(AGENT.namespace("knowledge")) == []  # nothing committed
    assert len(unconsolidated_episodes(store)) == 1  # markers untouched


def test_failed_distill_leaves_markers_untouched_and_retry_succeeds(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "c.db", ["task"])

    with pytest.raises(ValueError):
        run_maintenance(
            store,
            FakeLLM([say("here's a summary, not JSON")]),
            scope=AGENT,
            constructor="capable/deep",
            out_dir=tmp_path / "bundles",
        )
    assert len(unconsolidated_episodes(store)) == 1  # nothing advanced

    retry = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )
    assert retry.ingested and unconsolidated_episodes(store) == []


def test_zero_record_batch_is_marked_without_a_bundle(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "d.db", ["task"])

    report = run_maintenance(
        store,
        FakeLLM([say("")]),  # valid pass, nothing worth keeping
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )

    assert report.records == 0 and report.bundle is None and not report.ingested
    assert unconsolidated_episodes(store) == []  # processed, won't retry forever


def test_limit_bounds_the_batch(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "e.db", ["one", "two", "three"])
    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
        limit=2,
    )
    assert len(report.episodes) == 2
    assert len(unconsolidated_episodes(store)) == 1  # the rest waits its turn


def test_parse_scope() -> None:
    assert parse_scope("agent") is AGENT
    assert parse_scope("persona:alice") == persona("alice")
    with pytest.raises(ValueError):
        parse_scope("persona")  # missing id
    with pytest.raises(ValueError):
        parse_scope("episode:r1")  # not a maintenance target


def test_cli_export_dumps_json_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "f.db"
    store = _store_with_episodes(db, ["task"])
    store.write("agent/knowledge", "k1", {"content": "a fact"})
    store.close()

    assert main(["memory", "export", "--db", str(db), "--namespace", "agent/knowledge"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{"namespace": "agent/knowledge", "key": "k1", "value": {"content": "a fact"}}]


def test_cli_consolidate_refuses_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VSIGMA_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("VSIGMA_LLM_API_KEY", raising=False)
    _store_with_episodes(tmp_path / "g.db", ["task"]).close()

    assert main(["memory", "consolidate", "--db", str(tmp_path / "g.db")]) == 1
    assert "no LLM configured" in capsys.readouterr().err


def test_cli_consolidate_dry_run_prints_the_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "h.db"
    _store_with_episodes(db, ["task"]).close()
    monkeypatch.setattr("vsigma.cli.default_llm", lambda: FakeLLM([say(_FACT_LINE)]))

    code = main(
        ["memory", "consolidate", "--db", str(db), "--dry-run", "--out", str(tmp_path / "out")]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run: 1 episode(s) -> 1 record(s)" in out and _FACT in out

    # And the store really was untouched.
    store = SQLiteStore(db)
    assert store.query(AGENT.namespace("knowledge")) == []
    assert len(unconsolidated_episodes(store)) == 1
    store.close()
