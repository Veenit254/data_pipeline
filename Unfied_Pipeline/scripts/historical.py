"""Historical focus: companies delisted 15-20 years ago (config "focus": {"years_ago": [15, 20]}).

Two steps, in this order, ahead of every other phase of the query plan:
1. Proof. Find official or archive evidence of each delisting:
   - phase historical_proof: per window year, queries for NSE/BSE/SEBI delisting orders, notices and lists;
   - phase historical_company_proof: per company in the window whose records have no official/archive
     source yet: "<name>" delisted <year> on NSE / BSE / SEBI, its ISIN.
2. Supporting data. Once a company is proven (phase historical_company_support): exit offer / public
   announcement, the reason-specific document (amalgamation scheme, liquidation order), the last annual
   report before delisting. Documents found are tagged with the company in the docket, and
   run_limits.max_docs_per_company caps how many are fetched per company.
Company queries are generated from data/processed/nse_delisted_master.csv on every run (not stored in the
config). Official URLs from the window that are gone (404/410) are looked up in the Wayback Machine.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date

from common import CONFIG, PROCESSED, domain_matches

OFFICIAL_DOMAINS = ("nseindia.com", "bseindia.com", "sebi.gov.in", "ibbi.gov.in", "nclt.gov.in", "mca.gov.in")
ARCHIVE_DOMAINS = ("web.archive.org", "archive.org")
NEWS_DOMAINS = ("business-standard.com", "economictimes.indiatimes.com", "moneycontrol.com", "livemint.com",
                "thehindubusinessline.com", "financialexpress.com", "ndtvprofit.com", "thehindu.com",
                "timesofindia.indiatimes.com", "reuters.com", "bloomberg.com")
SUFFIX_RE = re.compile(r"\b(limited|ltd\.?|the|pvt\.?|private|co\.?|company|corporation|corp\.?)\b", re.I)
PROOF_PHASES = ("historical_proof", "historical_company_proof")
SUPPORT_PHASE = "historical_company_support"


def window(today: date | None = None) -> tuple[int, int]:
    """(first year, last year) of the focus window, e.g. (2006, 2011) in 2026."""
    try:
        f = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8")).get("focus") or {}
    except (OSError, ValueError):
        f = {}
    lo, hi = sorted(f.get("years_ago", [15, 20]))
    y = (today or date.today()).year
    return y - hi, y - lo


def in_window(year, win: tuple[int, int] | None = None) -> bool:
    win = win or window()
    try:
        return win[0] <= int(str(year)[:4]) <= win[1]
    except (TypeError, ValueError):
        return False


def tier(url: str) -> int:
    """0 official or archive, 1 other sites, 2 news: historical records before recent news."""
    if domain_matches(url, OFFICIAL_DOMAINS + ARCHIVE_DOMAINS):
        return 0
    return 2 if domain_matches(url, NEWS_DOMAINS) else 1


def company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", SUFFIX_RE.sub(" ", (name or "").lower()))


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", SUFFIX_RE.sub(" ", name or "")).strip(" .,&-")


def proven(rec: dict) -> bool:
    urls = [u for u in (rec.get("all_source_urls") or rec.get("source_url") or "").split(" | ") if u]
    return any(domain_matches(u, OFFICIAL_DOMAINS + ARCHIVE_DOMAINS) for u in urls)


def companies(win: tuple[int, int] | None = None, master_rows: list[dict] | None = None) -> list[dict]:
    """Companies in the window, oldest delisting first, with whether official/archive proof exists."""
    win = win or window()
    if master_rows is None:
        p = PROCESSED / "nse_delisted_master.csv"
        master_rows = list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []
    out: dict[str, dict] = {}
    for r in master_rows:
        if not in_window(r.get("calendar_year"), win) or not r.get("company_name"):
            continue
        k = company_key(r["company_name"])
        c = out.setdefault(k, {"company": k, "name": r["company_name"], "year": int(r["calendar_year"]),
                               "isin": r.get("isin") or "", "symbol": r.get("nse_symbol") or "",
                               "type": r.get("delisting_type") or "", "proven": False, "records": 0})
        c["records"] += 1
        c["proven"] = c["proven"] or proven(r)
        c["isin"] = c["isin"] or r.get("isin") or ""
    return sorted(out.values(), key=lambda c: (c["year"], c["name"]))


def proof_queries(year: int) -> list[str]:
    return [f"NSE delisted companies list {year}",
            f"compulsory delisting order {year} site:nseindia.com",
            f"BSE delisting notice {year} site:bseindia.com",
            f"SEBI order delisting {year}",
            f"voluntary delisting exit offer public announcement {year}"]


def company_queries(c: dict) -> list[tuple[str, str]]:
    """(phase, query) for one company: proof first; supporting documents once proven."""
    n, y = clean_name(c["name"]), c["year"]
    if not c["proven"]:
        qs = [f'"{n}" delisted {y} site:nseindia.com', f'"{n}" delisting {y} site:bseindia.com',
              f'"{n}" delisting SEBI order {y}']
        if c["isin"]:
            qs.append(f'{c["isin"]} delisting')
        return [("historical_company_proof", q) for q in qs]
    t = c["type"].upper()
    reason = (f'"{n}" scheme of amalgamation' if "MERGER" in t or "AMALGAM" in t
              else f'"{n}" liquidation order' if "IBC" in t or "LIQUID" in t
              else f'"{n}" compulsory delisting order' if "COMPULSORY" in t
              else f'"{n}" delisting offer letter exit price')
    return [(SUPPORT_PHASE, q) for q in (f'"{n}" delisting public announcement {y}', reason,
                                         f'"{n}" annual report {y - 1}')]


def items(state: dict, win: tuple[int, int] | None = None, master_rows: list[dict] | None = None) -> list[dict]:
    """Query items for collect.py, in order: window-year proof, company proof, company support."""
    win = win or window()
    out = [{"phase": "historical_proof", "query": q, "year_tag": str(y), "pages": 2}
           for y in range(win[0], win[1] + 1) for q in proof_queries(y)]
    comps = companies(win, master_rows)
    for phase in ("historical_company_proof", SUPPORT_PHASE):
        for c in comps:
            for ph, q in company_queries(c):
                if ph == phase:
                    out.append({"phase": ph, "query": q, "year_tag": str(c["year"]), "pages": 1,
                                "company": c["company"], "company_name": c["name"]})
    seen, uniq = set(), []
    for it in out:
        if it["query"] not in seen:
            seen.add(it["query"])
            uniq.append(it)
    return uniq


def coverage(win: tuple[int, int] | None = None, docs_by_company: dict | None = None) -> dict:
    comps = companies(win)
    docs = docs_by_company or {}
    return {"window": list(win or window()), "companies": len(comps),
            "proven": sum(c["proven"] for c in comps), "unproven": sum(not c["proven"] for c in comps),
            "with_supporting_docs": sum(1 for c in comps if docs.get(c["company"]))}


def archive_copy(session, url: str, year=None) -> str | None:
    """The Wayback Machine's closest snapshot of `url` (raw bytes form, id_), or None."""
    ts = f"{str(year)[:4]}0701" if year and str(year)[:4].isdigit() else None
    try:
        r = session.get("https://archive.org/wayback/available", params={"url": url, **({"timestamp": ts} if ts else {})},
                        timeout=20)
        snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
    except Exception:
        return None
    if not snap.get("available") or str(snap.get("status", "200")) != "200" or not snap.get("url"):
        return None
    return re.sub(r"^http://", "https://", re.sub(r"/web/(\d+)/", r"/web/\1id_/", snap["url"], count=1))
