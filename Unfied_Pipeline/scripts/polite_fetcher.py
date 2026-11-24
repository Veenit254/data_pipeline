"""Cached, rate-limited downloader.

- Never fetches the same URL twice (checked against logs/fetch_manifest.jsonl).
- Per-domain delay and per-run budget (config/query_plan.json "domain_limits"): NSE rate-limits hard,
  so it gets long pauses and few requests per run; URLs over budget are deferred to a later run.
- Any website may be fetched when "open_web" is true, except "deny_domains" (social media, video…).
- Exponential backoff on 429/5xx.
- Non-200 or empty responses go to data/quarantine/.
- Every attempt is appended to logs/fetch_manifest.jsonl.

Examples:
  python scripts/polite_fetcher.py --url "https://www.nseindia.com/..." --year-tag 2017
  python scripts/polite_fetcher.py --from-cache-file data/search_cache/ab12.json --limit 10 \
      --domains nseindia.com,sebi.gov.in,bseindia.com
"""
from __future__ import annotations

import argparse
import functools
import json
import mimetypes
import re
import sys
import time
from collections import Counter
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from common import (
    CONFIG, MANIFEST, NON_LISTING_RE, PRIMARY_DOMAINS, PROCEDURAL_RE, QUARANTINE, ROOT, USER_AGENT, append_jsonl, domain_matches, domain_of, emit, log,
    now_iso, polite_sleep, raw_dir_for_tag, read_jsonl, sha256_bytes, sha256_text,
)

MAX_RETRIES = 4
RETRY_AFTER_CAP_S = 120
DEFAULT_MAX_DOWNLOAD_MB = 40     # config "max_download_mb"; the largest real delisting document is a few MB
DOC_URL_RE = re.compile(r"\.(pdf|xlsx?|csv|zip)$", re.I)
# Block / challenge pages that WAFs serve with status 200 instead of the document
WAF_RE = re.compile(rb"access denied|request unsuccessful|incapsula|attention required|cf-browser-verification|"
                    rb"captcha|are you a robot|reference #\d", re.I)
CONTENT_EXT = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/plain": ".txt",
}


# No .zip: on BSE/NSE these are bundles of scanned newspaper notices (5–15 MB each, no extractable text).
DOC_LINK_RE = re.compile(r"\.(pdf|xlsx?|csv)(\?|#|$)", re.I)
RELEVANT_RE = re.compile(r"delist|exit|compuls|order|suspend|reverse.?book", re.I)
DEFAULT_LIMITS = {"default": {"max_per_run": 15, "delay_s": [2, 5]}}
RUN_COUNTS: Counter = Counter()  # requests per domain in this process (one collect.py run)


def fetch_policy() -> dict:
    """Which domains may be fetched, and how often, from config/query_plan.json."""
    try:
        plan = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    return {"open_web": plan.get("open_web", False), "known": plan.get("fetch_domains", list(PRIMARY_DOMAINS)),
            "deny": plan.get("deny_domains", []), "limits": {**DEFAULT_LIMITS, **plan.get("domain_limits", {})}}


def allowed(url: str, policy: dict) -> bool:
    if domain_matches(url, policy["deny"]):
        return False
    return policy["open_web"] or domain_matches(url, policy["known"])


def limit_key(url: str, policy: dict) -> str:
    """The domain_limits entry for a URL: a configured domain (subdomains count), else the host itself."""
    return next((k for k in policy["limits"] if k != "default" and domain_matches(url, [k])), domain_of(url))


def limits_for(key: str, policy: dict) -> dict:
    return {**policy["limits"]["default"], **policy["limits"].get(key, {})}


def harvest_links(path: Path, page_url: str, policy: dict | None = None) -> list[str]:
    """Document links (PDF/XLS/XLSX/CSV) found in a downloaded HTML page, pointing to a known source
    domain or to the page's own site. On pages whose URL mentions delisting every document link is
    kept; elsewhere only links whose URL or anchor text looks delisting-related."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "lxml")
    policy = policy or fetch_policy()
    page_relevant = bool(re.search(r"delist", page_url, re.I))
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        url = re.sub(r"(?<!:)//+", "/", url)  # NSE links often contain '//web/...'
        if not DOC_LINK_RE.search(urlparse(url).path) or not allowed(url, policy):
            continue
        if not (domain_matches(url, policy["known"]) or domain_of(url) == domain_of(page_url)):
            continue
        fname = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        if PROCEDURAL_RE.search(fname) or NON_LISTING_RE.search(fname):  # forms/SOPs, broker/member orders
            continue
        if page_relevant or RELEVANT_RE.search(url) or RELEVANT_RE.search(a.get_text(" ", strip=True)):
            out.append(url)
    return list(dict.fromkeys(out))


def already_fetched() -> dict[str, dict]:
    """URL -> manifest entry for URLs that must not be requested again: successful downloads and
    definitive client errors (4xx except 429). Timeouts, 429 and 5xx stay retryable."""
    done = {}
    for e in read_jsonl(MANIFEST):
        st = e.get("http_status")
        if (st == 200 and not e.get("quarantined")) or (isinstance(st, int) and 400 <= st < 500 and st != 429):
            done[e["url"]] = e
    return done


def pick_ext(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_EXT:
        return CONTENT_EXT[ct]
    suffix = Path(urlparse(url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return mimetypes.guess_extension(ct) or ".bin"


def retry_after(value) -> int | None:
    """Seconds to wait from a Retry-After header: delay-seconds or an HTTP date (RFC 9110 10.2.3).
    None if absent or unreadable (the caller falls back to exponential backoff); 0 if the date has passed."""
    s = str(value).strip() if value is not None else ""
    if re.fullmatch(r"\d{1,9}", s):
        return int(s)
    if not s or not re.search(r"[A-Za-z]", s):
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int(dt.timestamp() - time.time()))


@functools.lru_cache(maxsize=1)
def max_download_bytes() -> int:
    try:
        mb = float(json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8")).get("max_download_mb")
                   or DEFAULT_MAX_DOWNLOAD_MB)
    except (OSError, ValueError, TypeError):
        mb = DEFAULT_MAX_DOWNLOAD_MB
    return int(mb * 1e6)


def read_body(r, cap: int) -> tuple[bytes, bool]:
    """The response body, streamed, stopping as soon as it exceeds cap bytes: (body, too_large)."""
    cl = str(r.headers.get("Content-Length") or "")
    if cl.isdigit() and int(cl) > cap:
        return b"", True
    if hasattr(r, "iter_content"):
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) > cap:
                return b"", True
        return bytes(buf), False
    body = r.content
    return (b"", True) if len(body) > cap else (body, False)


def soft_block(url: str, ctype: str, body: bytes) -> str | None:
    """Why a 200 response is a block page rather than the document, or None."""
    head = body[:5000]
    is_html = head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")) or "html" in (ctype or "").lower()
    if not is_html or body.startswith(b"%PDF"):
        return None
    if DOC_URL_RE.search(urlparse(url).path):
        return "an HTML page was served for a document URL"
    if len(body) < 30_000 and WAF_RE.search(head):
        return "a block/challenge page (WAF)"
    return None


def fetch_one(session: requests.Session, url: str, year_tag: str | None, harvested_from: str | None = None) -> dict:
    """GET with up to MAX_RETRIES attempts for 429/5xx/network errors (Retry-After honoured, capped; no sleep
    after the last attempt). The manifest entry records the last outcome: http_status of the last response
    (None if the last attempt got no response), `error` for a network failure, `last_http_status` for the
    last HTTP error seen before it."""
    status, body, ctype, err, last_http, too_large = None, b"", "", None, None, False
    cap = max_download_bytes()
    for attempt in range(MAX_RETRIES):
        final = attempt == MAX_RETRIES - 1
        try:
            r = session.get(url, timeout=45, allow_redirects=True, stream=True)
            status, ctype, err = r.status_code, r.headers.get("Content-Type", ""), None
            try:
                body, too_large = (b"", False) if status == 429 or status >= 500 else read_body(r, cap)
            finally:
                getattr(r, "close", lambda: None)()
            if status == 429 or status >= 500:
                last_http = status
                if not final:
                    time.sleep(min(retry_after(r.headers.get("Retry-After")) or 2 ** (attempt + 2), RETRY_AFTER_CAP_S))
                continue
            break
        except requests.RequestException as e:
            status, body, ctype, err = None, b"", "", f"{type(e).__name__}: {e}"[:300]
            if not final:
                time.sleep(2 ** (attempt + 2))

    blocked = soft_block(url, ctype, body) if status == 200 and body else None
    digest = sha256_bytes(body) if body else None
    ok = status == 200 and len(body) > 0 and not blocked and not too_large
    base = sha256_text(url)[:16]
    ext = pick_ext(url, ctype)
    target_dir = raw_dir_for_tag(year_tag) if ok else QUARANTINE
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{base}{ext}"
    if body:  # atomic: the process worker may be scanning data/raw/ right now
        tmp = path.with_name(f".{path.name}.part")
        tmp.write_bytes(body)
        tmp.replace(path)

    entry = {
        "timestamp": now_iso(), "url": url, "http_status": status, "sha256": digest,
        "local_path": str(path.relative_to(ROOT)) if body else None, "year_tag": year_tag,
        "content_type": ctype.split(";")[0] if ctype else None, "bytes": len(body),
        "quarantined": not ok,
    }
    if harvested_from:
        entry["harvested_from"] = harvested_from
    if too_large:
        entry.update(permanent=True, error=f"too large (> {cap // 1_000_000} MB, config max_download_mb)")
    if blocked:
        entry.update(soft_block=True, error=f"soft block: {blocked}")
    if err and status is None:
        entry["error"] = err
        if last_http:
            entry["last_http_status"] = last_http
    append_jsonl(MANIFEST, entry)
    log(f"{'OK' if ok else 'QUARANTINE'} {status} {url} -> {entry['local_path']}",
        level="INFO" if ok else "WARN", kind="fetch", url=url, domain=domain_of(url),
        http_status=status, bytes=len(body), quarantined=not ok, year_tag=year_tag,
        local_path=entry["local_path"])
    return entry


def urls_from_cache_file(path: Path, domains: list[str] | dict | None) -> tuple[list[str], str | None]:
    """Result URLs of a cached search, filtered by an allowlist of domains or a fetch_policy()."""
    d = json.loads(path.read_text(encoding="utf-8"))
    urls = [r["url"] for r in d.get("results", []) if r.get("url")]
    if isinstance(domains, dict):
        urls = [u for u in urls if allowed(u, domains)]
    elif domains:
        urls = [u for u in urls if domain_matches(u, domains)]
    return urls, d.get("year_tag")


def fetch_urls(urls: list[str], tag: str | None, limit: int,
               blocked_domains: set[str] | None = None, block_after: int = 3,
               harvested_from: str | None = None) -> dict:
    """Fetch up to `limit` not-yet-fetched URLs. Domains that fail `block_after` times in a row
    (403/429/5xx/timeouts; 404/410 don't count) are added to `blocked_domains` and skipped.
    URLs whose domain used up its per-run budget are returned in `deferred` (not fetched)."""
    blocked = blocked_domains if blocked_domains is not None else set()
    policy = fetch_policy()
    done = already_fetched()
    unique = list(dict.fromkeys(urls))
    pending = [u for u in unique if u not in done]
    skipped_blocked = [u for u in pending if domain_of(u) in blocked]
    todo = [u for u in pending if domain_of(u) not in blocked][:limit]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    ok, quarantined, saved, fails = 0, 0, [], {}
    newly_blocked, deferred = [], []
    for url in todo:
        d = domain_of(url)
        if d in blocked:
            continue
        key = limit_key(url, policy)
        lim = limits_for(key, policy)
        if RUN_COUNTS[key] >= lim["max_per_run"]:
            deferred.append(url)
            continue
        if RUN_COUNTS.total():
            polite_sleep(*lim["delay_s"])
        RUN_COUNTS[key] += 1
        e = fetch_one(session, url, tag, harvested_from)
        if e["quarantined"]:
            quarantined += 1
            if e["http_status"] in (404, 410):  # missing file: not a sign the site is blocking us
                continue
            fails[d] = fails.get(d, 0) + 1
            if fails[d] >= block_after:
                blocked.add(d)
                newly_blocked.append(d)
                log(f"domain blocked after {fails[d]} consecutive failures: {d}", level="WARN",
                    kind="domain_blocked", domain=d)
        else:
            ok += 1
            fails[d] = 0
            saved.append(e["local_path"])

    return {"requested": len(unique), "skipped_cached": len(unique) - len(pending),
            "skipped_blocked": len(skipped_blocked), "fetched_ok": ok, "quarantined": quarantined,
            "newly_blocked_domains": newly_blocked, "deferred": deferred, "year_tag": tag, "saved": saved}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--from-cache-file", type=Path)
    src.add_argument("--harvest", type=Path, metavar="HTML", help="fetch document links found in a downloaded page")
    ap.add_argument("--year-tag", help="overrides the cache file's year_tag")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--domains", help="comma-separated allowlist, e.g. nseindia.com,sebi.gov.in")
    a = ap.parse_args()

    domains = [x.strip() for x in a.domains.split(",")] if a.domains else None
    m: dict = {}
    if a.url:
        urls, tag = [a.url], a.year_tag
    elif a.harvest:
        p = a.harvest if a.harvest.is_absolute() else ROOT / a.harvest
        m = next((e for e in read_jsonl(MANIFEST) if e.get("local_path") == str(p.relative_to(ROOT))), {})
        urls, tag = harvest_links(p, m.get("url", "")), a.year_tag or m.get("year_tag")
    else:
        p = a.from_cache_file if a.from_cache_file.is_absolute() else ROOT / a.from_cache_file
        urls, tag = urls_from_cache_file(p, domains)
        tag = a.year_tag or tag

    emit(fetch_urls(urls, tag, a.limit, harvested_from=m.get("url") if a.harvest else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
