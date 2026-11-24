"""Serper.dev: Google results through an API. Key: SERPER_API_KEY (https://serper.dev, free credits on sign-up).

provider_settings.serper: {"gl": "in", "hl": "en", "num": 10, "max_queries_per_run": 40, "monthly_limit": null}
One page = one credit.
"""
from __future__ import annotations

import requests

from search_providers import register
from search_providers.base import Answer, SearchProvider, result

ENDPOINT = "https://google.serper.dev/search"


@register
class Serper(SearchProvider):
    name = "serper"
    key_names = ("SERPER_API_KEY",)
    max_pages = 10

    def search(self, session: requests.Session, query: str, page: int, units: list[str]) -> Answer:
        s = self.settings
        r = session.post(ENDPOINT, timeout=30,
                         headers={"X-API-KEY": self.key(), "Content-Type": "application/json"},
                         json={"q": query, "page": page, "num": int(s.get("num", 10)),
                               "gl": s.get("gl", "in"), "hl": s.get("hl", "en")})
        if r.status_code != 200:
            return self.http_failure(r)
        items = r.json().get("organic") or []
        return Answer([result(i.get("link", ""), i.get("title"), i.get("snippet"), self.name, i.get("date"))
                       for i in items if i.get("link")])
