"""The interface every search provider implements. See search_providers/__init__.py for how to add one."""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

import credentials


@dataclass
class Answer:
    """One results page. `results` are dicts with url, title, content, engines, publishedDate.
    `failures` are (unit, reason) pairs for units that did not answer; the reason decides the cooldown
    (matched against search.cooldown_min in config/query_plan.json: "too many requests", "captcha",
    "access denied", "quota", ...)."""
    results: list[dict] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    mode: str = "json"


class SearchProvider:
    """A search API. Subclasses set `name` and implement `search`; `@register` makes them usable.

    Rotation works on *units*: the things that get rate-limited and benched independently. An API is one
    unit (itself); SearXNG's units are its upstream engines. Each query uses `units_per_query` of them.
    """
    name: str = ""
    key_names: tuple[str, ...] = ()   # credentials this provider needs (looked up via credentials.get)
    units_per_query: int = 1
    max_pages: int = 10               # deepest results page the API serves

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}

    # --- override these -------------------------------------------------------------------------
    def search(self, session: requests.Session, query: str, page: int, units: list[str]) -> Answer:
        raise NotImplementedError

    def units(self) -> list[str]:
        return [self.name]

    def health(self) -> str | None:
        """None if the provider can be used, else why not (cheap: no paid request)."""
        missing = [k for k in self.key_names if not credentials.get(k)]
        return f"no API key: set {', '.join(missing)} in .env or ~/.config/nse-unified-pipeline/keys.env" \
            if missing else None

    # --- helpers --------------------------------------------------------------------------------
    def key(self, name: str | None = None) -> str:
        v = credentials.get(name or self.key_names[0])
        if not v:
            raise RuntimeError(f"{self.name}: missing API key {name or self.key_names[0]}")
        return v

    def redact(self, text: str) -> str:
        """Remove this provider's keys from text that may be logged (error messages, URLs)."""
        for k in self.key_names:
            v = credentials.get(k)
            if v:
                text = text.replace(v, "***")
        return text

    def http_failure(self, r: requests.Response) -> Answer:
        """Map a non-200 API response to a failure reason the rotation understands."""
        body = (r.text or "")[:300].lower()
        if r.status_code == 429:
            reason = "quota exhausted (429)" if "quota" in body else "too many requests (429)"
        elif r.status_code in (401, 403) or "invalid" in body and "key" in body:
            reason = f"access denied: API key rejected ({r.status_code})"
        elif "credit" in body or "quota" in body or r.status_code == 402:
            reason = f"quota exhausted ({r.status_code})"
        else:
            reason = f"http {r.status_code}"
        return Answer(failures=[(self.name, reason)])


def result(url: str, title: str = "", content: str = "", engine: str = "", published: str | None = None) -> dict:
    return {"url": url, "title": title or "", "content": content or "", "engines": [engine] if engine else [],
            "publishedDate": published}
