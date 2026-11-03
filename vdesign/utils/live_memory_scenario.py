"""Real-LLM check of the memory loop: run-end distill, maintain, recall, isolation, forget.

Needs `VSIGMA_LLM_BASE_URL`, `VSIGMA_LLM_API_KEY` and `VSIGMA_LLM_MODEL`. Thinking is turned off
with `chat_template_kwargs` (vLLM, llama.cpp); set `VSIGMA_LLM_EXTRA_BODY` for another
provider's switch. About ten short calls. Run from the repository root:

    set -a; . <env file>; set +a
    uv run python vdesign/utils/live_memory_scenario.py

Stores go in a fresh temporary directory, printed at the start.
"""

import json
import os
import sqlite3
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from vsigma import (
    Interceptor,
    LLMResponse,
    OpenAICompatibleLLM,
    PersistentStore,
    RecallContext,
    SessionDenied,
    Tool,
    make_react_agent,
    reflexive_consolidator,
)

WORK = Path(tempfile.mkdtemp(prefix="vsigma-live-"))
print(f"stores in {WORK}")

EXTRA = os.environ.get("VSIGMA_LLM_EXTRA_BODY")
BASE: dict[str, Any] = {
    "model": os.environ["VSIGMA_LLM_MODEL"],
    "base_url": os.environ["VSIGMA_LLM_BASE_URL"],
    "api_key": os.environ["VSIGMA_LLM_API_KEY"],
    "timeout": float(os.environ.get("VSIGMA_LLM_TIMEOUT", "600")),
    "max_tokens": 1024,
    "extra_body": json.loads(EXTRA)
    if EXTRA
    else {"chat_template_kwargs": {"enable_thinking": False}},
}


class Counting:
    """Counts an LLM's calls and keeps what each one saw."""

    def __init__(self) -> None:
        self.inner = OpenAICompatibleLLM(**BASE)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> LLMResponse:
        start = time.time()
        r = self.inner.complete(messages, tools)
        self.calls.append(
            {"sec": time.time() - start, "messages": messages,
             "tools": [c.name for c in r.tool_calls]}
        )
        return r


actor, hook_llm, deep_llm = Counting(), Counting(), Counting()

order_tool = Tool(
    name="order_status",
    description="Look up the shipping status of an order by its id.",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"]},
    func=lambda a: (
        f"Order {a.get('order_id')}: packed at warehouse WH-42, awaiting carrier pickup."
    ),
)

store = PersistentStore(str(WORK / "agent.db"))
saver = SqliteSaver(sqlite3.connect(str(WORK / "threads.db"), check_same_thread=False))
agent = make_react_agent(
    actor, [order_tool], checkpointer=saver,
    interceptor=Interceptor(memory=store, run_end=reflexive_consolidator(hook_llm),
                            context=RecallContext(max_facts=8)),
)

results: list[tuple[str, bool]] = []


def check(name: str, ok: object, detail: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}", flush=True)


def knowledge(prefix: str) -> list[tuple[str, object, str]]:
    """(namespace, confidence, content) for each knowledge record under `prefix`."""
    out = []
    for ns in store.namespaces():
        if ns.startswith(prefix) and ns.endswith("/knowledge"):
            for _, value in store.query(ns):
                record = value if isinstance(value, dict) else {"content": value}
                prov = record.get("provenance")
                conf = prov.get("confidence") if isinstance(prov, dict) else None
                out.append((ns, conf, str(record.get("content"))))
    return out


def show(rows: list[tuple[str, object, str]]) -> None:
    for ns, conf, content in rows:
        print(f"      {ns} conf={conf}: {content[:110]}")


def injected(call: dict[str, Any]) -> str:
    return " ".join(
        str(m.get("content", "")) for m in call["messages"] if m.get("role") == "system"
    )


def step(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)


def first_session() -> None:
    s1 = agent.session("alice:s1", user_id="alice")
    r1 = s1.send("Hi. For all my shipments I always use DHL Express, and my warehouse code "
                 "is WH-42. Please keep that in mind.")
    check("s1 turn 1 finishes", r1.status == "done", f"answer: {str(r1.result)[:90]!r}")
    n = len(actor.calls)
    r2 = s1.send("What's the status of order 1001?")
    used = [t for c in actor.calls[n:] for t in c["tools"]]
    check("s1 turn 2 calls the tool", "order_status" in used,
          f"answer: {str(r2.result)[:90]!r}")
    rows = knowledge("session:alice:s1")
    check("run-end distill wrote session knowledge", rows, f"{len(rows)} records")
    show(rows)


def maintenance() -> None:
    rep = agent.maintain(distiller=deep_llm).memory
    assert rep is not None
    print(f"      memory pass: episodes={len(rep.episodes)} records={rep.records} "
          f"ingested={rep.ingested} unowned={rep.unowned} calls={len(deep_llm.calls)}")
    rows = knowledge("persona:alice")
    check("maintain wrote persona knowledge", rep.ingested and rows, f"{len(rows)} records")
    show(rows)
    stored = " ".join(c for _, _, c in knowledge(""))
    check("memory holds the carrier and warehouse code", "DHL" in stored and "WH-42" in stored)
    # Persona memory reaches every later session: the order's status at the time ("is
    # awaiting pickup") misleads them, while a past event ("was packed") stays true.
    status = [c for _, _, c in knowledge("persona:") if "1001" in c and " was " not in c]
    check("persona memory holds no current order status", not status,
          f"{len(status)} records state order 1001's status")
    calls = len(deep_llm.calls)
    rep2 = agent.maintain(distiller=deep_llm).memory
    assert rep2 is not None
    check("second maintain is idle", not rep2.episodes and len(deep_llm.calls) == calls,
          f"episodes={len(rep2.episodes)} new calls={len(deep_llm.calls) - calls}")


QUESTION = "Which carrier and warehouse code should you use for my next shipment? Answer briefly."


def recall_alice() -> None:
    n = len(actor.calls)
    r = agent.session("alice:s2", user_id="alice").send(QUESTION)
    sys_text = injected(actor.calls[n])
    check("alice:s2 prompt carries her memory", "DHL" in sys_text and "WH-42" in sys_text)
    ans = str(r.result)
    check("alice:s2 answer uses it", "DHL" in ans and "WH-42" in ans, f"answer: {ans[:110]!r}")


def isolate_bob() -> None:
    n = len(actor.calls)
    r = agent.session("bob:s1", user_id="bob").send(QUESTION)
    sys_text = injected(actor.calls[n])
    check("bob's prompt has none of Alice's memory",
          "DHL" not in sys_text and "WH-42" not in sys_text)
    check("bob's answer doesn't know it", "WH-42" not in str(r.result),
          f"answer: {str(r.result)[:110]!r}")
    try:
        _ = agent.session("alice:s1", user_id="bob").dialog
        check("bob refused on alice:s1", False)
    except SessionDenied:
        check("bob refused on alice:s1", True)


def forget() -> None:
    agent.session("alice:s1", user_id="alice").end(forget=True)
    check("forget clears session memory", not knowledge("session:alice:s1"))
    check("persona memory stays", knowledge("persona:alice"))


for name, fn in [("s1", first_session), ("maintain", maintenance),
                 ("recall alice", recall_alice), ("isolate bob", isolate_bob),
                 ("forget", forget)]:
    step(name, fn)

secs = sum(c["sec"] for llm in (actor, hook_llm, deep_llm) for c in llm.calls)
print(f"\ncalls: actor={len(actor.calls)} run-end={len(hook_llm.calls)} "
      f"maintain={len(deep_llm.calls)}  model time {secs:.0f}s")
failed = [name for name, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed"
      + (f"; failed: {failed}" if failed else ""))
