"""Measure a distill prompt: replay one `distill` call many times against a real LLM.

A full run of `live_memory_scenario.py` samples the prompt once, and five runs can pass by
luck. This replays the distill call alone, so a wording is judged on dozens of samples before
`_DISTILL_SYSTEM` changes. It calls the real `distill`, so what it counts is what would be
stored.

Each case below is a set of sources with two lists: what a good result must keep and what it
must not. Per prompt and case it reports the runs with a leak (something kept that must not
be) and with a loss (something missing that must be kept).

Needs `VSIGMA_LLM_BASE_URL`, `VSIGMA_LLM_API_KEY` and `VSIGMA_LLM_MODEL`, as the scenario does;
thinking is off unless `VSIGMA_LLM_EXTRA_BODY` says otherwise. Run from the repository root:

    set -a; . <env file>; set +a
    uv run python vdesign/utils/replay_distill.py                  # the prompt as built
    uv run python vdesign/utils/replay_distill.py -n 40 -p candidate.txt -v

`-p FILE` adds a complete system prompt to compare with the built one, and may be repeated.
Start a candidate from the built prompt:

    uv run python -c "from vsigma.construction import _DISTILL_SYSTEM as s; print(s)"
"""

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import vsigma.construction as construction
from vsigma import OpenAICompatibleLLM
from vsigma.construction import Source, distill
from vsigma.scope import persona

Check = Callable[[str], bool]


@dataclass(frozen=True)
class Case:
    name: str
    sources: list[Source]
    keep: dict[str, Check]  # label -> is this record the fact that must be kept?
    drop: dict[str, Check]  # label -> is this record one that must not be kept?


def _episode(steps: dict[str, object]) -> str:
    return json.dumps(steps)


_INTRO = "Hi. For all my shipments I always use DHL Express, and my warehouse code is WH-42. " \
         "Please keep that in mind."
_NOTED = "Understood. I've noted that you always use DHL Express and your warehouse code is " \
         "WH-42. How can I assist you further?"
_STATUS = "Order 1001 is currently packed at warehouse WH-42 and is awaiting carrier pickup."

# Two episodes of one session, in the shape `episode_trajectory` gives `maintain`: the user
# states two lasting facts, then asks for an order's status, which a tool reports.
SESSION = Case(
    name="session",
    sources=[
        ("trajectory:ep1", _episode({
            "input:1": {"messages": [{"role": "user", "content": _INTRO}]},
            "act:1": {"messages": [{"role": "assistant", "content": _NOTED}],
                      "result": _NOTED, "attempt_count": 1, "status": "done"},
        })),
        ("trajectory:ep2", _episode({
            "prior_context:ep1": [{"role": "user", "content": _INTRO},
                                  {"role": "assistant", "content": _NOTED}],
            "input:2": {"messages": [{"role": "user",
                                      "content": "What's the status of order 1001?"}]},
            "act:2": {"messages": [
                {"role": "assistant", "content": "", "tool_calls": [{
                    "type": "function", "id": "call_1",
                    "function": {"name": "order_status", "arguments": '{"order_id": "1001"}'}}]},
                {"role": "tool", "tool_call_id": "call_1",
                 "content": "Order 1001: packed at warehouse WH-42, awaiting carrier pickup."},
            ], "attempt_count": 1, "status": "running"},
            "act:3": {"messages": [{"role": "assistant", "content": _STATUS}],
                      "result": _STATUS, "attempt_count": 2, "status": "done"},
        })),
    ],
    keep={
        "carrier": lambda c: "dhl" in c.lower(),
        "warehouse code": lambda c: "WH-42" in c and "1001" not in c,
    },
    # A past event ("was packed") stays true, so only a current status counts as a leak.
    drop={"order status": lambda c: "1001" in c and " was " not in c},
)

# A document source, as bootstrap distills: standing rules, with two notes about today.
HANDBOOK = Case(
    name="handbook",
    sources=[
        ("docs/handbook.md",
         "# Fulfilment team handbook\n"
         "Deploys go through the `ship` command; never push to production by hand.\n"
         "The warehouse WH-42 operates on weekdays from 08:00 to 18:00.\n"
         "Refunds above 500 EUR need approval from the finance lead.\n"
         "The API rate limit for the carrier gateway is 60 requests per minute."),
        ("docs/notes-today.md",
         "Staging is down this afternoon for a database migration.\n"
         "The carrier gateway currently has a backlog of 340 labels.\n"
         "Support tickets are answered in English and German."),
    ],
    keep={
        "deploys": lambda c: "ship" in c.lower(),
        "opening hours": lambda c: "weekdays" in c.lower(),
        "refunds": lambda c: "500" in c,
        "rate limit": lambda c: "60 requests" in c,
        "languages": lambda c: "German" in c,
    },
    drop={
        "staging down": lambda c: "staging" in c.lower(),
        "backlog": lambda c: "backlog" in c.lower(),
    },
)

CASES = [SESSION, HANDBOOK]


def _llm() -> OpenAICompatibleLLM:
    extra = os.environ.get("VSIGMA_LLM_EXTRA_BODY")
    return OpenAICompatibleLLM(
        model=os.environ["VSIGMA_LLM_MODEL"],
        base_url=os.environ["VSIGMA_LLM_BASE_URL"],
        api_key=os.environ["VSIGMA_LLM_API_KEY"],
        timeout=float(os.environ.get("VSIGMA_LLM_TIMEOUT", "120")),
        max_tokens=1024,
        extra_body=json.loads(extra) if extra
        else {"chat_template_kwargs": {"enable_thinking": False}},
    )


def _once(case: Case, llm: OpenAICompatibleLLM) -> list[str] | str:
    """The contents one distill call would store, or why there is no result."""
    for _ in range(3):
        try:
            return [r.content for r in distill(case.sources, llm, scope=persona("alice"))]
        except ValueError:
            return "rejected"  # the model broke the output shape
        except Exception:  # noqa: BLE001  (the endpoint timed out or dropped the call)
            time.sleep(2)
    return "no answer"


def _measure(prompt: str, case: Case, runs: int, workers: int, verbose: bool) -> str:
    # `distill` reads the module's prompt; one prompt at a time, so the swap is not a race.
    construction._DISTILL_SYSTEM = prompt
    llm = _llm()
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda _: _once(case, llm), range(runs)))
    kept = [r for r in results if isinstance(r, list)]
    leaks: Counter[str] = Counter()
    losses: Counter[str] = Counter()
    leaked_runs = lost_runs = 0
    for contents in kept:
        leaked = {c for c in contents for check in case.drop.values() if check(c)}
        lost = [label for label, check in case.keep.items() if not any(map(check, contents))]
        leaks.update(leaked)
        losses.update(lost)
        leaked_runs += bool(leaked)
        lost_runs += bool(lost)
    other = Counter(r for r in results if isinstance(r, str))
    line = f"{case.name:9} runs={len(kept)}  leak={leaked_runs}  loss={lost_runs}"
    line += "".join(f"  {why}={n}" for why, n in other.items())
    for content, n in leaks.most_common():
        line += f"\n    leak x{n}: {content}"
    for label, n in losses.most_common():
        line += f"\n    lost x{n}: {label}"
    if verbose:
        for content, n in Counter(c for contents in kept for c in contents).most_common():
            line += f"\n    kept x{n}: {content}"
    return line


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    args.add_argument("-n", "--runs", type=int, default=20, help="replays per prompt and case")
    args.add_argument("-p", "--prompt", type=Path, action="append", default=[],
                      help="a file holding a complete system prompt to compare (repeatable)")
    args.add_argument("-c", "--case", choices=[c.name for c in CASES], action="append")
    args.add_argument("-w", "--workers", type=int, default=4, help="calls in flight at once")
    args.add_argument("-v", "--verbose", action="store_true", help="list every kept fact")
    opts = args.parse_args()

    prompts = {"built": construction._DISTILL_SYSTEM}
    prompts.update({p.stem: p.read_text().strip() for p in opts.prompt})
    for name, prompt in prompts.items():
        print(f"== {name}")
        for case in CASES:
            if not opts.case or case.name in opts.case:
                print(_measure(prompt, case, opts.runs, opts.workers, opts.verbose))


if __name__ == "__main__":
    main()
