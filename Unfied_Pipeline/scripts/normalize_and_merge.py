"""Normalize parsed candidate rows, reconcile them into one record per delisting event, and export the dataset.

Reads  data/processed/parsed/*.jsonl
Writes data/processed/nse_delisted_master.csv / .jsonl, priority_years_audit.csv, bse_only_delistings.csv,
       out_of_scope_delistings.csv (years outside 2006-2026), proposed_delisting_leads.csv,
       record_sources.json (which sources back which record_id: keeps ids stable across runs),
       extraction_review.csv (candidates an extractor refused, e.g. OCR garbage names: for a human, never merged)
All files are written atomically (readers never see a half-written file).

Reconciliation, in two steps:
1. Company (entity): rows with the same ISIN, the same normalized name or the same NSE symbol are one
   company, unless that would join two different ISINs. A name or symbol shared by two ISINs is
   ambiguous: rows without an ISIN are not guessed into either (flag `ambiguous_company`).
2. Event, within a company:
   - effective dates within 30 days (7 days if two specific types disagree);
   - an order date-only row up to 270 days before (or, as sources differ, 30 days after) an effective date;
   - an undated row joins the company's only event.
   Anything else is a separate event. Example: one company delisted under two symbols five years apart
   stays two records. Sources that disagree by months are not merged; the audit flags them for review.
Merging keeps the most confident source's values and fills gaps from the others:
- `source_conflicts` records every disagreeing date or type.
- `review_flags` records fields taken from NEEDS_REVIEW sources (e.g. OCR) and order dates rejected as
  implausible (more than 30 days after the effective date, or more than 2 years before it).

  python scripts/normalize_and_merge.py
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date

from common import (
    PARSED, PRIORITY_YEARS, PROCESSED, SCHEMA_FIELDS, atomic_write_text, domain_of, emit, isin_valid, log, read_jsonl,
)

CONF_RANK = {"VERIFIED_PRIMARY": 3, "CORROBORATED_SECONDARY": 2, "NEEDS_REVIEW": 1}
TYPES = {"COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT", "OTHER"}
NAME_STOP = re.compile(r"\b(LTD|LIMITED|PVT|PRIVATE|THE|CO|COMPANY|CORPN?|CORPORATION|OF)\b")  # "X of India" = "X India"
MERGE_WINDOW_DAYS = 30           # same type, or one side OTHER (unspecified)
CROSS_TYPE_WINDOW_DAYS = 7       # two specific types that disagree: only (nearly) the same date
ORDER_TO_EFFECTIVE_DAYS = 270    # an order and the delisting it ordered (verify_records.py uses the same window)
ORDER_AFTER_EFFECTIVE_DAYS = 30  # sources date the same event a few days apart: an order up to 30 days "after" still pairs
MAX_ORDER_LEAD_DAYS = 730        # a borrowed order date further before the effective date is implausible
SCOPE_YEARS = (2006, 2026)
FILL_ORDER = ["effective_delisting_date", "order_date", "company_name", "nse_symbol", "isin", "exit_or_fair_value_inr",
              "source_url", "local_artifact_path"]

REGIMES = [  # (start date inclusive, label) — newest first
    (date(2024, 9, 25), "SEBI_2024_Amendment"),
    (date(2021, 6, 10), "SEBI_2021_Regs"),
    (date(2009, 6, 10), "SEBI_2009_Regs"),
    (date(1900, 1, 1), "SEBI_2003_Guidelines"),
]


def norm_name(name: str | None) -> str:
    s = (name or "").upper()
    s = re.sub(r"[.,&()'\"\-/]", " ", s)
    s = NAME_STOP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_d(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def financial_year(d: date | None) -> str | None:
    if not d:
        return None
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY{start}-{str(start + 1)[-2:]}"


def regime_for(d: date | None) -> str | None:
    if not d:
        return None
    return next(label for start, label in REGIMES if d >= start)


def clean_isin(v) -> str | None:
    s = re.sub(r"\s+", "", str(v or "")).upper()
    return s if s and isin_valid(s) else None


def derive(r: dict) -> dict:
    """Year, FY, regime and priority flag from the record's (effective, else order) date."""
    eff, order = parse_d(r.get("effective_delisting_date")), parse_d(r.get("order_date"))
    anchor = eff or order
    year = anchor.year if anchor else None
    r.update({"order_date": order.isoformat() if order else None, "effective_delisting_date": eff.isoformat() if eff else None,
              "calendar_year": year, "financial_year": financial_year(anchor), "regulatory_regime": regime_for(anchor),
              "is_priority_year": bool((year in PRIORITY_YEARS) or (order and order.year in PRIORITY_YEARS))})
    return r


def normalize(r: dict) -> dict:
    isin = clean_isin(r.get("isin"))
    sym = (r.get("nse_symbol") or "").strip().upper() or "UNKNOWN"
    dtype = r.get("delisting_type") if r.get("delisting_type") in TYPES else "OTHER"
    name = re.sub(r"\s+", " ", (r.get("company_name") or "")).strip()
    name = re.sub(r"\s*\(Paid Up Value[^)]*\)?\s*$", "", name, flags=re.I) or None  # BSE list suffix
    out = {
        "company_name": name,
        "nse_symbol": sym,
        "isin": isin,
        "delisting_type": dtype,
        "order_date": r.get("order_date"),
        "effective_delisting_date": r.get("effective_delisting_date"),
        "exit_or_fair_value_inr": r.get("exit_or_fair_value_inr"),
        "source_url": r.get("source_url"),
        "local_artifact_path": r.get("local_artifact_path"),
        "confidence_status": r.get("confidence_status") if r.get("confidence_status") in CONF_RANK else "NEEDS_REVIEW",
        "date_source": r.get("date_source") or (("document_text" if r.get("extraction") not in ("table", "status_table")
                                                  else "table_column")
                                                 if (r.get("effective_delisting_date") or r.get("order_date")) else None),
        "_date_evidence": r.get("date_evidence"),
        "_flags": list(r.get("parse_flags") or []),
        "_key": isin or norm_name(name),
        "_name": norm_name(name),
        "_sym": sym if sym != "UNKNOWN" else None,
    }
    return derive(out)


# ---------- step 1: companies ----------------------------------------------------------------------------------
def entities(rows: list[dict]) -> tuple[list[int], set[int]]:
    """Company id per row (union-find over ISIN, then name, then symbol; never joining two ISINs), and the rows
    whose name/symbol is shared by more than one ISIN (left unassigned to either: ambiguous)."""
    parent = list(range(len(rows)))
    isins = [{r["isin"]} if r["isin"] else set() for r in rows]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb or (isins[ra] and isins[rb] and isins[ra] != isins[rb]):
            return
        parent[rb] = ra
        isins[ra] |= isins[rb]

    ambiguous: set[int] = set()
    for key in ("isin", "_name", "_sym"):
        groups: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            if r[key]:
                groups[r[key]].append(i)
        for idxs in groups.values():
            distinct = {rows[i]["isin"] for i in idxs if rows[i]["isin"]}
            if key != "isin" and len(distinct) > 1:
                loose = [i for i in idxs if not rows[i]["isin"]]
                ambiguous.update(loose)
                for i in loose[1:]:
                    union(loose[0], i)
                continue
            for i in idxs[1:]:
                union(idxs[0], i)
    return [find(i) for i in range(len(rows))], ambiguous


# ---------- step 2: events --------------------------------------------------------------------------------------
def dates_compatible(a: dict, b: dict) -> bool:
    ea, eb = parse_d(a["effective_delisting_date"]), parse_d(b["effective_delisting_date"])
    oa, ob = parse_d(a["order_date"]), parse_d(b["order_date"])
    conflict = a["delisting_type"] != b["delisting_type"] and "OTHER" not in (a["delisting_type"], b["delisting_type"])
    window = CROSS_TYPE_WINDOW_DAYS if conflict else MERGE_WINDOW_DAYS
    if ea and eb:
        return abs((ea - eb).days) <= window
    if oa and eb:
        return -ORDER_AFTER_EFFECTIVE_DAYS <= (eb - oa).days <= ORDER_TO_EFFECTIVE_DAYS
    if ob and ea:
        return -ORDER_AFTER_EFFECTIVE_DAYS <= (ea - ob).days <= ORDER_TO_EFFECTIVE_DAYS
    if oa and ob:
        return abs((oa - ob).days) <= window
    return False


def same_event(a: dict, b: dict) -> bool:
    """Two normalized rows describe the same delisting event (same company and compatible dates)."""
    if a["isin"] and b["isin"] and a["isin"] != b["isin"]:
        return False
    if not ((a["isin"] and a["isin"] == b["isin"]) or a["_name"] == b["_name"] or (a["_sym"] and a["_sym"] == b["_sym"])):
        return False
    if undated(a) or undated(b):
        return a["delisting_type"] == b["delisting_type"]
    return dates_compatible(a, b)


def undated(r: dict) -> bool:
    return not r["effective_delisting_date"] and not r["order_date"]


def nse_side(r: dict) -> bool:
    """Not a BSE document: a BSE notice dates the BSE delisting, which may differ from NSE's."""
    return not domain_of(r.get("source_url") or "").endswith("bseindia.com")


def rank(r: dict) -> tuple:
    """Best source first: confidence, then non-BSE (an event is anchored on NSE's dates, never BSE's), then dated."""
    return (CONF_RANK[r["confidence_status"]], nse_side(r), bool(r["effective_delisting_date"]),
            sum(v not in (None, "", "UNKNOWN") for k, v in r.items() if not k.startswith("_") and k != "date_source"))


def events(members: list[dict]) -> list[list[dict]]:
    members = sorted(members, key=rank, reverse=True)
    evs: list[list[dict]] = []
    for r in (x for x in members if not undated(x)):
        for ev in evs:
            if dates_compatible(ev[0], r):
                ev.append(r)
                break
        else:
            evs.append([r])
    for r in (x for x in members if undated(x)):  # no date = no event identity
        same_type = [ev for ev in evs if ev[0]["delisting_type"] == r["delisting_type"]]
        target = evs[0] if len(evs) == 1 else same_type[0] if same_type else None
        if target is None and evs:
            r = dict(r, _flags=r.get("_flags", []) + ["undated_unattached"])
        (target.append(r) if target is not None else evs.append([r]))
    return evs


# ---------- merging one event -------------------------------------------------------------------------------------
# After the schema columns: provenance and reconciliation notes.
PROVENANCE_FIELDS = ["source_count", "all_source_urls", "all_artifact_paths", "source_conflicts", "review_flags",
                     "date_source"]


def is_bse_only_source(r: dict) -> bool:
    urls = r["all_source_urls"].split(" | ") if r.get("all_source_urls") else []
    return bool(urls) and all(domain_of(u).endswith("bseindia.com") for u in urls)


def empty(v) -> bool:
    return v in (None, "", "UNKNOWN")


def merge_group(group: list[dict]) -> dict:
    group = sorted(group, key=rank, reverse=True)
    best = dict(group[0])
    best_rank = CONF_RANK[best["confidence_status"]]
    flags: list[str] = list(best.pop("_flags", []))
    conflicts: list[str] = []
    for other in group[1:]:
        for k in FILL_ORDER:
            v = other.get(k)
            if not empty(best.get(k)) or empty(v):
                continue
            if k == "order_date" and best.get("effective_delisting_date"):
                o, e = parse_d(v), parse_d(best["effective_delisting_date"])
                if not (o and e and -ORDER_AFTER_EFFECTIVE_DAYS <= (e - o).days <= MAX_ORDER_LEAD_DAYS):
                    flags.append(f"order_date_rejected:{v}({domain_of(other.get('source_url') or '')})")
                    continue
            best[k] = v
            if CONF_RANK[other["confidence_status"]] < best_rank:
                flags.append(f"{k}:needs_review_source")
        flags += other.get("_flags", [])
    specific = list(dict.fromkeys(r["delisting_type"] for r in group if r["delisting_type"] != "OTHER"))
    if best["delisting_type"] == "OTHER" and specific:
        best["delisting_type"] = specific[0]       # the most confident source that names a type
    if len(specific) > 1:
        conflicts.append("delisting_type: " + " vs ".join(specific))
    for k in ("effective_delisting_date", "order_date"):
        others = list(dict.fromkeys((r[k], domain_of(r.get("source_url") or "")) for r in group
                                    if r[k] and r[k] != best[k]))
        if others:
            conflicts.append(f"{k}: {best[k] or '-'} vs " + ", ".join(f"{v} ({d})" for v, d in others))
    derive(best)
    # where the record's date came from: the best-ranked source stating that same date (a stated column or
    # sentence beats a date read from a document header)
    key = "effective_delisting_date" if best["effective_delisting_date"] else "order_date"
    same = [r for r in group if best[key] and r[key] == best[key] and r.get("date_source")]
    same.sort(key=lambda r: r["date_source"] == "document_header")
    best["date_source"] = same[0]["date_source"] if same else None
    urls = list(dict.fromkeys(r["source_url"] for r in group if r.get("source_url")))
    if best["confidence_status"] == "NEEDS_REVIEW" and len({domain_of(u) for u in urls}) >= 2:
        best["confidence_status"] = "CORROBORATED_SECONDARY"
    best["source_count"] = len(urls)
    best["all_source_urls"] = " | ".join(urls)
    # every downloaded file behind this record (best source first), not only the one its fields came from
    best["all_artifact_paths"] = " | ".join(dict.fromkeys(
        r["local_artifact_path"] for r in group if r.get("local_artifact_path")))
    best["source_conflicts"] = "; ".join(conflicts)
    best["review_flags"] = " ".join(dict.fromkeys(flags))
    best["_sources"] = sorted({source_key(r) for r in group})
    best["record_id"] = hash_id(best)
    return best


def hash_id(r: dict, salt: str = "") -> str:
    key_date = r["effective_delisting_date"] or r["order_date"] or ""
    return hashlib.sha256(f"{r['_key']}|{key_date}|{r['delisting_type']}{salt}".encode()).hexdigest()[:16]


def source_key(r: dict) -> str:
    """One parsed row's identity across runs: where it came from, which company, which date."""
    return f"{r.get('source_url') or r.get('local_artifact_path')}|{r['_name']}|" \
           f"{r['effective_delisting_date'] or r['order_date'] or ''}"


def assign_ids(records: list[dict], previous: dict[str, str]) -> int:
    """Keep a record's id when its sources backed a record in the previous run (a better source arriving must
    not change the id of the same event); otherwise the content hash. Ids stay unique. Returns ids kept."""
    taken: set[str] = set()
    kept = 0
    for r in sorted(records, key=lambda x: (-x["source_count"], x["record_id"])):
        prior = Counter(previous[s] for s in r["_sources"] if s in previous)
        reuse = next((rid for rid, _ in prior.most_common() if rid not in taken), None)
        if reuse:
            r["record_id"] = reuse
            kept += reuse != hash_id(r)
        n = 1
        while r["record_id"] in taken:
            r["record_id"], n = hash_id(r, f"#{n}"), n + 1
        taken.add(r["record_id"])
    return kept


def load_previous_sources() -> dict[str, str]:
    p = PROCESSED / "record_sources.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}
    return {s: rid for rid, srcs in data.items() for s in srcs}


REVIEW_FIELDS = ["reason", "candidate_name", "trailing_name", "isin", "extraction", "line", "source_url",
                 "local_artifact_path"]


def render_csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def main() -> int:
    raw = []
    for p in sorted(PARSED.glob("*.jsonl")):
        raw.extend(read_jsonl(p))
    proposed_raw = [r for r in raw if r.get("record_status") == "PROPOSED"]
    raw = [r for r in raw if r.get("record_status") != "PROPOSED"]
    rows = [normalize(r) for r in raw]
    rows = [r for r in rows if r["_key"] and r["company_name"]]  # a record needs a company name
    dropped = len(raw) - len(rows)

    ent, ambiguous = entities(rows)
    for i in ambiguous:
        rows[i]["_flags"] = rows[i].get("_flags", []) + ["ambiguous_company"]
    by_entity: dict[int, list[dict]] = defaultdict(list)
    for i, r in enumerate(rows):
        r["_entity"] = ent[i]
        by_entity[ent[i]].append(r)
    groups = [ev for members in by_entity.values() for ev in events(members)]

    merged = [merge_group(g) for g in groups]
    for g, r in zip(groups, merged):
        r["_entity"] = g[0]["_entity"]
    ids_kept = assign_ids(merged, load_previous_sources())
    merged.sort(key=lambda r: (r["calendar_year"] or 0, r["company_name"] or "", r["record_id"]))
    # BSE-only delistings (no NSE symbol, only BSE sources) are not NSE delistings: many of these companies were
    # never NSE-listed, and a BSE event that did not merge into a company's NSE event is BSE's own delisting.
    bse_only = [r for r in merged if is_bse_only_source(r) and r["nse_symbol"] == "UNKNOWN"]
    bse_ids = {r["record_id"] for r in bse_only}
    rest = [r for r in merged if r["record_id"] not in bse_ids]
    out_of_scope = [r for r in rest if r["calendar_year"] and not SCOPE_YEARS[0] <= r["calendar_year"] <= SCOPE_YEARS[1]]
    oos_ids = {r["record_id"] for r in out_of_scope}
    master = [r for r in rest if r["record_id"] not in oos_ids]

    fields = SCHEMA_FIELDS + PROVENANCE_FIELDS
    priority = [r for r in master if r["is_priority_year"]]
    atomic_write_text(PROCESSED / "nse_delisted_master.csv", render_csv(master, fields))
    atomic_write_text(PROCESSED / "nse_delisted_master.jsonl", "".join(
        json.dumps({k: r.get(k) for k in fields}, ensure_ascii=False) + "\n" for r in master))
    atomic_write_text(PROCESSED / "priority_years_audit.csv", render_csv(priority, fields))
    atomic_write_text(PROCESSED / "bse_only_delistings.csv", render_csv(bse_only, fields))
    atomic_write_text(PROCESSED / "out_of_scope_delistings.csv", render_csv(out_of_scope, fields))
    atomic_write_text(PROCESSED / "record_sources.json", json.dumps(
        {r["record_id"]: r["_sources"] for r in merged}, indent=0, sort_keys=True))

    # Proposed-delisting leads: companies to verify later (not part of the master dataset).
    known = {("I", r["isin"]) for r in merged if r["isin"]} | {("N", r["_name"]) for r in merged} | \
            {("S", r["_sym"]) for r in merged if r["_sym"]}
    leads: dict = {}
    for r in (normalize(x) for x in proposed_raw):
        if r["_key"] and r["_key"] not in leads:
            r["already_in_master"] = bool({("I", r["isin"]), ("N", r["_name"]), ("S", r["_sym"])} & known)
            leads[r["_key"]] = r
    review_path = PARSED / "_review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    review_rows = [c for _f, cands in sorted(review.items()) for c in cands]
    atomic_write_text(PROCESSED / "extraction_review.csv", render_csv(review_rows, REVIEW_FIELDS))
    lead_fields = ["company_name", "nse_symbol", "isin", "source_url", "local_artifact_path", "already_in_master"]
    atomic_write_text(PROCESSED / "proposed_delisting_leads.csv",
                      render_csv(sorted(leads.values(), key=lambda r: r["company_name"] or ""), lead_fields))

    summary = {
        "parsed_rows_in": len(raw), "proposed_leads": len(leads), "bse_only": len(bse_only),
        "out_of_scope_years": len(out_of_scope), "dropped_no_key": dropped, "records_out": len(master),
        "duplicates_merged": len(rows) - len(merged), "priority_records": len(priority),
        "records_with_source_conflicts": sum(bool(r["source_conflicts"]) for r in master),
        "records_with_review_flags": sum(bool(r["review_flags"]) for r in master),
        "ambiguous_company_rows": len(ambiguous), "extraction_review_candidates": len(review_rows), "record_ids_kept_from_previous_run": ids_kept,
        "by_confidence": {c: sum(r["confidence_status"] == c for r in master) for c in CONF_RANK},
    }
    log(f"merge in={len(raw)} out={len(master)} priority={len(priority)}", kind="merge", **summary)
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
