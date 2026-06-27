"""Lightweight web-browsing tools (HTTP fetch + stdlib HTML parsing).

No browser binaries: fetch a page over HTTP and extract readable text or links.
The `fetch` callable is injectable so tests (and the offline demo) can supply
canned pages with no network. The tool interface is shaped so a Playwright-backed
adapter could replace the fetcher later without touching agent logic.
"""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from .tools import Tool

#: Maps a URL to raw HTML/text.
Fetcher = Callable[[str], str]

_MAX_TEXT = 4000
_MAX_LINKS = 50
_USER_AGENT = "vsigma-bot/0.1"
_SKIP_TAGS = {"script", "style", "head", "title", "noscript"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    @property
    def text(self) -> str:
        return " ".join(self._chunks)


class _LinkExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(urljoin(self._base, value))


def _extract_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text[:_MAX_TEXT]


def _extract_links(html: str, base_url: str) -> list[str]:
    parser = _LinkExtractor(base_url)
    parser.feed(html)
    # Stable de-duplication.
    seen: dict[str, None] = {}
    for link in parser.links:
        seen.setdefault(link, None)
    return list(seen)[:_MAX_LINKS]


def _httpx_fetch(url: str) -> str:
    response = httpx.get(
        url, headers={"User-Agent": _USER_AGENT}, timeout=30.0, follow_redirects=True
    )
    response.raise_for_status()
    return response.text


_URL_PARAM: dict[str, object] = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "The page URL."}},
    "required": ["url"],
}


def make_web_tools(fetch: Fetcher | None = None) -> list[Tool]:
    do_fetch: Fetcher = fetch or _httpx_fetch

    def fetch_page(arguments: dict[str, object]) -> str:
        return _extract_text(do_fetch(str(arguments["url"])))

    def find_links(arguments: dict[str, object]) -> str:
        url = str(arguments["url"])
        links = _extract_links(do_fetch(url), url)
        return "\n".join(links) if links else "(no links found)"

    return [
        Tool(
            name="fetch_page",
            description="Fetch a web page and return its readable text.",
            parameters=_URL_PARAM,
            func=fetch_page,
        ),
        Tool(
            name="find_links",
            description="Fetch a web page and return the links it contains.",
            parameters=_URL_PARAM,
            func=find_links,
        ),
    ]
