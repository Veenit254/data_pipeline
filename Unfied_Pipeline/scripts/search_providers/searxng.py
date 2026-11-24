"""Local SearXNG metasearch (docker-compose.yml). Free; its units are SearXNG's upstream engines.

provider_settings.searxng: {"url": "http://localhost:8082", "engines": [...], "engines_per_query": 2}
(engines and engines_per_query may also sit directly in the search block, as before).
Uses the JSON API; if SearXNG answers 403 (JSON disabled) it parses the HTML results page instead.
"""
from __future__ import annotations

import time

import requests
from bs4 import BeautifulSoup

from common import SEARXNG_URL
from search_providers import register
from search_providers.base import Answer, SearchProvider

DEFAULT_ENGINES = ["google cse", "brave", "duckduckgo", "startpage", "bing", "mojeek",
                   "yahoo", "qwant", "dogpile", "presearch", "yep"]


def parse_html_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for art in soup.select("article.result"):
        a = art.select_one("h3 a") or art.select_one("a.url_header") or art.select_one("a")
        if not a or not a.get("href"):
            continue
        content = art.select_one(".content")
        results.append({"url": a["href"], "title": a.get_text(" ", strip=True),
                        "content": content.get_text(" ", strip=True) if content else "", "engines": []})
    return results


@register
class SearXNG(SearchProvider):
    name = "searxng"
    max_pages = 20

    @property
    def url(self) -> str:
        return self.settings.get("url", SEARXNG_URL).rstrip("/")

    @property
    def units_per_query(self) -> int:  # type: ignore[override]
        return int(self.settings.get("engines_per_query", 2))

    def units(self) -> list[str]:
        return list(self.settings.get("engines") or DEFAULT_ENGINES)

    def health(self) -> str | None:
        try:
            r = requests.get(f"{self.url}/search", params={"q": "nse", "format": "json"}, timeout=15)
            return None if r.status_code == 200 else f"SearXNG at {self.url} answered {r.status_code}"
        except requests.RequestException as e:
            return f"SearXNG not reachable at {self.url}: {type(e).__name__}"

    def search(self, session: requests.Session, query: str, page: int, units: list[str],
               retries: int = 3) -> Answer:
        params = {"q": query, "pageno": page, "format": "json", "engines": ",".join(units)}
        for attempt in range(retries):
            r = session.get(f"{self.url}/search", params=params, timeout=30)
            if r.status_code == 200:
                d = r.json()
                failures = [(str(e[0]), str(e[1]) if len(e) > 1 else "unresponsive") if isinstance(e, list)
                            else (str(e), "unresponsive") for e in d.get("unresponsive_engines", [])]
                return Answer(d.get("results", []), failures)
            if r.status_code == 403:
                params.pop("format")
                h = session.get(f"{self.url}/search", params=params, timeout=30)
                h.raise_for_status()
                return Answer(parse_html_results(h.text), mode="html_fallback")
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            r.raise_for_status()
        raise RuntimeError(f"SearXNG failed after {retries} attempts for page {page}")
