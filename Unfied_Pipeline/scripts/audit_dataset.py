"""Data-quality audit of data/processed/nse_delisted_master.csv.

Errors (exit 1): duplicate record_id, malformed ISIN, invalid enum values, rows without a source,
rows from non-listing pages (brokers/members), missing backing artifact, a field label or identifier as the
company name ("ISIN", "Symbol: X | ISIN", "ISIN of ...").
Warnings: no date at all, year outside 2006–2026, suspicious company names, order date after the effective
date, sources that disagree (source_conflicts), fields from review-grade sources (review_flags), and
possible duplicate events: two records of the same company (linked by ISIN, normalized name or symbol,
whatever the type) whose dates are within 3 years or that have no date. A company with two genuine
delistings 5 years apart is not flagged.

Writes data/metadata/dataset_audit.json (also included in collection_metadata.json), or --out FILE
(read-only checks, e.g. scripts/check.sh, write elsewhere so the tracked file is untouched).

  python scripts/audit_dataset.py
  python scripts/audit_dataset.py --out /tmp/audit.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from common import METADATA, PROCESSED, ROOT, atomic_write_text, emit, isin_equity, isin_valid, now_iso

TYPES = {"COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT", "OTHER"}
CONFIDENCE = {"VERIFIED_PRIMARY", "CORROBORATED_SECONDARY", "NEEDS_REVIEW"}
NON_LISTING_RE = re.compile(r"member|broker|defaulter|expelled|arbitrat|complaint|debarred", re.I)
BAD_NAME_RE = re.compile(r"^(nan|nat|none|total|sr\.? ?no\.?|company name|name of the company)$|^\W*$|^\d+$", re.I)
MAX_EXAMPLES = 10
NEAR_EVENT_DAYS = 1095   # two "events" of one company within 3 years: a human should look
LABEL_NAME_RE = re.compile(r"^(?:ISIN|SYMBOL|CIN|SCRIP\s*CODE|NSE\s*SYMBOL|NAME)\b|\||\bSymbol\s*:|\bIN[E9][A-Z0-9]{8}[0-9]\b",
                           re.I)
NAME_STOP = re.compile(r"\b(LTD|LIMITED|PVT|PRIVATE|THE|CO|COMPANY|CORPN?|CORPORATION|OF)\b")  # "X of India" = "X India"


def norm_name(name: str) -> str:  # same normalization as normalize_and_merge.py
    s = re.sub(r"[.,&()'\"\-/]", " ", (name or "").upper())
    return re.sub(r"\s+", " ", NAME_STOP.sub(" ", s)).strip()


def to_date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def companies(rows: list[dict]) -> list[int]:
    """Company id per row: rows sharing an ISIN, a normalized name or an NSE symbol (union-find)."""
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for key in (lambda r: r.get("isin") or None, lambda r: norm_name(r.get("company_name")) or None,
                lambda r: r.get("nse_symbol") if r.get("nse_symbol") not in (None, "", "UNKNOWN") else None):
        first: dict = {}
        for i, r in enumerate(rows):
            k = key(r)
            if k:
                parent[find(i)] = find(first.setdefault(k, i))
    return [find(i) for i in range(len(rows))]


def audit(rows: list[dict]) -> dict:
    errors: dict[str, list] = defaultdict(list)
    warnings: dict[str, list] = defaultdict(list)

    def flag(bucket, check, r):
        bucket[check].append({"record_id": r.get("record_id"), "company_name": r.get("company_name"),
                              "source_url": r.get("source_url")})

    ids = Counter(r.get("record_id") for r in rows)
    by_company = defaultdict(list)
    # A fresh clone has the dataset but not the raw downloads (data/raw/ is git-ignored): that is not an error.
    raw_present = any(p.is_file() and not p.name.startswith(".") for p in (ROOT / "data" / "raw").rglob("*"))
    for r in rows:
        if ids[r.get("record_id")] > 1:
            flag(errors, "duplicate_record_id", r)
        if r.get("isin") and not isin_valid(r["isin"]):
            flag(errors, "malformed_isin", r)
        elif r.get("isin") and not isin_equity(r["isin"]):
            flag(errors, "debt_security_isin", r)
        if r.get("delisting_type") not in TYPES:
            flag(errors, "invalid_delisting_type", r)
        if r.get("confidence_status") not in CONFIDENCE:
            flag(errors, "invalid_confidence_status", r)
        if not r.get("source_url"):
            flag(errors, "missing_source_url", r)
        elif NON_LISTING_RE.search(r["source_url"]):
            flag(errors, "non_listing_source", r)
        lap = r.get("local_artifact_path")
        extra = [x for x in (r.get("all_artifact_paths") or "").split(" | ") if x]
        if not lap:
            flag(errors, "missing_artifact", r)
        elif not all((ROOT / x).exists() for x in [lap, *extra]):
            flag(errors if raw_present else warnings, "missing_artifact" if raw_present else "raw_files_not_downloaded", r)
        if not r.get("order_date") and not r.get("effective_delisting_date"):
            flag(warnings, "no_date", r)
        y = r.get("calendar_year")
        if y and not 2006 <= int(y) <= 2026:
            flag(warnings, "year_out_of_range", r)
        name = (r.get("company_name") or "").strip()
        if LABEL_NAME_RE.search(name):
            flag(errors, "label_as_company_name", r)
        elif BAD_NAME_RE.search(name) or len(name) < 3:
            flag(warnings, "suspicious_company_name", r)
        o, e = to_date(r.get("order_date")), to_date(r.get("effective_delisting_date"))
        if o and e and o > e:
            flag(warnings, "order_after_effective", r)
        if r.get("source_conflicts"):
            flag(warnings, "source_conflicts", r)
        if r.get("review_flags"):
            flag(warnings, "review_flags", r)
    for cid, r in zip(companies(rows), rows):
        by_company[cid].append(r)
    for group in by_company.values():
        dup: set[int] = set()
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                di = to_date(group[i].get("effective_delisting_date") or group[i].get("order_date"))
                dj = to_date(group[j].get("effective_delisting_date") or group[j].get("order_date"))
                if not di or not dj or abs((di - dj).days) <= NEAR_EVENT_DAYS:
                    dup |= {i, j}
        for i in sorted(dup):
            flag(warnings, "possible_duplicate_event", group[i])

    def summarize(d):
        return {k: {"count": len(v), "examples": v[:MAX_EXAMPLES]} for k, v in sorted(d.items())}

    return {"generated_at": now_iso(), "records": len(rows),
            "error_count": sum(len(v) for v in errors.values()),
            "warning_count": sum(len(v) for v in warnings.values()),
            "errors": summarize(errors), "warnings": summarize(warnings)}


def load_rows() -> list[dict]:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(out: Path | None = None) -> dict:
    result = audit(load_rows())
    target = Path(out) if out else METADATA / "dataset_audit.json"
    atomic_write_text(target, json.dumps(result, indent=1))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--out", type=Path, help="write the report here instead of data/metadata/dataset_audit.json")
    a = ap.parse_args()
    r = run(a.out)
    emit({"records": r["records"], "errors": {k: v["count"] for k, v in r["errors"].items()},
          "warnings": {k: v["count"] for k, v in r["warnings"].items()},
          "report": str(a.out) if a.out else "data/metadata/dataset_audit.json"})
    return 1 if r["error_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
