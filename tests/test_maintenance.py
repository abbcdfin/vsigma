"""Maintenance pass (docs/proposals/maintenance-pass.md §B/§C)."""

import json
from datetime import datetime
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
    session,
    unconsolidated_episodes,
)
from vsigma.cli import main
from vsigma.episodes import EPISODES_NS
from vsigma.maintenance import MAINTENANCE_NS, parse_scope, parse_source

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
    assert len(unconsolidated_episodes(store, sources=None)) == 2

    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        sources=None,
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )

    assert len(report.episodes) == 2 and report.records == 1 and report.ingested
    [(_, value)] = store.query(AGENT.namespace("knowledge"))
    assert isinstance(value, dict) and value["content"] == _FACT
    markers = [k for k, _ in store.query(MAINTENANCE_NS)]
    assert len(markers) == 2 and all(k.startswith("deep:") for k in markers)
    assert unconsolidated_episodes(store, sources=None) == []

    # Second invocation: nothing to do — the cursor held.
    idle = run_maintenance(
        store,
        FakeLLM([]),
        sources=None,
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path,
    )
    assert idle.episodes == [] and not idle.ingested


def test_dry_run_writes_bundle_but_commits_nothing(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "b.db", ["task"])

    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        sources=None,
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
        dry_run=True,
    )

    assert report.bundle is not None and not report.ingested
    assert (Path(report.bundle) / "records.jsonl").is_file()  # reviewable artifact
    assert store.query(AGENT.namespace("knowledge")) == []  # nothing committed
    assert len(unconsolidated_episodes(store, sources=None)) == 1  # markers untouched


def test_failed_distill_leaves_markers_untouched_and_retry_succeeds(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "c.db", ["task"])

    with pytest.raises(ValueError):
        run_maintenance(
            store,
            FakeLLM([say("here's a summary, not JSON")]),
            sources=None,
            scope=AGENT,
            constructor="capable/deep",
            out_dir=tmp_path / "bundles",
        )
    assert len(unconsolidated_episodes(store, sources=None)) == 1  # nothing advanced

    retry = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        sources=None,
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )
    assert retry.ingested and unconsolidated_episodes(store, sources=None) == []


def test_zero_record_batch_is_marked_without_a_bundle(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "d.db", ["task"])

    report = run_maintenance(
        store,
        FakeLLM([say("")]),  # valid pass, nothing worth keeping
        sources=None,
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )

    assert report.records == 0 and report.bundle is None and not report.ingested
    assert unconsolidated_episodes(store, sources=None) == []  # processed, won't retry forever


def test_limit_bounds_the_batch(tmp_path: Path) -> None:
    store = _store_with_episodes(tmp_path / "e.db", ["one", "two", "three"])
    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        sources=None,
        scope=AGENT,
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
        limit=2,
    )
    assert len(report.episodes) == 2
    assert len(unconsolidated_episodes(store, sources=None)) == 1  # the rest waits its turn


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

    assert (
        main(
            ["memory", "consolidate", "--db", str(tmp_path / "g.db"),
             "--all-sources", "--scope", "agent"]
        )
        == 1
    )
    assert "no LLM configured" in capsys.readouterr().err


def test_cli_consolidate_dry_run_prints_the_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "h.db"
    _store_with_episodes(db, ["task"]).close()
    monkeypatch.setattr("vsigma.cli.default_llm", lambda: FakeLLM([say(_FACT_LINE)]))

    code = main(
        ["memory", "consolidate", "--db", str(db), "--dry-run",
         "--all-sources", "--scope", "agent", "--out", str(tmp_path / "out")]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run: 1 episode(s) -> 1 record(s)" in out and _FACT in out

    # And the store really was untouched.
    store = SQLiteStore(db)
    assert store.query(AGENT.namespace("knowledge")) == []
    assert len(unconsolidated_episodes(store, sources=None)) == 1
    store.close()


def _tenant_store(db: Path) -> tuple[SQLiteStore, str, str]:
    """One store, two tenants — the shape `--scope` alone cannot tell apart."""
    store = SQLiteStore(db)
    svc = Services(memory=store, observer=TrajectoryLog())

    def run(session_id: str, user_id: str) -> str:
        agent = make_react_agent(FakeLLM([say("done")]), [], services=svc)
        return agent.run("task", session_id=session_id, user_id=user_id).run_id

    return store, run("a", "alice"), run("b", "bob")


def test_a_source_bounds_which_tenants_episodes_are_read(tmp_path: Path) -> None:
    """§A.5: `--scope` names the destination, so before this a single pass
    distilled every tenant's episodes into one persona's scope."""
    store, run_a, run_b = _tenant_store(tmp_path / "t.db")

    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        sources=[session("a")],
        scope=persona("jian"),
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )

    assert report.episodes == [run_a]  # session b never read
    markers = [key for key, _ in store.query(MAINTENANCE_NS)]
    assert markers == [f"deep:{run_a}"]  # and never marked
    assert unconsolidated_episodes(store, sources=[session("b")]) == [run_b]


def test_a_persona_source_matches_the_owning_persona(tmp_path: Path) -> None:
    store, _, run_b = _tenant_store(tmp_path / "p.db")
    assert unconsolidated_episodes(store, sources=[persona("bob")]) == [run_b]


def test_unindexed_episodes_match_only_all_sources(tmp_path: Path) -> None:
    """Ownership that cannot be established fails closed."""
    store, run_a, _ = _tenant_store(tmp_path / "u.db")
    store.write(EPISODES_NS, run_a, None)  # simulate a pre-index episode

    assert run_a not in unconsolidated_episodes(store, sources=[session("a")])
    assert run_a not in unconsolidated_episodes(store, sources=[persona("alice")])
    assert run_a in unconsolidated_episodes(store, sources=None)


def test_cli_requires_an_explicit_ownership_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "c.db"
    _store_with_episodes(db, ["task"]).close()
    monkeypatch.setattr("vsigma.cli.default_llm", lambda: FakeLLM([say(_FACT_LINE)]))
    base = ["memory", "consolidate", "--db", str(db), "--scope", "agent"]

    assert main(base) == 1  # neither
    assert "ownership boundary" in capsys.readouterr().err
    assert main([*base, "--source", "session:a", "--all-sources"]) == 1  # both
    assert "ownership boundary" in capsys.readouterr().err


def test_parse_source_rejects_non_ownership_scopes() -> None:
    assert parse_source("session:a") == session("a")
    assert parse_source("persona:alice") == persona("alice")
    for bad in ("agent", "global", "episode:r1", "session"):
        with pytest.raises(ValueError):
            parse_source(bad)


def test_bundle_manifest_declares_the_trajectories_it_read(tmp_path: Path) -> None:
    """§B.4: an unaudited bundle cannot be checked against the boundary that
    produced it."""
    store, run_a, _ = _tenant_store(tmp_path / "m.db")

    report = run_maintenance(
        store,
        FakeLLM([say(_FACT_LINE)]),
        sources=[session("a")],
        scope=session("a"),
        constructor="capable/deep",
        out_dir=tmp_path / "bundles",
    )

    assert report.bundle is not None
    manifest = json.loads((Path(report.bundle) / "manifest.json").read_text())
    assert [source["ref"] for source in manifest["sources"]] == [f"trajectory:{run_a}"]
    assert all(len(source["sha256"]) == 64 for source in manifest["sources"])


def test_two_passes_in_one_second_get_distinct_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle names carried only second precision, and `write_bundle` mkdirs with
    `exist_ok` and overwrites both files. A collision is silent *and* wrong: each
    batch's `deep:` markers keep pointing at that path, so one batch's markers end
    up citing the other batch's manifest as evidence of what it read."""

    class _FrozenClock:
        @staticmethod
        def now() -> datetime:
            return datetime(2026, 9, 9, 12, 0, 0)

    monkeypatch.setattr("vsigma.maintenance.datetime", _FrozenClock)
    store, run_a, run_b = _tenant_store(tmp_path / "x.db")
    out = tmp_path / "bundles"

    def one_pass(source: str) -> str:
        report = run_maintenance(
            store,
            FakeLLM([say(json.dumps({"content": f"fact {source}", "source_ref": "t"}))]),
            sources=[session(source)],
            scope=session(source),
            constructor="capable/deep",
            out_dir=out,
        )
        assert report.bundle is not None
        return report.bundle

    first, second = one_pass("a"), one_pass("b")

    assert first != second
    for bundle, run_id in ((first, run_a), (second, run_b)):
        manifest = json.loads((Path(bundle) / "manifest.json").read_text())
        assert [source["ref"] for source in manifest["sources"]] == [f"trajectory:{run_id}"]

    # And every marker cites the bundle that actually read that episode.
    markers = dict(store.query(MAINTENANCE_NS))
    assert markers[f"deep:{run_a}"]["bundle"] == first  # type: ignore[index]
    assert markers[f"deep:{run_b}"]["bundle"] == second  # type: ignore[index]
