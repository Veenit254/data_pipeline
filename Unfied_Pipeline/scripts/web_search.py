"""Web search for the collector through pluggable providers, with results cached in data/search_cache/.

Providers (scripts/search_providers/): searxng (local, free), serper (Serper.dev), brave (Brave Search
API), plus any module dropped into that folder. config/query_plan.json, "search" block:
  "providers": ["searxng", "serper", "brave"]   order of use: the next one only when the previous is
                                                 used up (all units benched, daily/monthly limit reached,
                                                 no key, or down)
  "provider_settings": {"serper": {"daily_limit": 50, ...}, ...}
API keys come from the environment, ~/.config/nse-unified-pipeline/keys.env or the git-ignored .env
(scripts/credentials.py), never from code or config.

Rotation: each provider has *units* that are benched independently (SearXNG: its upstream engines, an
API: itself). A query asks `units_per_query` of them, longest-rested first. A unit that is rate-limited,
CAPTCHA'd, denied or out of quota is benched for a cooldown that depends on the reason; one whose answers
are junk (no result on a known source domain and none about delisting) is benched too.
State: data/metadata/engine_state.json.

Examples:
  python scripts/web_search.py --query "nse compulsory delisting 2017" --pages 2 --year-tag 2017
  python scripts/web_search.py --engines        # providers, keys found (not shown), usable / benched units
  python scripts/web_search.py --check          # can any provider search right now? (exit 1 if none)
  python scripts/web_search.py --list-cache
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta

import requests

import credentials
import search_providers
from common import atomic_write_text, CONFIG, METADATA, SEARCH_CACHE, USER_AGENT, domain_matches, emit, log, now_iso, \
    polite_sleep, sha256_text
from search_providers.base import Answer, SearchProvider

ENGINE_STATE = METADATA / "engine_state.json"
SEARCH_DEFAULTS = {
    "providers": ["searxng", "serper", "brave"],
    "provider_settings": {},
    # minutes a unit is benched, by the failure reason (substring match, lower case; longest wins)
    "cooldown_min": {"too many requests": 60, "suspended": 60, "captcha": 360, "access denied": 720,
                     "irrelevant": 360, "quota": 1440, "default": 120},
    "irrelevant_after": 3,          # consecutive answers with no relevant result -> benched
    "query_delay_s": [20, 40],      # pause between queries (collect.py)
    # a clean empty answer: also ask the next provider? Off by default: the next ones are usually paid APIs
    # (Serper/Brave credits). Set "fallback_on_empty": true in config "search" to trade credits for recall.
    "fallback_on_empty": False,
}

# A result is relevant if it is on a known source domain or its text is about delisting.
RELEVANT_RESULT_RE = re.compile(r"delist|exit offer|reverse book|compulsor|nseindia|bseindia|sebi", re.I)
NEW_UNIT = {"benched_until": 0, "reason": None, "last_used": 0, "uses": 0, "results": 0, "official": 0,
            "irrelevant_streak": 0}


def relevant(r: dict, domains) -> bool:
    text = " ".join(str(r.get(k) or "") for k in ("url", "title", "content"))
    return domain_matches(r.get("url", ""), domains) or bool(RELEVANT_RESULT_RE.search(text))


class SearchExhausted(RuntimeError):
    """No provider can search now; `until` is when the first one is usable again."""
    def __init__(self, until: float, detail: dict | None = None):
        super().__init__(f"all search providers used up; next usable in {(until - time.time()) / 60:.0f} min")
        self.until, self.detail = until, detail or {}


def search_settings() -> dict:
    try:
        cfg = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8")).get("search", {})
    except (OSError, ValueError):
        cfg = {}
    s = {**SEARCH_DEFAULTS, **cfg}
    s["cooldown_min"] = {**SEARCH_DEFAULTS["cooldown_min"], **cfg.get("cooldown_min", {})}
    s["provider_settings"] = {k: dict(v) for k, v in (cfg.get("provider_settings") or {}).items()}
    sx = s["provider_settings"].setdefault("searxng", {})
    for k in ("engines", "engines_per_query"):  # older configs keep these at the top of the block
        if k in cfg:
            sx.setdefault(k, cfg[k])
    try:
        s["official_domains"] = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8"))["fetch_domains"]
    except (OSError, ValueError, KeyError):
        s["official_domains"] = ["nseindia.com", "sebi.gov.in", "bseindia.com"]
    return s


def load_state() -> dict:
    try:
        st = json.loads(ENGINE_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if any(isinstance(v, dict) and "benched_until" in v for v in st.values()):  # old flat SearXNG-only file
        st = {"searxng": {k: v for k, v in st.items() if isinstance(v, dict) and "benched_until" in v}}
    return st


class Rotation:
    """Bench / rest bookkeeping for one provider's units."""

    def __init__(self, provider: str, units: list[str], settings: dict, state: dict):
        self.provider, self.units, self.s, self.state = provider, list(units), settings, state
        for u in self.units:
            state.setdefault(u, dict(NEW_UNIT))

    def usable(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        free = [u for u in self.units if self.state[u]["benched_until"] <= now]
        return sorted(free, key=lambda u: self.state[u]["last_used"])  # longest-rested first

    def pick(self, n: int, exclude: set[str] = frozenset()) -> list[str]:
        return [u for u in self.usable() if u not in exclude][:n]

    def until(self) -> float:
        return min((self.state[u]["benched_until"] for u in self.units), default=0)

    def bench(self, unit: str, reason: str) -> None:
        r = reason.lower()
        minutes = max((m for k, m in self.s["cooldown_min"].items() if k != "default" and k in r),
                      default=self.s["cooldown_min"]["default"])
        self.state[unit].update(benched_until=time.time() + 60 * minutes, reason=reason)
        log(f"{self.provider} {unit} benched {minutes} min: {reason}", level="WARN", kind="engine_benched",
            provider=self.provider, engine=unit, reason=reason, minutes=minutes)

    def record(self, units: list[str], results: list[dict], failures: list[tuple[str, str]]) -> None:
        failed = dict(failures)
        for u in units:
            st = self.state[u]
            st["last_used"], st["uses"] = time.time(), st["uses"] + 1
            if u in failed:
                self.bench(u, failed[u])
                continue
            mine = [r for r in results if u in (r.get("engines") or [])]
            st["results"] += len(mine)
            st["official"] += sum(domain_matches(r.get("url", ""), self.s["official_domains"]) for r in mine)
            useful = sum(relevant(r, self.s["official_domains"]) for r in mine)
            # empty or junk-only answers count toward benching the unit
            st["irrelevant_streak"] = 0 if useful else st["irrelevant_streak"] + 1
            if st["irrelevant_streak"] >= self.s["irrelevant_after"]:
                st["irrelevant_streak"] = 0
                self.bench(u, f"irrelevant: {self.s['irrelevant_after']} answers in a row with no relevant result")

    def report(self) -> dict:
        now = time.time()
        return {"usable": self.usable(now),
                "benched": {u: {"minutes_left": round((self.state[u]["benched_until"] - now) / 60),
                                "reason": self.state[u]["reason"]}
                            for u in self.units if self.state[u]["benched_until"] > now},
                "stats": {u: {k: self.state[u][k] for k in ("uses", "results", "official")} for u in self.units}}


def _next_midnight(now: datetime) -> float:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _next_month(now: datetime) -> float:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1).timestamp()


class Search:
    """The configured providers, their rotations and budgets."""

    def __init__(self, settings: dict | None = None, registry: dict | None = None):
        self.s = settings or search_settings()
        self.state = load_state()
        registry = registry if registry is not None else search_providers.load_all()
        self.providers: list[SearchProvider] = []
        self.unknown = [n for n in self.s["providers"] if n not in registry]
        for n in self.s["providers"]:
            if n in registry:
                self.providers.append(registry[n](self.s["provider_settings"].get(n, {})))
        self.rot = {p.name: Rotation(p.name, p.units(), self.s, self.state.setdefault(p.name, {}))
                    for p in self.providers}
        self.down: dict[str, str] = {}  # provider -> error, for this process

    def save(self) -> None:
        ENGINE_STATE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(ENGINE_STATE, json.dumps(self.state, indent=1))

    # --- budgets ---------------------------------------------------------------------------------
    def usage(self, name: str) -> dict:
        now = datetime.now()
        u = self.state.setdefault("_usage", {}).setdefault(name, {})
        if u.get("day") != now.strftime("%Y-%m-%d"):
            u.update(day=now.strftime("%Y-%m-%d"), today=0)
        if u.get("month") != now.strftime("%Y-%m"):
            u.update(month=now.strftime("%Y-%m"), this_month=0)
        u.setdefault("total", 0)
        return u

    def budget_until(self, p: SearchProvider) -> float | None:
        """None if the provider may make another request, else when its limit resets."""
        u, now = self.usage(p.name), datetime.now()
        day, month = p.settings.get("daily_limit"), p.settings.get("monthly_limit")
        if month is not None and u["this_month"] >= month:
            return _next_month(now)
        if day is not None and u["today"] >= day:
            return _next_midnight(now)
        return None

    def spend(self, p: SearchProvider) -> None:
        u = self.usage(p.name)
        u["today"] += 1
        u["this_month"] += 1
        u["total"] += 1

    # --- status ----------------------------------------------------------------------------------
    def blocker(self, p: SearchProvider) -> tuple[str, float | None] | None:
        """Why p can't search now (and until when, if it will recover by waiting), or None."""
        if p.name in self.down:
            return f"down: {self.down[p.name]}", None
        if p.key_names and not all(credentials.get(k) for k in p.key_names):
            return p.health() or "no API key", None
        b = self.budget_until(p)
        if b:
            return "daily/monthly limit reached", b
        if not self.rot[p.name].usable():
            return "all units benched", self.rot[p.name].until()
        return None

    def usable(self) -> list[str]:
        """Usable units as provider:unit (just the provider for single-unit APIs)."""
        out = []
        for p in self.providers:
            if not self.blocker(p):
                out += [p.name if u == p.name else f"{p.name}:{u}" for u in self.rot[p.name].usable()]
        return out

    def cooldown_until(self) -> float:
        """0 if some provider can search now, else when the first one that recovers by waiting will."""
        waits = []
        for p in self.providers:
            b = self.blocker(p)
            if not b:
                return 0
            if b[1]:
                waits.append(b[1])
        return min(waits, default=0)

    def report(self, check: bool = False) -> dict:
        provs = {}
        for p in self.providers:
            b = self.blocker(p)
            why = (p.health() if check else None) or (b[0] if b else None)
            r = self.rot[p.name].report()
            if p.units() == [p.name]:
                r.pop("usable")
            provs[p.name] = {"ready": why is None, **({"why_not": why} if why else {}),
                             **({"key_from": ", ".join(credentials.where(k) or "not found" for k in p.key_names)}
                                if p.key_names else {}),
                             "usage": {k: v for k, v in self.usage(p.name).items() if k in ("today", "this_month", "total")},
                             **{k: v for k, v in p.settings.items() if k.endswith("_limit")}, **r}
        benched = {(p if u == p else f"{p}:{u}"): v for p in provs for u, v in provs[p]["benched"].items()}
        return {"order": [p.name for p in self.providers], "unknown_providers": self.unknown,
                "usable": self.usable(), "benched": benched, "providers": provs}

    # --- searching -------------------------------------------------------------------------------
    def page(self, session: requests.Session, query: str, page: int) -> tuple[Answer, str, list[str]]:
        """One results page from the first provider that can answer it."""
        waits, errors, failed_answer, empty_answer = [], {}, None, None
        for p in self.providers:
            if page > p.max_pages:
                continue
            b = self.blocker(p)
            if b:
                if b[1]:
                    waits.append(b[1])
                continue
            rot, tried, last = self.rot[p.name], set(), None
            for attempt in range(2):  # SearXNG: if the first engines give nothing, try the next ones once
                units = rot.pick(p.units_per_query, exclude=tried)
                if not units or self.budget_until(p):
                    break
                if attempt:
                    polite_sleep()
                tried.update(units)
                try:
                    ans = p.search(session, query, page, units)
                except Exception as e:  # provider down / network: next provider, don't bench its units
                    self.down[p.name] = errors[p.name] = p.redact(f"{type(e).__name__}: {e}")[:200]
                    log(f"search provider {p.name} failed: {self.down[p.name]}", level="ERROR",
                        kind="search_provider_error", provider=p.name)
                    break
                self.spend(p)
                rot.record(units, ans.results, ans.failures)
                self.save()
                if ans.results:
                    return ans, p.name, units
                if set(units) - {u for u, _ in ans.failures}:
                    last = (ans, p.name, units)  # someone answered: genuinely no results
                else:
                    failed_answer = failed_answer or (ans, p.name, units)
            if last:  # a clean empty answer: ask the next provider too (scrapers miss old documents), unless off
                if not self.s.get("fallback_on_empty", False):
                    return last
                empty_answer = empty_answer or last
                continue
            b = self.blocker(p)
            if b and b[1]:
                waits.append(b[1])
        if empty_answer:  # every provider that answered had nothing
            return empty_answer
        if failed_answer:  # every unit asked failed and nobody else could answer: caller sees the failures
            return failed_answer
        if waits:
            raise SearchExhausted(min(waits), errors)
        raise RuntimeError("no search provider available: "
                           + "; ".join(f"{p.name}: {(self.blocker(p) or ('no answer',))[0]}" for p in self.providers))


def cache_path(query: str, page: int):
    return SEARCH_CACHE / f"{sha256_text(f'{query}|{page}')[:16]}.json"


def run(query: str, pages: int, year_tag: str | None, search: Search | None = None) -> dict:
    SEARCH_CACHE.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    search = search or Search()
    seen: set[str] = set()
    cache_files, new_urls, mode, cached_pages = [], [], "json", 0
    unresponsive: set[str] = set()
    used: set[str] = set()
    providers_used: set[str] = set()
    for page in range(1, pages + 1):
        path = cache_path(query, page)
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):  # truncated by a crash: a miss, searched again and rewritten
            log(f"search cache {path.name} unreadable: searching again", level="WARN", kind="search_cache_corrupt")
            payload = None
        if payload and payload["results"]:
            cached_pages += 1
        else:  # not cached, or cached empty (engines may have been throttled) -> ask again
            if page > 1:
                polite_sleep()
            ans, prov, units = search.page(session, query, page)
            mode = ans.mode
            providers_used.add(prov)
            used.update(units)
            unresponsive.update(u for u, _ in ans.failures)
            payload = {
                "query": query, "page": page, "year_tag": year_tag, "provider": prov, "mode": mode,
                "retrieved_at": now_iso(), "unresponsive_engines": [list(f) for f in ans.failures],
                "results": [{k: r.get(k) for k in ("url", "title", "content", "engines", "publishedDate")}
                            for r in ans.results],
            }
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))
        cache_files.append(str(path.relative_to(SEARCH_CACHE.parent.parent)))
        page_new = [r["url"] for r in payload["results"] if r.get("url") and r["url"] not in seen]
        seen.update(page_new)
        new_urls.extend(page_new)
        if not page_new:
            break

    log(f"query={query!r} pages={len(cache_files)} urls={len(new_urls)} via={sorted(providers_used)} tag={year_tag}",
        kind="search", query=query, pages=len(cache_files), cached_pages=cached_pages, urls=len(new_urls),
        mode=mode, year_tag=year_tag, providers=sorted(providers_used), unresponsive_engines=sorted(unresponsive),
        engines=sorted(used))
    return {"query": query, "year_tag": year_tag, "mode": mode, "providers": sorted(providers_used),
            "cache_files": cache_files, "cached_pages": cached_pages,
            "unresponsive_engines": sorted(unresponsive), "engines_used": sorted(used),
            "result_count": len(new_urls), "top_urls": new_urls[:10]}


def list_cache() -> dict:
    entries = []
    for p in sorted(SEARCH_CACHE.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries.append({"file": p.name, "query": d.get("query"), "page": d.get("page"), "provider": d.get("provider"),
                        "year_tag": d.get("year_tag"), "results": len(d.get("results", []))})
    return {"cached": len(entries), "entries": entries}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--query", "-q")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--year-tag", help="2007|2017|2020|2024|master|<year>")
    ap.add_argument("--list-cache", action="store_true")
    ap.add_argument("--engines", "--providers", action="store_true", help="providers and rotation state")
    ap.add_argument("--check", action="store_true", help="exit 0 if some provider can search now")
    a = ap.parse_args()

    if a.engines or a.check:
        rep = Search().report(check=a.check)
        emit(rep)
        return 0 if not a.check or any(v["ready"] for v in rep["providers"].values()) else 1
    if a.list_cache:
        emit(list_cache())
        return 0
    if not a.query:
        ap.error("--query is required")
    try:
        emit(run(a.query, max(1, a.pages), a.year_tag))
    except (requests.RequestException, RuntimeError) as e:  # incl. SearchExhausted
        log(f"query={a.query!r} failed: {e}", level="ERROR", kind="search_error", query=a.query)
        emit({"error": str(e), "query": a.query})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
