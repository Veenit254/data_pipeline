"""Brave Search API (independent index). Key: BRAVE_API_KEY (https://api-dashboard.search.brave.com,
the key is sent as X-Subscription-Token). Not the same as SearXNG's scraped "brave" engine.

provider_settings.brave: {"country": "IN", "search_lang": "en", "count": 20, "max_queries_per_run": 40,
"monthly_limit": null}. The API serves at most 10 pages (offset 0-9).
"""
from __future__ import annotations

import requests

from search_providers import register
from search_providers.base import Answer, SearchProvider, result

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


@register
class BraveAPI(SearchProvider):
    name = "brave"
    key_names = ("BRAVE_API_KEY",)
    max_pages = 10

    def search(self, session: requests.Session, query: str, page: int, units: list[str]) -> Answer:
        s = self.settings
        r = session.get(ENDPOINT, timeout=30,
                        headers={"X-Subscription-Token": self.key(), "Accept": "application/json"},
                        params={"q": query, "offset": page - 1, "count": int(s.get("count", 20)),
                                "country": s.get("country", "IN"), "search_lang": s.get("search_lang", "en")})
        if r.status_code != 200:
            return self.http_failure(r)
        items = (r.json().get("web") or {}).get("results") or []
        return Answer([result(i.get("url", ""), i.get("title"), i.get("description"), self.name,
                              i.get("page_age") or i.get("age")) for i in items if i.get("url")])
