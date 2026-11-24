"""Build the collection metadata: what has been searched, fetched, parsed, and recorded.

Writes:
  data/metadata/collection_metadata.json   totals, indicators, breakdowns, disk, audit, run + agent-session history
  data/metadata/artifacts.csv              one row per downloaded file (source, status, size, rows parsed)
  data/metadata/dataset_audit.json         data-quality checks (via audit_dataset.py)
  data/metadata/domain_registry.json       every website we fetched from: files, rows, records; new = not in fetch_domains

Run automatically at the end of every collect.py run; safe to run any time:
  python scripts/build_metadata.py
"""
from __future__ import annotations

import csv
import io
import json
import sys
from collections import Counter, defaultdict

import audit_dataset
from common import (
    atomic_write_text,
    AGENT_SESSIONS, CONFIG, MANIFEST, METADATA, PARSED, PRIORITY_YEARS, PROCESSED, RAW, ROOT, RUNS,
    disk_status, domain_of, emit, now_iso, read_jsonl,
)

TYPES = ["COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT"]
COMPLETENESS_FIELDS = ["isin", "nse_symbol", "effective_delisting_date", "order_date", "exit_or_fair_value_inr"]


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def load_master() -> list[dict]:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def query_stats() -> dict:
    plan_p, state_p = CONFIG / "query_plan.json", METADATA / "collection_state.json"
    plan = json.loads(plan_p.read_text(encoding="utf-8")) if plan_p.exists() else {"phases": []}
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {"queries": {}}
    phases = {}
    for ph in plan["phases"]:
        c = Counter()
        for q in ph["queries"]:
            text = q["q"] if isinstance(q, dict) else q
            st = state["queries"].get(text, {}).get("status", "pending")
            c["pending" if st == "engines_blocked" else st] += 1  # blocked = not really searched yet
        total = len(ph["queries"])
        phases[ph["name"]] = {"total": total, "done": c["done"], "no_results": c["no_results"],
                              "failed": c["failed"], "pending": c["pending"],
                              "complete_pct": pct(total - c["pending"] - c["failed"], total)}
    planned = {(q["q"] if isinstance(q, dict) else q) for ph in plan["phases"] for q in ph["queries"]}
    adhoc = [q for q in state["queries"] if q not in planned]
    dstats = docket_stats()
    return {"phases": phases, "adhoc_queries": len(adhoc),
            "blocked_domains": dstats.get("blocked_domains", state.get("blocked_domains", [])), "docket": dstats}


def docket_stats() -> dict:
    """Queue between discovery and fetching (scripts/docket.py), from its last written summary."""
    p = METADATA / "docket_stats.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fetch_stats(manifest: list[dict]) -> dict:
    by_domain: dict[str, Counter] = defaultdict(Counter)
    status, ctype = Counter(), Counter()
    total_bytes = 0
    for e in manifest:
        d = domain_of(e["url"])
        by_domain[d]["quarantined" if e.get("quarantined") else "ok"] += 1
        by_domain[d]["bytes"] += e.get("bytes") or 0
        status[str(e.get("http_status"))] += 1
        ctype[e.get("content_type") or "unknown"] += 1
        total_bytes += e.get("bytes") or 0
    ok = sum(c["ok"] for c in by_domain.values())
    return {"attempts": len(manifest), "ok": ok, "quarantined": len(manifest) - ok,
            "success_rate_pct": pct(ok, len(manifest)), "total_mb": round(total_bytes / 1e6, 2),
            "by_domain": {d: dict(c) for d, c in sorted(by_domain.items(), key=lambda x: -sum(x[1].values()))},
            "by_http_status": dict(status), "by_content_type": dict(ctype)}


def artifact_inventory(manifest: list[dict]) -> list[dict]:
    by_path = {e["local_path"]: e for e in manifest if e.get("local_path")}
    rp = PARSED / "_relevance.json"
    relevance = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
    rows = []
    for p in sorted(x for x in RAW.rglob("*") if x.is_file() and not x.name.startswith(".")):
        rel = str(p.relative_to(ROOT))
        m = by_path.get(rel, {})
        parsed = PARSED / f"{p.parent.name}__{p.stem}.jsonl"
        n_rows = sum(1 for _ in parsed.open(encoding="utf-8")) if parsed.exists() else None
        rows.append({"local_path": rel, "bucket": p.parent.name, "source_url": m.get("url"),
                     "domain": domain_of(m["url"]) if m.get("url") else None,
                     "year_tag": m.get("year_tag"), "content_type": m.get("content_type"),
                     "bytes": p.stat().st_size, "sha256": m.get("sha256"),
                     "harvested_from": m.get("harvested_from"), "fetched_at": m.get("timestamp"),
                     "parsed": parsed.exists(), "relevance": relevance.get(rel), "rows_extracted": n_rows})
    return rows


def domain_registry(inventory: list[dict], master: list[dict], known: list[str]) -> dict:
    """Per website: files fetched, files that gave rows, rows, master records citing it. Websites
    outside the plan's fetch_domains are "new": the agent adds site: queries for the productive ones."""
    reg: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, str] = {}
    for a in inventory:
        d = a["domain"]
        if not d:
            continue
        reg[d]["files"] += 1
        reg[d]["files_with_rows"] += bool(a["rows_extracted"])
        reg[d]["rows"] += a["rows_extracted"] or 0
        if a["fetched_at"] and (d not in first or a["fetched_at"] < first[d]):
            first[d] = a["fetched_at"]
    for r in master:
        for d in {domain_of(u) for u in (r.get("all_source_urls") or r.get("source_url") or "").split(" | ") if u}:
            reg[d]["records"] += 1
    out = {d: {**dict(c), "first_fetched": first.get(d),
               "new": not any(d == k or d.endswith("." + k) for k in known)}
           for d, c in sorted(reg.items(), key=lambda x: (-x[1]["records"], -x[1]["rows"], -x[1]["files"]))}
    return out


def record_stats(rows: list[dict]) -> dict:
    grid = defaultdict(Counter)
    for r in rows:
        grid[r.get("calendar_year") or "unknown"][r["delisting_type"]] += 1
    cells = [(y, t) for y in PRIORITY_YEARS for t in TYPES]
    filled = [f"{y}:{t}" for y, t in cells if grid[str(y)][t]]
    return {
        "total": len(rows),
        "by_year": {y: sum(c.values()) for y, c in sorted(grid.items())},
        "by_type": dict(Counter(r["delisting_type"] for r in rows)),
        "by_confidence": dict(Counter(r["confidence_status"] for r in rows)),
        "by_regime": dict(Counter(r.get("regulatory_regime") or "unknown" for r in rows)),
        "priority_year_records": sum(r.get("is_priority_year") == "True" for r in rows),
        "priority_grid": {str(y): {t: grid[str(y)][t] for t in TYPES} for y in sorted(PRIORITY_YEARS)},
        "priority_grid_cells_filled": len(filled), "priority_grid_cells_total": len(cells),
        "field_completeness_pct": {f: pct(sum(bool(r.get(f)) and r.get(f) != "UNKNOWN" for r in rows), len(rows))
                                   for f in COMPLETENESS_FIELDS},
        "years_covered": sorted(y for y in grid if y != "unknown"),
    }


def run_history(limit: int = 30) -> list[dict]:
    out = []
    for p in sorted(RUNS.glob("*.json"))[-limit:]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({k: r.get(k) for k in ("run_id", "started_at", "status", "stop_reason", "duration_s",
                                          "queries_done", "fetched_ok", "quarantined", "new_records")})
    return out


def agent_sessions(limit: int = 30) -> list[dict]:
    """Summarise Cline --json session logs written by scripts/run_agent.sh."""
    out = []
    for p in sorted(AGENT_SESSIONS.glob("*.jsonl"))[-limit:]:
        tools, first_ts, result = Counter(), None, {}
        for e in read_jsonl(p):
            first_ts = first_ts or e.get("ts")
            ev = e.get("event") or {}
            if ev.get("type") == "content_start" and ev.get("contentType") == "tool":
                tools[ev.get("toolName", "?")] += 1
            if e.get("type") == "run_result":
                result = e
        usage = result.get("aggregateUsage") or result.get("usage") or {}
        out.append({"file": str(p.relative_to(ROOT)), "started_at": first_ts,
                    "finish_reason": result.get("finishReason", "running_or_killed"),
                    "iterations": result.get("iterations"), "tool_calls": sum(tools.values()),
                    "tools": dict(tools), "input_tokens": usage.get("inputTokens"),
                    "output_tokens": usage.get("outputTokens")})
    return out


def csv_rows(name: str) -> int:
    p = PROCESSED / name
    if not p.exists():
        return 0
    with p.open(encoding="utf-8", newline="") as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)


def build() -> dict:
    manifest = read_jsonl(MANIFEST)
    inventory = artifact_inventory(manifest)
    master = load_master()
    queries = query_stats()
    fetch = fetch_stats(manifest)
    records = record_stats(master)
    runs = run_history()
    sessions = agent_sessions()
    audit = audit_dataset.run()
    disk = disk_status()
    plan = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8"))
    domains = domain_registry(inventory, master, plan.get("fetch_domains", []))
    new_domains = {d: v for d, v in domains.items() if v["new"]}

    total_q = sum(p["total"] for p in queries["phases"].values())
    done_q = sum(p["total"] - p["pending"] - p["failed"] for p in queries["phases"].values())
    parsed = [a for a in inventory if a["parsed"]]
    indicators = {
        "records_total": records["total"],
        "priority_year_records": records["priority_year_records"],
        "verified_primary_pct": pct(records["by_confidence"].get("VERIFIED_PRIMARY", 0), records["total"]),
        "needs_review": records["by_confidence"].get("NEEDS_REVIEW", 0),
        "priority_grid_coverage_pct": pct(records["priority_grid_cells_filled"], records["priority_grid_cells_total"]),
        "query_plan_complete_pct": pct(done_q, total_q),
        "queries_done": done_q, "queries_total": total_q,
        "fetch_success_rate_pct": fetch["success_rate_pct"],
        "artifacts_downloaded": len(inventory),
        "artifacts_with_rows": sum(1 for a in parsed if a["rows_extracted"]),
        "artifacts_zero_rows": sum(1 for a in parsed if a["rows_extracted"] == 0
                                   and not (a["relevance"] or "").startswith("irrelevant")),
        "artifacts_irrelevant": sum(1 for a in parsed if (a["relevance"] or "").startswith("irrelevant")),
        "blocked_domains": len(queries["blocked_domains"]),
        "docket": {k: queries["docket"].get(k) for k in ("by_status", "fetch_ready_now", "awaiting_parse",
                                                         "waiting_on_blocked_domain", "evidence_unfetched",
                                                         "parsed_zero_rows_relevant", "historical")}
        if queries["docket"] else None,
        "isin_completeness_pct": records["field_completeness_pct"]["isin"],
        "last_run": runs[-1] if runs else None,
        "agent_sessions": len(sessions),
        "proposed_delisting_leads": csv_rows("proposed_delisting_leads.csv"),
        "bse_only_delistings": csv_rows("bse_only_delistings.csv"),
        "source_domains": len(domains),
        "new_source_domains": len(new_domains),
        "new_domains_with_rows": [d for d, v in new_domains.items() if v["rows"]][:15],
        "audit_errors": audit["error_count"], "audit_warnings": audit["warning_count"],
        "disk": disk,
    }
    meta = {"generated_at": now_iso(), "indicators": indicators, "queries": queries, "fetch": fetch,
            "artifacts": {"total": len(inventory), "by_bucket": dict(Counter(a["bucket"] for a in inventory)),
                          "parsed": len(parsed), "rows_extracted": sum(a["rows_extracted"] or 0 for a in parsed)},
            "records": records, "audit": {"errors": {k: v["count"] for k, v in audit["errors"].items()},
                                          "warnings": {k: v["count"] for k, v in audit["warnings"].items()}},
            "runs": runs, "agent_sessions": sessions}

    METADATA.mkdir(parents=True, exist_ok=True)
    atomic_write_text(METADATA / "collection_metadata.json", json.dumps(meta, indent=1, default=str))
    atomic_write_text(METADATA / "domain_registry.json", json.dumps(domains, indent=1))
    fields = ["local_path", "bucket", "source_url", "domain", "year_tag", "content_type", "bytes",
              "sha256", "harvested_from", "fetched_at", "parsed", "relevance", "rows_extracted"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
    w.writeheader()
    w.writerows(inventory)
    atomic_write_text(METADATA / "artifacts.csv", buf.getvalue())
    return meta


def main() -> int:
    meta = build()
    emit({"written": ["data/metadata/collection_metadata.json", "data/metadata/artifacts.csv"],
          "indicators": meta["indicators"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
