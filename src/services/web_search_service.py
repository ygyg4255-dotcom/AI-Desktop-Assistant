"""Lightweight web search: query DuckDuckGo's HTML endpoint and parse the top results.

The HTTP fetch is injected by the caller (``tools.py`` passes the SSRF-checked public opener),
so this module has no network access of its own and is easy to test offline.
"""
import html
import re
from typing import Callable, List
from urllib.parse import parse_qs, quote_plus, urlparse

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/?q={query}"
MAX_RESULTS_LIMIT = 8
MAX_QUERY_CHARS = 200
_RESULT_LINK = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_RESULT_SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I)


class WebSearchError(ValueError):
    """The query is invalid or the search page could not be used."""


def _plain_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _resolve_link(href: str) -> str:
    """Turn DuckDuckGo's redirect links (//duckduckgo.com/l/?uddg=...) into the target URL."""
    href = html.unescape(href).strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        href = target or href
    parsed = urlparse(href)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return href


def parse_results(page: str, limit: int) -> List[dict]:
    snippets = [_plain_text(m.group(1)) for m in _RESULT_SNIPPET.finditer(page)]
    results: List[dict] = []
    seen = set()
    for index, match in enumerate(_RESULT_LINK.finditer(page)):
        url = _resolve_link(match.group(1))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "title": _plain_text(match.group(2))[:200],
            "url": url,
            "snippet": snippets[index][:400] if index < len(snippets) else "",
        })
        if len(results) >= limit:
            break
    return results


def search(query: str, fetch: Callable[[str], str], limit: int = 5) -> List[dict]:
    """Return up to ``limit`` results as dicts with title, url and snippet."""
    text = str(query or "").strip()
    if not text:
        raise WebSearchError("A search query is required.")
    if len(text) > MAX_QUERY_CHARS or any(ord(ch) < 32 for ch in text):
        raise WebSearchError("That search query is too long or contains control characters.")
    limit = max(1, min(int(limit), MAX_RESULTS_LIMIT))
    return parse_results(fetch(SEARCH_ENDPOINT.format(query=quote_plus(text))), limit)
