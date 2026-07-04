"""Demo: a web-browsing automation agent assembled from vsigma blocks.

Run offline with `python -m examples.web_browsing.demo` — a scripted `FakeLLM`
and canned pages make it deterministic with no network or API key. Set
``VSIGMA_LLM_BASE_URL`` / ``VSIGMA_LLM_API_KEY`` to drive it with a real
OpenAI-compatible model against the live web instead.
"""

from __future__ import annotations

from vsigma import (
    LLM,
    Agent,
    FakeLLM,
    LLMResponse,
    Run,
    ToolCall,
    default_llm,
    make_react_agent,
)

from .tools import Fetcher, make_web_tools

BROWSE_SYSTEM = (
    "You are a web-browsing assistant. Use the fetch_page and find_links tools to "
    "navigate the web and answer the user's question. When you have the answer, "
    "reply with it directly and stop calling tools."
)

DEFAULT_TASK = "What is the title of the latest post linked from https://example.com ?"

_FAKE_PAGES: dict[str, str] = {
    "https://example.com": (
        "<html><head><title>Example</title></head><body><h1>Example Domain</h1>"
        "<p>Latest post: <a href='/post/hello'>Hello World</a></p></body></html>"
    ),
    "https://example.com/post/hello": (
        "<html><body><h1>Hello World</h1><p>The first post.</p></body></html>"
    ),
}


def _fake_fetch(url: str) -> str:
    return _FAKE_PAGES.get(url, "<html><body>not found</body></html>")


def _demo_fake_llm() -> FakeLLM:
    return FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="fetch_page", arguments={"url": "https://example.com"})
                ]
            ),
            LLMResponse(text="The latest post linked from example.com is titled 'Hello World'."),
        ]
    )


def build_browsing_agent(llm: LLM, *, fetch: Fetcher | None = None) -> Agent:
    # The web-browsing agent is just the flat ReAct template wired with web tools.
    return make_react_agent(llm, make_web_tools(fetch), system=BROWSE_SYSTEM)


def run_demo(
    *, llm: LLM | None = None, fetch: Fetcher | None = None, task: str = DEFAULT_TASK
) -> Run:
    """Build and run the browsing agent; returns the `Run` (also printed)."""
    if llm is None:
        remote = default_llm()
        if remote is not None:
            llm = remote
        else:
            llm = _demo_fake_llm()
            if fetch is None:
                fetch = _fake_fetch
    agent = build_browsing_agent(llm, fetch=fetch)
    run = agent.run(task, system=BROWSE_SYSTEM, session_id="demo-session")
    print(f"task:   {task}")
    print(f"status: {run.status}")
    print(f"result: {run.result}")
    print("trajectory:")
    for event in run.trajectory:
        print(f"  - {event.summary}")
    return run


if __name__ == "__main__":
    run_demo()
