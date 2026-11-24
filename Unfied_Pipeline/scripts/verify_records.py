"""Cross-check every master record: is the company really delisted, was it listed on NSE/BSE, and
is the delisting year right?

Checks
- Still listed? The record's symbol, ISIN or name is looked up in NSE's current lists of listed
  companies (data/reference/nse_equity_listed.csv, nse_sme_listed.csv; EQUITY_L / SME_EQUITY_L).
  Listed since before the delisting date -> STILL_LISTED (contradiction). Listed after it -> the
  company was relisted or the symbol was reused (RELISTED_OR_SYMBOL_REUSED: check by hand).
- Exchange evidence: which exchanges' documents back the record (all_source_urls).
- Year: NSE publishes two official delisted lists (delisted.xlsx and the older DelistedCos_Buy_Sell.xls);
  when both name the company, their dates must agree. Order date must not be after the effective
  date (or more than 2 years before it); dates in the future or before 1995 are wrong; the same
  company with two records of the same type in different years is suspicious.

Writes data/processed/verification_report.csv (one row per record) and prints a JSON summary.
  python scripts/verify_records.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter, defaultdict
from datetime import date

from common import atomic_write_text, DATA, PARSED, PROCESSED, domain_of, emit, log, read_jsonl

REFERENCE = DATA / "reference"
OFFICIAL_LISTS = {"delisted.xlsx": "master_lists__d07b1e746ddd066e.jsonl",
                  "DelistedCos_Buy_Sell.xls": "master_lists__62a32760866c0bbb.jsonl"}
OFFICIAL_LISTS_KEYS = list(OFFICIAL_LISTS)
SUFFIX_RE = re.compile(r"\b(LIMITED|LTD|THE|CO|COMPANY|CORPORATION|CORP|INDIA|OF|AND|PVT|PRIVATE)\b")


def norm_name(name: str) -> str:
    # words in brackets stay: "Ponni Sugars (Orissa)" is not "Ponni Sugars (Erode)"
    n = (name or "").upper().replace("(", " ").replace(")", " ").replace("&", " AND ")
    return re.sub(r"[^A-Z0-9]", "", SUFFIX_RE.sub(" ", n))


def to_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def listed_date(s: str) -> date | None:
    from datetime import datetime
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def load_listed() -> dict[str, dict]:
    """Currently listed NSE companies, indexed by symbol, ISIN and normalised name."""
    idx: dict[str, dict] = {}
    for fname, board in (("nse_equity_listed.csv", "main"), ("nse_sme_listed.csv", "SME")):
        p = REFERENCE / fname
        if not p.exists():
            continue
        for r in csv.DictReader(p.open(encoding="utf-8-sig")):
            r = {k.strip().upper().replace("_", " "): (v or "").strip() for k, v in r.items() if k}
            e = {"symbol": r.get("SYMBOL"), "name": r.get("NAME OF COMPANY"), "isin": r.get("ISIN NUMBER"),
                 "listed": listed_date(r.get("DATE OF LISTING", "")), "board": board}
            for k in (f"S:{e['symbol']}", f"I:{e['isin']}", f"N:{norm_name(e['name'])}"):
                idx.setdefault(k, e)
    return idx


def official_dates() -> dict[str, dict[str, list[date]]]:
    """Normalised company name -> {official list: its delisting dates} (a company can have several)."""
    out: dict[str, dict[str, list[date]]] = defaultdict(lambda: defaultdict(list))
    for label, fname in OFFICIAL_LISTS.items():
        for r in read_jsonl(PARSED / fname) if (PARSED / fname).exists() else []:
            d = to_date(r.get("effective_delisting_date"))
            if d and r.get("record_status") != "PROPOSED" and d not in out[norm_name(r["company_name"])][label]:
                out[norm_name(r["company_name"])][label].append(d)
    return out


def official_reasons() -> dict[str, str]:
    """NSE symbol -> 'Type of Delisting' from delisted.xlsx (first sheet)."""
    p = DATA / "raw" / "master_lists" / "d07b1e746ddd066e.xlsx"
    if not p.exists():
        return {}
    import pandas as pd
    df = pd.read_excel(p, sheet_name=0)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return {str(r["symbol"]).strip(): str(r["type of delisting"]).strip() for _, r in df.iterrows()}


def split_events(rows: list[dict]) -> set[str]:
    """Order-only records (a Delisting Committee order/letter, no effective date) whose company has a
    dated record up to 9 months after the order: one delisting, split in two."""
    dated = defaultdict(list)
    for r in rows:
        if to_date(r["effective_delisting_date"]):
            dated[norm_name(r["company_name"])].append(to_date(r["effective_delisting_date"]))
    out = set()
    for r in rows:
        o = to_date(r["order_date"])
        if o and not r["effective_delisting_date"] and any(
                0 <= (d - o).days <= 270 for d in dated[norm_name(r["company_name"])]):
            out.add(r["record_id"])
    return out


def check(r: dict, listed: dict, official: dict, same_company: Counter, reasons: dict, split: set) -> dict:
    eff, order = to_date(r["effective_delisting_date"]), to_date(r["order_date"])
    when = eff or order
    urls = [u for u in (r.get("all_source_urls") or r["source_url"]).split(" | ") if u]
    domains = {domain_of(u) for u in urls}
    nse_ev = any(d.endswith("nseindia.com") for d in domains)
    bse_ev = any(d.endswith("bseindia.com") for d in domains)
    sym = r["nse_symbol"] if r["nse_symbol"] not in ("", "UNKNOWN") else None
    name = norm_name(r["company_name"])
    flags, notes = [], []
    if reasons.get(sym or "", "").lower().startswith("exit from itp"):
        flags.append("NOT_A_DELISTING")
        notes.append("NSE lists it as 'Exit from ITP platform': it left the Institutional Trading Platform, "
                     "it was not delisted from NSE")
    if r["record_id"] in split:
        flags.append("SPLIT_EVENT")
        notes.append("same delisting as the company's record with the effective date (this one has only the order date)")

    # 1. still listed on NSE?
    hit, how = None, None
    for key, label in ((f"I:{r['isin']}", "isin"), (f"S:{sym}", "symbol"), (f"N:{name}", "name")):
        if key in listed and key[2:]:
            hit, how = listed[key], label
            break
    listing_status = "NOT_LISTED_NOW"
    if hit:
        same_co = norm_name(hit["name"]) == name or how == "isin"
        if hit["listed"] and when and hit["listed"] > when:
            listing_status = "RELISTED_OR_SYMBOL_REUSED"
            notes.append(f"NSE {hit['board']} lists {hit['symbol']} ({hit['name']}) since {hit['listed']}"
                         + ("" if same_co else " — different company, symbol reused"))
            if same_co:
                flags.append("RELISTED")
        elif same_co:
            listing_status = "STILL_LISTED"
            flags.append("STILL_LISTED_ON_NSE")
            notes.append(f"matched by {how}: {hit['symbol']} listed since {hit['listed']}")
        else:
            listing_status = "SYMBOL_IN_USE_BY_OTHER_COMPANY"
            notes.append(f"symbol {hit['symbol']} now belongs to {hit['name']}")

    # 2. year
    off = official.get(name, {})
    off_dates = sorted({d for ds in off.values() for d in ds})
    year_status = "NO_OFFICIAL_LIST_DATE"
    if off_dates:
        if r["record_id"] in split and eff is None:
            year_status = "ORDER_DATE_ONLY"
        elif when and any(abs((d - when).days) <= 31 for d in off_dates):
            year_status = "MATCHES_OFFICIAL_LIST"
        elif when:
            year_status = "DIFFERS_FROM_OFFICIAL_LIST"
            flags.append("YEAR_MISMATCH")
            notes.append("official list date(s): " + ", ".join(f"{k} {d}" for k, ds in off.items() for d in ds))
        if len(off) > 1 and not any(abs((a - b).days) <= 31 for a in off[OFFICIAL_LISTS_KEYS[0]]
                                    for b in off[OFFICIAL_LISTS_KEYS[1]]):
            flags.append("OFFICIAL_LISTS_DISAGREE")
            notes.append("NSE lists disagree: " + ", ".join(f"{k} {d}" for k, ds in off.items() for d in ds))
    if not when:
        flags.append("NO_DATE")
    elif when > date.today() or when.year < 1995:
        flags.append("IMPLAUSIBLE_DATE")
    if eff and order and (order > eff or (eff - order).days > 730):
        flags.append("ORDER_VS_EFFECTIVE_DATE")
        notes.append(f"order {order}, effective {eff}")
    if same_company[(name, r["delisting_type"])] > 1:
        flags.append("SAME_COMPANY_TWICE")

    if {"STILL_LISTED_ON_NSE", "YEAR_MISMATCH", "IMPLAUSIBLE_DATE", "NOT_A_DELISTING"} & set(flags):
        verdict = "PROBLEM"
    elif flags:
        verdict = "CHECK"
    elif off_dates and year_status == "MATCHES_OFFICIAL_LIST":
        verdict = "CONFIRMED"
    else:
        verdict = "CONFIRMED_SINGLE_SOURCE" if nse_ev or bse_ev else "UNVERIFIED"
    return {"record_id": r["record_id"], "company_name": r["company_name"], "nse_symbol": r["nse_symbol"],
            "delisting_type": r["delisting_type"], "delisting_date": when.isoformat() if when else "",
            "year": when.year if when else "", "verdict": verdict, "flags": " ".join(flags),
            "nse_evidence": nse_ev, "bse_evidence": bse_ev, "listing_status": listing_status,
            "year_status": year_status, "official_lists": " | ".join(sorted(off)), "sources": len(urls),
            "notes": "; ".join(notes)}


def main() -> int:
    rows = list(csv.DictReader((PROCESSED / "nse_delisted_master.csv").open(encoding="utf-8")))
    listed, official = load_listed(), official_dates()
    if not listed:
        emit({"error": "data/reference/nse_equity_listed.csv missing (NSE EQUITY_L.csv)"})
        return 1
    same_company = Counter((norm_name(r["company_name"]), r["delisting_type"]) for r in rows)
    reasons, split = official_reasons(), split_events(rows)
    out = [check(r, listed, official, same_company, reasons, split) for r in rows]
    path = PROCESSED / "verification_report.csv"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(out[0]), lineterminator="\r\n")
    w.writeheader()
    w.writerows(sorted(out, key=lambda x: (x["verdict"] != "PROBLEM", x["verdict"] != "CHECK", x["company_name"])))
    atomic_write_text(path, buf.getvalue())
    flags = Counter(fl for o in out for fl in o["flags"].split())
    summary = {"records": len(out), "verdict": dict(Counter(o["verdict"] for o in out)), "flags": dict(flags),
               "listing_status": dict(Counter(o["listing_status"] for o in out)),
               "year_status": dict(Counter(o["year_status"] for o in out)),
               "exchange_evidence": {"nse": sum(o["nse_evidence"] for o in out),
                                     "bse": sum(o["bse_evidence"] for o in out),
                                     "both": sum(o["nse_evidence"] and o["bse_evidence"] for o in out)},
               "report": str(path.relative_to(DATA.parent))}
    log(f"verification: {summary['verdict']}", kind="verify", **{k: summary[k] for k in ("verdict", "flags")})
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
