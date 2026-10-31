"""Tests for Phase 4.13: PersistentStore adapter over LangGraph's SqliteStore."""

import math
import random
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from vsigma import (
    FakeLLM,
    InMemoryStore,
    Interceptor,
    MaintainableStore,
    MemoryStore,
    PersistentStore,
    TrajectoryLog,
    make_react_agent,
    say,
)
from vsigma.cli import main as cli_main
from vsigma.memory import _dump_json
from vsigma.stores import _encode


def test_namespace_encoding_dots_and_percents(tmp_path: Path) -> None:
    cases = [
        "simple",
        "user.alice",
        "user%20name",
        "user.alice%20name.with.dots",
        "persona:alice.smith%20/knowledge",
        "percent%25encoded%2Edot",
    ]
    for ns in cases:
        encoded = _encode(ns)
        assert "." not in encoded[1], f"Encoded label contains dot: {encoded}"

    db = tmp_path / "enc.db"
    store = PersistentStore(db)
    for ns in cases:
        store.write(ns, "k1", f"val_{ns}")
        assert store.read(ns, "k1") == f"val_{ns}"
        assert dict(store.query(ns)) == {"k1": f"val_{ns}"}

    assert store.namespaces() == cases
    store.close()


def test_value_fidelity_surrogate_nan_int64_datetime(tmp_path: Path) -> None:
    db = tmp_path / "fidelity.db"
    store = PersistentStore(db)

    # 1. Lone surrogate
    surrogate_str = "hello \ud800 world"
    store.write("ns", "surrogate", surrogate_str)
    assert store.read("ns", "surrogate") == surrogate_str

    # 2. Integer over 64 bits
    big_int = 2**100
    store.write("ns", "big_int", big_int)
    assert store.read("ns", "big_int") == big_int

    # 3. NaN
    store.write("ns", "nan", float("nan"))
    read_nan = store.read("ns", "nan")
    assert isinstance(read_nan, float) and math.isnan(read_nan)

    # 4. datetime
    now = datetime(2026, 10, 31, 12, 30, 45)
    store.write("ns", "dt", now)
    assert store.read("ns", "dt") == str(now)

    store.close()


def test_agent_turn_with_lone_surrogate(tmp_path: Path) -> None:
    db = tmp_path / "agent_surrogate.db"
    store = PersistentStore(db)
    agent = make_react_agent(
        FakeLLM([say("reply with surrogate: \ud800")]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )
    run = agent.run("start", user_id="alice")
    assert run.status == "done"
    store.close()


def test_query_first_write_order_preserved_on_rewrite(tmp_path: Path) -> None:
    db = tmp_path / "order.db"
    store = PersistentStore(db)
    store.write("ns", "k1", "first")
    store.write("ns", "k2", "second")
    store.write("ns", "k3", "third")

    # Update k1 and k2
    store.write("ns", "k1", "first-updated")
    store.write("ns", "k2", "second-updated")

    results = store.query("ns")
    assert [k for k, _ in results] == ["k1", "k2", "k3"]
    assert [v for _, v in results] == ["first-updated", "second-updated", "third"]
    store.close()


def test_namespaces_first_write_order_and_empty_omission(tmp_path: Path) -> None:
    db = tmp_path / "ns_order.db"
    store = PersistentStore(db)
    store.write("ns1", "k", "v1")
    store.write("ns2", "k", "v2")
    store.write("ns3", "k", "v3")

    # Rewriting in ns1 does not change namespace order
    store.write("ns1", "k", "v1-updated")
    assert store.namespaces() == ["ns1", "ns2", "ns3"]

    # Delete all keys in ns2 -> ns2 is omitted
    store.delete("ns2", ["k"])
    assert store.namespaces() == ["ns1", "ns3"]
    assert "ns2" not in store.namespace_sizes()

    # Re-writing to ns2 appends it to first-write order
    store.write("ns2", "k", "v2-new")
    assert store.namespaces() == ["ns1", "ns3", "ns2"]
    store.close()


def test_delete_ignores_duplicate_and_missing_keys(tmp_path: Path) -> None:
    db = tmp_path / "delete.db"
    store = PersistentStore(db)
    store.write("ns", "k1", "val1")
    store.write("ns", "k2", "val2")

    initial_size = store.namespace_sizes()["ns"]
    k1_bytes = _dump_json("val1")[1]

    # Delete with duplicates and nonexistent keys
    store.delete("ns", ["k1", "k1", "missing1", "missing2"])
    assert store.read("ns", "k1") is None
    assert store.read("ns", "k2") == "val2"
    assert store.namespace_sizes()["ns"] == initial_size - k1_bytes

    # Delete with all nonexistent keys is a no-op
    store.delete("ns", ["missing1", "missing2"])
    assert store.namespace_sizes()["ns"] == initial_size - k1_bytes
    store.close()


def test_namespace_sizes_matches_recount_concurrent(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    store = PersistentStore(db)
    errors: list[Exception] = []

    def worker(tid: int) -> None:
        try:
            for i in range(30):
                ns = f"ns_{random.randint(1, 4)}"
                k = f"k_{random.randint(1, 10)}"
                if random.random() < 0.7:
                    store.write(ns, k, {"worker": tid, "seq": i, "payload": "x" * 10})
                else:
                    store.delete(ns, [k])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Worker threads encountered errors: {errors}"

    sizes = store.namespace_sizes()
    for ns in store.namespaces():
        items = store.query(ns)
        recount = sum(_dump_json(v)[1] for _, v in items)
        assert sizes[ns] == recount, f"Size mismatch for {ns}: {sizes[ns]} != {recount}"

    store.close()


def test_multi_process_concurrency(tmp_path: Path) -> None:
    db = tmp_path / "multiproc.db"
    PersistentStore(db).close()

    script = """
import sys
from vsigma.stores import PersistentStore

pid = int(sys.argv[1])
db = sys.argv[2]
store = PersistentStore(db)
sys.stdout.write("READY\\n")
sys.stdout.flush()
sys.stdin.readline()

for i in range(500):
    store.write("shared_ns", f"k_{pid}_{i}", {"pid": pid, "i": i, "payload": "x" * 20})

for i in range(0, 500, 5):
    store.delete("shared_ns", [f"k_{pid}_{i}"])

store.close()
"""
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(p), str(db)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for p in range(2)
    ]
    for p in procs:
        assert p.stdout is not None
        assert p.stdout.readline().strip() == "READY"

    for p in procs:
        assert p.stdin is not None
        p.stdin.write("GO\n")
        p.stdin.flush()

    exit_codes = [p.wait() for p in procs]
    assert exit_codes == [0, 0]

    store = PersistentStore(db)
    items = store.query("shared_ns")
    assert len(items) == 800
    sizes = store.namespace_sizes()
    recount = sum(_dump_json(v)[1] for _, v in items)
    assert sizes["shared_ns"] == recount

    with store._transaction("BEGIN"):
        meta = store._store.get(("n",), "shared_ns")
    assert meta is not None
    assert meta.value["rows"] == 800
    store.close()


def test_maintainable_store_protocol(tmp_path: Path) -> None:
    ps = PersistentStore(tmp_path / "p.db")
    im = InMemoryStore()

    assert isinstance(ps, MaintainableStore)
    assert isinstance(ps, MemoryStore)
    assert isinstance(im, MaintainableStore)
    assert isinstance(im, MemoryStore)

    # Test InMemoryStore MaintainableStore methods
    im.write("ns1", "k1", "val1")
    im.write("ns2", "k2", "val2")
    assert im.namespaces() == ["ns1", "ns2"]
    assert im.namespace_sizes() == {
        "ns1": _dump_json("val1")[1],
        "ns2": _dump_json("val2")[1],
    }

    im.delete("ns1", ["k1", "k1", "nonexistent"])
    assert im.namespaces() == ["ns2"]
    assert "ns1" not in im.namespace_sizes()

    # Concurrent namespace snapshot check on InMemoryStore
    errors: list[Exception] = []
    stop_event = threading.Event()

    def modifier() -> None:
        i = 0
        while not stop_event.is_set():
            im.write(f"dyn_{i % 5}", f"k_{i}", "val")
            if i % 3 == 0:
                im.delete(f"dyn_{i % 5}", [f"k_{i}"])
            i += 1
            time.sleep(0.001)

    def reader() -> None:
        try:
            for _ in range(50):
                im.namespaces()
                im.namespace_sizes()
                time.sleep(0.001)
        except Exception as exc:
            errors.append(exc)

    t_mod = threading.Thread(target=modifier)
    t_read = threading.Thread(target=reader)
    t_mod.start()
    t_read.start()
    t_read.join()
    stop_event.set()
    t_mod.join()

    assert not errors, f"InMemoryStore raised during concurrent iteration: {errors}"
    ps.close()


def test_old_file_rejection_byte_identical_wal(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    wal = tmp_path / "old.db-wal"
    script = f"""
import sqlite3, os
conn = sqlite3.connect(r"{db}")
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute(
    "CREATE TABLE memory (namespace TEXT, key TEXT, value TEXT, PRIMARY KEY (namespace, key));"
)
conn.execute("INSERT INTO memory VALUES ('ns', 'k1', 'val1');")
conn.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script], check=True)
    assert db.exists()
    assert wal.exists()
    db_before = db.read_bytes()
    wal_before = wal.read_bytes()

    with pytest.raises(RuntimeError) as exc_info:
        PersistentStore(db)

    assert "table 'memory'" in str(exc_info.value)
    assert "pre-v0.3.0" in str(exc_info.value)

    db_after = db.read_bytes()
    wal_after = wal.read_bytes()
    assert db_before == db_after
    assert wal_before == wal_after


def test_old_file_uri_escaping(tmp_path: Path) -> None:
    # Test path containing # and ?
    for name in ("old#store.db", "old?store.db"):
        db = tmp_path / name
        script = f"""
import sqlite3
conn = sqlite3.connect(r"{db}")
conn.execute("CREATE TABLE memory (k TEXT, v TEXT);")
conn.execute("INSERT INTO memory VALUES ('1', '2');")
conn.commit()
conn.close()
"""
        subprocess.run([sys.executable, "-c", script], check=True)
        # Should raise on old file and NOT create a stray file like 'old'
        with pytest.raises(RuntimeError):
            PersistentStore(db)
        assert not (tmp_path / "old").exists()

    # Test path containing %41
    db_pct = tmp_path / "test%41.db"
    s1 = PersistentStore(db_pct)
    s1.write("ns", "k1", "val1")
    s1.close()

    # Second open must not raise OperationalError
    s2 = PersistentStore(db_pct)
    assert s2.read("ns", "k1") == "val1"
    s2.close()


def test_batch_rollback_on_meta_update_failure(tmp_path: Path) -> None:
    db = tmp_path / "rollback_update.db"
    store = PersistentStore(db)
    store.write("test_ns", "k1", "val1")
    store.write("test_ns", "k2", "val2")

    # Trigger to fail when inserting/updating 'n' prefix
    store._conn.execute("""
    CREATE TRIGGER fail_meta_insert BEFORE INSERT ON store WHEN NEW.prefix = 'n'
    BEGIN
        SELECT RAISE(ABORT, 'injected meta update failure');
    END;
    """)

    with pytest.raises(Exception, match="injected meta update failure"):
        store.delete("test_ns", ["k1"])

    # k1 and k2 must both still exist after rollback
    assert store.read("test_ns", "k1") == "val1"
    assert store.read("test_ns", "k2") == "val2"
    assert dict(store.query("test_ns")) == {"k1": "val1", "k2": "val2"}
    store.close()


def test_batch_rollback_on_meta_delete_failure(tmp_path: Path) -> None:
    db = tmp_path / "rollback_delete.db"
    store = PersistentStore(db)
    store.write("test_ns", "k1", "val1")

    # Trigger to fail when deleting 'n' prefix
    store._conn.execute("""
    CREATE TRIGGER fail_meta_delete BEFORE DELETE ON store WHEN OLD.prefix = 'n'
    BEGIN
        SELECT RAISE(ABORT, 'injected meta delete failure');
    END;
    """)

    with pytest.raises(Exception, match="injected meta delete failure"):
        # Deleting k1 empties namespace, which attempts DELETE on ('n', 'test_ns')
        store.delete("test_ns", ["k1"])

    # k1 and test_ns must still exist
    assert store.read("test_ns", "k1") == "val1"
    assert "test_ns" in store.namespaces()
    store.close()


def test_failed_commit_and_raise_rollback_trigger(tmp_path: Path) -> None:
    db = tmp_path / "rollback_recovery.db"
    store = PersistentStore(db)

    # 1. Trigger with RAISE(ROLLBACK)
    store._conn.execute("""
    CREATE TRIGGER fail_auto_rollback BEFORE INSERT ON store WHEN NEW.key = 'fail_key'
    BEGIN
        SELECT RAISE(ROLLBACK, 'injected raise rollback trigger');
    END;
    """)

    with pytest.raises(Exception, match="injected raise rollback trigger"):
        store.write("ns", "fail_key", "value")

    # Store must not be broken; subsequent write/read succeeds
    store.write("ns", "ok_key", "ok_value")
    assert store.read("ns", "ok_key") == "ok_value"

    # 2. Deferred foreign key violation causing COMMIT failure
    store._conn.execute("PRAGMA foreign_keys = ON;")
    store._conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY);")
    store._conn.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, "
        "parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);"
    )

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction("BEGIN IMMEDIATE"):
            store._conn.execute("INSERT INTO child VALUES (1, 999);")

    # Store must not be broken after failed COMMIT
    store.write("ns", "after_commit_fail", "good")
    assert store.read("ns", "after_commit_fail") == "good"
    store.close()


def test_subprocess_kill_mid_write_consistency(tmp_path: Path) -> None:
    db = tmp_path / "kill.db"
    script = f"""
import sys
from vsigma.stores import PersistentStore
store = PersistentStore(r"{db}")
for i in range(100):
    store.write("ns", f"k{{i}}", {{"index": i, "payload": "data"}})
    if i == 25:
        print("READY", flush=True)
for i in range(100, 10000):
    store.write("ns", f"k{{i}}", {{"index": i, "payload": "data"}})
store.close()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready_line = proc.stdout.readline()
    assert ready_line.strip() == "READY"
    proc.kill()
    proc.wait()

    # Reopening the store should recover cleanly and maintain consistency
    store = PersistentStore(db)
    items = store.query("ns")
    assert len(items) >= 25
    sizes = store.namespace_sizes()
    recount = sum(_dump_json(v)[1] for _, v in items)
    assert sizes["ns"] == recount
    store.close()


def test_cli_export_on_old_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memory (k text);")
    conn.execute("INSERT INTO memory VALUES ('1');")
    conn.commit()
    conn.close()

    ret = cli_main(["memory", "export", "--db", str(db)])
    assert ret == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "pre-v0.3.0 data in table 'memory'" in err
