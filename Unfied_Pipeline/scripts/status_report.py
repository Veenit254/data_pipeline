"""Concise progress dashboard by year and delisting category.

Examples:
  python scripts/status_report.py
  python scripts/status_report.py --year 2017 --needs-review
  python scripts/status_report.py --json
  python scripts/status_report.py --watch 30      # live dashboard, refresh every 30 s (Ctrl-C to stop)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict

import build_metadata
import report_issue
import web_search
from common import PRIORITY_YEARS, PROCESSED

TYPES = ["COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT", "OTHER"]
SHORT = {"COMPULSORY": "COMP", "VOLUNTARY_EXIT_OFFER": "VOL_EX", "VOLUNTARY_NO_EXIT": "VOL_NO",
         "AMALGAMATION_MERGER": "MERGE", "IBC_NCLT": "IBC", "OTHER": "OTHER"}


def load_master() -> list[dict]:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def bar(p: float, width: int = 20) -> str:
    filled = round(width * min(max(p, 0), 100) / 100)
    return "█" * filled + "░" * (width - filled)


def print_indicators(meta: dict) -> None:
    ind = meta["indicators"]
    print("=" * 78)
    print(f" NSE DELISTING COLLECTION — {meta['generated_at']}")
    print("=" * 78)
    for label, val, extra in [
        ("Query plan", ind["query_plan_complete_pct"], f"{ind['queries_done']}/{ind['queries_total']} queries"),
        ("Priority grid", ind["priority_grid_coverage_pct"], "year × type cells with ≥1 record"),
        ("Fetch success", ind["fetch_success_rate_pct"], f"{ind['artifacts_downloaded']} files downloaded"),
        ("Verified primary", ind["verified_primary_pct"], f"{ind['needs_review']} rows NEEDS_REVIEW"),
        ("ISIN completeness", ind["isin_completeness_pct"], "rows with an ISIN"),
    ]:
        print(f" {label:<18} {bar(val)} {val:>5.1f}%  {extra}")
    print(f" Records: {ind['records_total']} (priority years: {ind['priority_year_records']}) | "
          f"files with rows: {ind['artifacts_with_rows']}, zero-row: {ind['artifacts_zero_rows']}, "
          f"off-topic: {ind['artifacts_irrelevant']} | "
          f"blocked domains: {ind['blocked_domains']} | proposed leads: {ind['proposed_delisting_leads']}"
          f" | BSE-only (separate file): {ind.get('bse_only_delistings', 0)}")
    lr = ind.get("last_run")
    if lr:
        print(f" Last run {lr['run_id']}: {lr['status']} ({lr['stop_reason']}), {lr['queries_done']} queries, "
              f"{lr['fetched_ok']} fetched, +{lr['new_records'] or 0} records, {lr['duration_s']} s")
    print(" Phases: " + "  ".join(f"{n} {p['complete_pct']:.0f}%" for n, p in meta["queries"]["phases"].items()))
    d = ind["disk"]
    mark = {"ok": "✓", "warn": "⚠ WARNING", "critical": "✖ CRITICAL"}[d["level"]]
    print(f" Disk: data/ {d['data_gb']} GB (warn at {d['warn_data_gb']:g} GB) | free {d['free_gb']} GB "
          f"(stop below {d['min_free_gb']:g} GB) {mark}")
    if "source_domains" in ind:
        nd = ind["new_domains_with_rows"]
        print(f" Websites: {ind['source_domains']} fetched from, {ind['new_source_domains']} new"
              + (f" — new with rows: {', '.join(nd[:6])}" if nd else "") + " (data/metadata/domain_registry.json)")
    hist = (ind.get("docket") or {}).get("historical")
    if hist:
        print(f" Historical focus {hist['window'][0]}–{hist['window'][1]}: {hist['companies']} companies, "
              f"{hist['proven']} with official/archive proof, {hist['unproven']} unproven, "
              f"{hist['with_supporting_docs']} with supporting documents")
    dk = ind.get("docket")
    if dk:
        st = dk["by_status"]
        print(f" Docket: {dk['fetch_ready_now']} ready to fetch, {st.get('failed', 0)} retrying, "
              f"{dk['awaiting_parse']} awaiting parse, {st.get('parsed', 0)} parsed, {st.get('dead', 0)} dead, "
              f"{st.get('parked', 0)} parked ({dk['evidence_unfetched']} with delisting evidence) "
              f"— scripts/docket.py --stats")
    print(f" Data audit: {ind['audit_errors']} errors, {ind['audit_warnings']} warnings "
          f"(data/metadata/dataset_audit.json)")
    eng = web_search.Search().report()
    nxt = min((v["minutes_left"] for v in eng["benched"].values()), default=0)
    provs = ", ".join(f"{n} {'✓' if v['ready'] else '✗'}" for n, v in eng["providers"].items())
    print(f" Search: {provs} | units {len(eng['usable'])} usable, {len(eng['benched'])} benched"
          + (f" (next back in {nxt} min)" if not eng["usable"] and eng["benched"] else "")
          + " — details: scripts/web_search.py --engines")
    open_issues = [i for i in report_issue.load() if i["status"] == "open"]
    print(f" Reported issues: {len(open_issues)} open (logs/issues.jsonl)"
          + (f" — latest: #{open_issues[-1]['id']} {open_issues[-1]['summary'][:70]}" if open_issues else ""))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--year", type=int)
    ap.add_argument("--needs-review", action="store_true", help="list NEEDS_REVIEW rows (max 25)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="refresh the dashboard every N seconds")
    a = ap.parse_args()

    if a.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")
                report(a)
                time.sleep(a.watch)
        except KeyboardInterrupt:
            return 0
    return report(a)


def report(a) -> int:
    meta = build_metadata.build()
    rows = load_master()

    grid: dict[str, Counter] = defaultdict(Counter)
    conf: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        y = r.get("calendar_year") or "unknown"
        grid[y][r["delisting_type"]] += 1
        conf[y][r["confidence_status"]] += 1
    years = sorted(set(grid) | {str(y) for y in PRIORITY_YEARS}, key=lambda y: (y == "unknown", y))
    if a.year:
        years = [str(a.year)]

    if a.json:
        print(json.dumps({"indicators": meta["indicators"],
                          "open_issues": sum(i["status"] == "open" for i in report_issue.load()),
                          "by_year": {y: dict(grid[y]) for y in years},
                          "confidence_by_year": {y: dict(conf[y]) for y in years}}, default=str))
        return 0

    print_indicators(meta)
    hdr = f"{'YEAR':<9}" + "".join(f"{SHORT[t]:>8}" for t in TYPES) + f"{'TOTAL':>7}{'VERIF':>7}{'REVIEW':>7}"
    print(hdr)
    print("-" * len(hdr))
    for y in years:
        mark = "*" if y.isdigit() and int(y) in PRIORITY_YEARS else " "
        g = grid[y]
        print(f"{y + mark:<9}" + "".join(f"{g[t]:>8}" for t in TYPES)
              + f"{sum(g.values()):>7}{conf[y]['VERIFIED_PRIMARY']:>7}{conf[y]['NEEDS_REVIEW']:>7}")
    print("* = priority year")
    gaps = [f"{y}:{SHORT[t]}" for y in map(str, sorted(PRIORITY_YEARS)) for t in TYPES[:5] if not grid[y][t]]
    if gaps:
        print("Priority gaps (no rows yet): " + ", ".join(gaps))

    if a.needs_review:
        nr = [r for r in rows if r["confidence_status"] == "NEEDS_REVIEW"
              and (not a.year or r.get("calendar_year") == str(a.year))]
        print(f"\nNEEDS_REVIEW ({len(nr)}):")
        for r in nr[:25]:
            print(f"  {r['record_id']} | {r['company_name']} | {r['isin'] or '-'} | "
                  f"{r['effective_delisting_date'] or '-'} | {r['source_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
