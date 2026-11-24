"""Pluggable search providers.

Built in: searxng (local, free), serper (Serper.dev Google results, needs SERPER_API_KEY), brave (Brave
Search API, needs BRAVE_API_KEY). Which ones run, in which order, and their settings: the "providers"
and "provider_settings" entries of the "search" block in config/query_plan.json. The first provider is
used until all its units are benched; then the next one, and so on.

Adding a provider = one file in this folder, no other code changes:

    # scripts/search_providers/mysearch.py
    import requests
    from search_providers import register
    from search_providers.base import Answer, SearchProvider, result

    @register
    class MySearch(SearchProvider):
        name = "mysearch"
        key_names = ("MYSEARCH_API_KEY",)        # read from env / .env / ~/.config/... (credentials.py)

        def search(self, session, query, page, units):
            r = session.get("https://api.mysearch.example/v1/search", timeout=30,
                            params={"q": query, "page": page},
                            headers={"Authorization": f"Bearer {self.key()}"})
            if r.status_code != 200:
                return self.http_failure(r)       # 429 -> benched, 401/403 -> key rejected, ...
            return Answer([result(i["url"], i["title"], i["snippet"], self.name) for i in r.json()["items"]])

then add "mysearch" to search.providers in config/query_plan.json and MYSEARCH_API_KEY=... to your key
file. Modules whose name starts with "_" are not loaded. Never put a key in the code or the config.
"""
from __future__ import annotations

import importlib
import pkgutil

from search_providers.base import Answer, SearchProvider, result

__all__ = ["Answer", "SearchProvider", "result", "register", "load_all", "REGISTRY"]

REGISTRY: dict[str, type[SearchProvider]] = {}


def register(cls: type[SearchProvider]) -> type[SearchProvider]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} has no name")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ValueError(f"search provider {cls.name!r} registered twice")
    REGISTRY[cls.name] = cls
    return cls


def load_all() -> dict[str, type[SearchProvider]]:
    """Import every provider module in this package (each registers itself)."""
    for m in pkgutil.iter_modules(__path__):
        if not m.name.startswith("_") and m.name != "base":
            importlib.import_module(f"{__name__}.{m.name}")
    return REGISTRY
