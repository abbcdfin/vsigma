"""Tests for the HTML extraction and web tools (no network)."""

from vsigma import make_web_tools

_HTML = (
    "<html><head><title>T</title><style>.x{}</style></head>"
    "<body><h1>Hello</h1><p>World <a href='/next'>link</a></p>"
    "<script>ignored()</script></body></html>"
)


def _fetch(url: str) -> str:
    return _HTML


def test_fetch_page_extracts_readable_text() -> None:
    tools = {tool.name: tool for tool in make_web_tools(_fetch)}
    text = tools["fetch_page"].run({"url": "https://e.com/p"})

    assert "Hello" in text
    assert "World" in text
    assert "ignored" not in text  # script content skipped
    assert "T" not in text.split()  # title skipped


def test_find_links_resolves_relative_urls() -> None:
    tools = {tool.name: tool for tool in make_web_tools(_fetch)}
    links = tools["find_links"].run({"url": "https://e.com/p"})

    assert links == "https://e.com/next"
