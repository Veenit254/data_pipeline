"""Collection run, driven by config/query_plan.json, in three stages joined by a persistent docket:

  Discovery  search the plan's queries; file candidate URLs + evidence in the docket   (this script)
  Fetching   download docket items at each domain's pace; file linked documents      (fetch_worker.py)
  Processing parse / OCR downloads, merge records                                    (process_worker.py)

By default (--stage all) the three run side by side: Discovery in this process, the fetch and process
workers as child processes that keep draining the docket while Discovery searches. A search never
waits for a download or OCR, and a failed download is retried from the docket (with backoff), never
by searching again. --stage picks one stage; --harvest-only (= --stage drain) fetches + processes only.

- Historical focus first (historical.py): companies delisted 15-20 years ago ("focus" in the config). Proof
  queries (official / archive records) run before every other phase, then supporting-document queries per
  proven company; official and archive results are downloaded before news.
- Caps and run modes (run_limits.py): max_files, max_companies, max_docs_per_company from "run_limits" in the
  config or --max-files / --max-companies / --max-docs-per-company / --unlimited; mode "ask" asks on a
  terminal only. Stop reasons: max_files_reached, max_companies_reached, max_queries_reached,
  queries_exhausted, docket_exhausted, sources_exhausted (plus search/disk/interrupt ones).
- Resumable: searched queries are tracked in data/metadata/collection_state.json; the docket is
  data/docket.sqlite (scripts/docket.py --stats).
- Search results beyond fetch_limit_per_query are filed as "parked" evidence, not downloaded.
- Each domain has a per-run request budget ("domain_limits"; NSE's is small); items over budget stay
  pending in the docket for the next run.
- Logs every step to logs/events.jsonl + logs/agent_run.log, and writes a run summary to
  logs/runs/<run_id>.json (also on Ctrl-C or error). Rebuilds data/metadata/ at the end.

Examples:
  python scripts/collect.py --dry-run                   # pending queries per phase, docket backlog
  python scripts/collect.py --max-queries 10            # next 10 queries; downloads + parsing alongside
  python scripts/collect.py --stage discover --max-queries 10   # search only (fill the docket)
  python scripts/collect.py --harvest-only --max-files 60       # drain the docket only (no searching)
  python scripts/collect.py --retry-failed --phase priority_2017
  python scripts/collect.py --unlimited                         # no caps: until everything is used up
  python scripts/collect.py --max-files 200 --max-companies all --phase historical_proof
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path


import build_metadata
import common
import docket
import historical
import polite_fetcher
import run_limits
import web_search
from common import (
    atomic_write_text,
    CONFIG, METADATA, PROCESSED, ROOT, RUNS, agent_session, check_disk,
    emit, log, now_iso, polite_sleep, run_id,
)


class LowDisk(Exception):
    pass

PLAN = CONFIG / "query_plan.json"
STATE = METADATA / "collection_state.json"
PY = sys.executable


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"queries": {}, "blocked_domains": []}


def save_state(state: dict) -> None:
    METADATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def plan_items(plan: dict, phase: str | None) -> list[dict]:
    items = []
    for ph in plan["phases"]:
        if phase and phase not in ("all", ph["name"]):
            continue
        for q in ph["queries"]:
            if isinstance(q, dict):
                items.append({"phase": ph["name"], "query": q["q"], "year_tag": q["year_tag"],
                              "pages": ph.get("pages", 1)})
            else:
                items.append({"phase": ph["name"], "query": q, "year_tag": ph["year_tag"],
                              "pages": ph.get("pages", 1)})
    return items


SEARCH_COOLDOWN_MIN = 30  # after search engines suspend SearXNG, no queries for this long


def search_cooldown_min_left(state: dict) -> int:
    """Minutes until searching is allowed: the global cooldown, or until the first benched engine is back."""
    until = state.get("engines_suspended_until", 0)
    until = max(until, web_search.Search().cooldown_until())
    return max(0, round((until - time.time()) / 60))


def master_count() -> int:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return 0
    with p.open(encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # records, not lines: quoted fields may hold newlines


def search_ready() -> dict:
    """Search provider -> None if usable (SearXNG reachable, API key present), else why not."""
    return {p.name: p.health() for p in web_search.Search().providers}


def mark_stale_runs(current: str) -> None:
    """Runs left at status "running" were killed without cleanup (e.g. by a tool timeout)."""
    for p in RUNS.glob("*.json"):
        if p.stem == current:
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("status") == "running":
            r.update(status="killed", stop_reason="process ended without cleanup")
            atomic_write_text(p, json.dumps(r, indent=1))


def bar(done: int, total: int, width: int = 20) -> str:
    filled = int(width * done / total) if total else width
    return "█" * filled + "░" * (width - filled)



def docket_backlog(dk: docket.Docket | None = None) -> int:
    """Docket items a fetch run could take now (pending or due for retry, domain not blocked)."""
    return (dk or docket.open_docket()).stats()["fetch_ready_now"]


def discover(todo: list[dict], state: dict, step: dict, plan: dict, rid: str, args,
             stop: run_limits.StopFile | None = None) -> None:
    """Search each query and file its results in the docket. No downloading here."""
    dk = docket.open_docket()
    policy = polite_fetcher.fetch_policy()
    fetch_cut = args.fetch_limit or plan.get("fetch_limit_per_query", 8)
    query_delay = web_search.search_settings()["query_delay_s"]
    search = web_search.Search()
    errors_in_row = blocks_in_row = 0
    stop = stop or run_limits.StopFile(None)
    for i, it in enumerate(todo, 1):
        if check_disk(state)["level"] == "critical":
            raise LowDisk()
        if stop.reason():  # another stage reached a cap
            step["stop_reason"] = stop.reason()
            return
        q, tag, phase = it["query"], it["year_tag"], it["phase"]
        ph = step["phases"].setdefault(phase, {"queries": 0, "urls": 0, "filed": 0})
        if i > 1:
            polite_sleep(*query_delay)  # long pause between queries: engines ban fast clients
        entry = {"phase": phase, "year_tag": tag, "ts": now_iso(), "run_id": rid}
        try:
            s = web_search.run(q, it["pages"], tag, search)
            errors_in_row = 0
        except web_search.SearchExhausted as e:  # every provider used up: stop, query stays pending
            state["engines_suspended_until"] = e.until
            save_state(state)
            step["stop_reason"] = "search_engines_suspended"
            log(f"stopping discovery: {e}; queries stay pending", level="ERROR", kind="search_engines_suspended",
                retry_after_min=round((e.until - time.time()) / 60))
            return
        except Exception as e:  # network / provider errors: record and continue
            errors_in_row += 1
            entry.update(status="failed", error=str(e)[:200])
            state["queries"][q] = entry
            save_state(state)
            step["queries_failed"] += 1
            log(f"search failed ({errors_in_row} in a row): {q}: {e}", level="ERROR", kind="search_error", query=q)
            if errors_in_row >= 3:
                step["stop_reason"] = "search_errors"
                return
            continue

        found = []
        for cf in s["cache_files"]:
            d = json.loads((ROOT / cf).read_text(encoding="utf-8"))
            found += [(r, d.get("provider")) for r in d.get("results", [])
                      if r.get("url") and polite_fetcher.allowed(r["url"], policy)]
        # official / archive records before other sites, news last (stable: keeps the engine's order within a tier)
        found.sort(key=lambda x: historical.tier(x[0]["url"]))
        items = [{"url": r["url"], "kind": "search", "year_tag": tag, "query": q, "rank": rank, "provider": prov,
                  "title": r.get("title"), "snippet": r.get("content"), "parked": rank > fetch_cut, "run_id": rid,
                  "company": it.get("company"), "company_name": it.get("company_name")}
                 for rank, (r, prov) in enumerate(found, 1)]
        filed = dk.file(items, policy["known"], polite_fetcher.already_fetched())

        engines_blocked = not s["result_count"] and bool(s.get("unresponsive_engines"))
        blocks_in_row = blocks_in_row + 1 if engines_blocked else 0
        if engines_blocked:
            log(f"0 results, unresponsive engines {s['unresponsive_engines']}: {q}", level="WARN",
                kind="engines_unresponsive", query=q, engines=s["unresponsive_engines"])
        status = "done" if s["result_count"] else ("engines_blocked" if engines_blocked else "no_results")
        entry.update(status=status, results=s["result_count"], providers=s.get("providers"),
                     unresponsive_engines=s.get("unresponsive_engines", []), candidate_urls=len(items),
                     filed_new=filed["new"], filed_pending=filed.get("pending", 0), cache_files=s["cache_files"])
        state["queries"][q] = entry
        save_state(state)
        if blocks_in_row >= 2:
            # Search engines rate-limit/CAPTCHA SearXNG; more queries would only extend the ban.
            step["stop_reason"] = "search_engines_suspended"
            state["engines_suspended_until"] = time.time() + 60 * SEARCH_COOLDOWN_MIN
            save_state(state)
            log(f"stopping discovery: engines suspended for {blocks_in_row} queries in a row "
                f"({', '.join(s['unresponsive_engines'])}); queries stay pending", level="ERROR",
                kind="search_engines_suspended", engines=s["unresponsive_engines"])
            return
        step["queries_done"] += 1
        step["queries_no_results"] += not s["result_count"]
        step["urls_found"] += s["result_count"]
        step["filed_new"] += filed["new"]
        ph["queries"] += 1
        ph["urls"] += s["result_count"]
        ph["filed"] += filed["new"]
        print(f"[{bar(i, len(todo))}] {i}/{len(todo)} {phase:<14} +{s['result_count']:>2} urls → docket "
              f"+{filed.get('pending', 0)} to fetch, +{filed.get('parked', 0)} parked  | {q[:55]}", flush=True)
        log(f"query done {i}/{len(todo)}: {q}", kind="query_done", query=q, phase=phase,
            progress=f"{i}/{len(todo)}", results=s["result_count"], filed_new=filed["new"])


def spawn(script: str, *args: str, out: Path) -> subprocess.Popen:
    return subprocess.Popen([PY, "-u", str(ROOT / "scripts" / script), *args, "--summary-out", str(out)], cwd=ROOT)


def worker_failure(workers: dict) -> str | None:
    """Why the run failed because of a worker, or None. A worker failed if it exited non-zero (a traceback, a
    signal, or its own error stop reason) or left no summary (it died before writing it)."""
    for name, w in workers.items():
        if w["exit_code"] not in (0, None) or not w["summary"]:
            detail = w["stop_reason"] or ("no summary written" if not w["summary"] else "")
            return f"{name}_failed (exit {w['exit_code']}{': ' + str(detail) if detail else ''})"[:300]
    return None


def child_summary(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--stage", choices=("all", "discover", "fetch", "process", "drain"), default="all")
    ap.add_argument("--phase", default="all", help="phase name from config/query_plan.json, or 'all'")
    ap.add_argument("--max-queries", type=int, default=0, help="stop after N queries (0 = no limit)")
    ap.add_argument("--fetch-limit", type=int, help="override fetch_limit_per_query (results filed to fetch)")
    ap.add_argument("--max-files", help="files downloaded OK this run (N, 0 or all = no cap)")
    ap.add_argument("--max-companies", help="new records in the master this run (N, 0 or all)")
    ap.add_argument("--max-docs-per-company", help="documents fetched per company, all runs (N, 0 or all)")
    ap.add_argument("--unlimited", action="store_true", help="no caps: run until queries, docket or sources run out")
    ap.add_argument("--capped", action="store_true", help="use the caps from the config even if its mode is ask")
    ap.add_argument("--max-fetch", dest="max_files", help=argparse.SUPPRESS)  # old name of --max-files
    ap.add_argument("--process-every", type=int, default=5, help="parse+merge once N downloads are waiting")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-run queries that failed or returned no results (e.g. engines were throttled)")
    ap.add_argument("--harvest-only", action="store_true", help="same as --stage drain: fetch + process, no search")
    ap.add_argument("--max-harvest", dest="max_files", help=argparse.SUPPRESS)  # old name of --max-files
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    stage = "drain" if a.harvest_only else a.stage
    common.graceful_sigterm()

    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    state = load_state()
    # historical focus first: window-year proof, company proof, company support; then the plan's phases
    hist = [it for it in historical.items(state) if a.phase in ("all", it["phase"])]
    planned = {it["query"] for it in hist}
    items = hist + [it for it in plan_items(plan, a.phase) if it["query"] not in planned]
    if not items:
        emit({"error": f"unknown or empty phase: {a.phase}", "phases": [p["name"] for p in plan["phases"]]})
        return 2

    def pending(it):
        st = state["queries"].get(it["query"], {}).get("status")
        # engines_blocked = searched while SearXNG's engines were suspended: never really searched
        return st in (None, "engines_blocked") or (a.retry_failed and st in ("failed", "no_results"))

    todo = [it for it in items if pending(it)] if stage in ("all", "discover") else []
    pending_total = len(todo)
    if a.max_queries:
        todo = todo[: a.max_queries]
    limits = run_limits.resolve(a, interactive=sys.stdin.isatty() and sys.stdout.isatty() and not a.dry_run)

    if a.dry_run:
        dk = docket.open_docket()
        ds = dk.stats()
        by_phase: dict[str, dict] = {}
        for it in items:
            d = by_phase.setdefault(it["phase"], {"total": 0, "pending": 0})
            d["total"] += 1
            d["pending"] += pending(it)
        emit({"dry_run": True, "stage": stage, "would_run": len(todo), "limits": limits,
              "focus_window": list(historical.window()), "historical": ds.get("historical"), "phases": by_phase,
              "harvest_backlog": ds["fetch_ready_now"],
              "docket": {k: ds[k] for k in ("by_status", "fetch_ready_now", "awaiting_parse",
                                            "waiting_on_blocked_domain", "evidence_unfetched")},
              "blocked_domains": ds["blocked_domains"],
              "search_cooldown_min_left": search_cooldown_min_left(state),
              "search_engines_usable": web_search.Search().usable(),
              "next": [t["query"] for t in todo[:5]]})
        return 0

    os.environ.pop("NSE_RUN_ID", None)  # every collect.py invocation is its own run (workers inherit it)
    rid = run_id()
    RUNS.mkdir(parents=True, exist_ok=True)
    mark_stale_runs(rid)
    summary_path = RUNS / f"{rid}.json"
    step = {
        "run_id": rid, "agent_session": agent_session(), "started_at": now_iso(), "finished_at": None,
        "duration_s": None, "status": "running", "stop_reason": None, "stage": stage, "args": vars(a),
        "limits": limits, "discovery_stop_reason": None,
        "queries_planned": len(todo), "queries_done": 0, "queries_failed": 0, "queries_no_results": 0,
        "phases": {}, "urls_found": 0, "filed_new": 0,
        "fetched_ok": 0, "fetch_failed": 0, "fetch_dead": 0, "quarantined": 0, "harvested_ok": 0,
        "harvest_backlog": None, "newly_blocked_domains": [], "fetch_stop_reason": None,
        "parsed_files": 0, "parsed_rows": 0, "parse_errors": 0, "process_stop_reason": None,
        "records_before": master_count(), "records_after": None, "new_records": None,
    }
    t0 = time.time()

    def write_summary():
        step["duration_s"] = round(time.time() - t0, 1)
        if step["records_after"] is not None:
            step["new_records"] = step["records_after"] - step["records_before"]
        atomic_write_text(summary_path, json.dumps(step, indent=1))

    log(f"run start: stage={stage}, {len(todo)} queries, phase={a.phase}, {limits['mode']}: "
        f"{run_limits.describe(limits)}", kind="run_start", queries_planned=len(todo), phase=a.phase, stage=stage,
        limits=limits)
    write_summary()

    # can't search now? still drain the docket instead of doing nothing
    if stage == "all" and todo:
        wait_s = state.get("engines_suspended_until", 0) - time.time()
        ready = search_ready()
        if wait_s > 0 or not any(v is None for v in ready.values()):
            step["stop_reason"] = "search_engines_suspended" if wait_s > 0 else "search_unavailable"
            log(f"no searching this run ({step['stop_reason']}); draining the docket only", level="WARN",
                kind="search_cooldown", minutes_left=round(max(wait_s, 0) / 60), providers=ready)
            todo, stage = [], "drain"
    elif stage == "discover" and todo:
        wait_s = state.get("engines_suspended_until", 0) - time.time()
        if wait_s > 0:
            step.update(status="error", stop_reason="search_engines_suspended", finished_at=now_iso())
            write_summary()
            emit({"run_id": rid, "status": "error", "stop_reason": "search_engines_suspended",
                  "retry_after_min": round(wait_s / 60), "hint": "--harvest-only still works"})
            return 1

    fetch_out, proc_out = RUNS / f".{rid}.fetch.json", RUNS / f".{rid}.process.json"
    marker = RUNS / f".{rid}.discovering"  # exists while Discovery runs: the fetch worker waits for new items
    stop = run_limits.StopFile(RUNS / f".{rid}.stop")  # the first stage to reach a cap writes the reason here
    cap = {k: str(limits[k] or "all") for k in run_limits.CAPS}
    children: dict[str, subprocess.Popen] = {}
    try:
        if stage in ("all", "drain", "fetch"):
            follow = []
            if stage == "all" and todo:
                marker.touch()
                follow = ["--follow", str(marker), "--follow-pid", str(os.getpid())]
            fw = spawn("fetch_worker.py", "--max-files", cap["max_files"], "--max-docs-per-company",
                       cap["max_docs_per_company"], "--stop-file", str(stop.path), *follow, out=fetch_out)
            children["fetch_worker"] = fw
            if stage != "fetch":
                children["process_worker"] = (spawn("process_worker.py", "--batch", str(a.process_every), "--follow", str(fw.pid),
                                      "--max-companies", cap["max_companies"], "--records-before",
                                      str(step["records_before"]), "--stop-file", str(stop.path), out=proc_out))
        elif stage == "process":
            children["process_worker"] = (spawn("process_worker.py", "--batch", "1", "--max-companies", cap["max_companies"],
                                  "--records-before", str(step["records_before"]), "--stop-file", str(stop.path),
                                  out=proc_out))
        if todo:
            try:
                discover(todo, state, step, plan, rid, a, stop)
            finally:
                marker.unlink(missing_ok=True)  # the fetch worker drains what is left, then stops
            write_summary()
        for c in children.values():
            c.wait()
        step["status"] = "completed"
        step["discovery_stop_reason"] = step["stop_reason"] or (
            None if not todo else "max_queries_reached" if pending_total > len(todo) else "queries_exhausted")
    except LowDisk:
        step.update(status="stopped", stop_reason="low_disk_space")
    except KeyboardInterrupt:
        step.update(status="interrupted", stop_reason="keyboard_interrupt")
    except Exception as e:
        step.update(status="error", stop_reason=f"{type(e).__name__}: {e}"[:300])
        log(traceback.format_exc()[-800:], level="ERROR", kind="run_error")
    finally:
        for c in children.values():  # Ctrl-C / job.py stop reach the workers too (same process group)
            try:
                c.wait(timeout=120)
            except subprocess.TimeoutExpired:
                c.terminate()
                try:
                    c.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    c.kill()
        f, pr = child_summary(fetch_out), child_summary(proc_out)
        step["workers"] = {name: {"exit_code": c.returncode,
                                  "summary": bool(f if name == "fetch_worker" else pr),
                                  "stop_reason": (f if name == "fetch_worker" else pr).get("stop_reason")}
                           for name, c in children.items()}
        failed = worker_failure(step["workers"])
        capped = stop.reason()
        for p in (fetch_out, proc_out, marker, stop.path):
            p.unlink(missing_ok=True)
        step.update(fetched_ok=f.get("fetched_ok", 0), fetch_failed=f.get("failed", 0), fetch_dead=f.get("dead", 0),
                    quarantined=f.get("failed", 0) + f.get("dead", 0), harvested_ok=f.get("harvested_new", 0),
                    newly_blocked_domains=f.get("newly_blocked_domains", []), fetch_stop_reason=f.get("stop_reason"),
                    parsed_files=pr.get("parsed_files", 0), parsed_rows=pr.get("parsed_rows", 0),
                    parse_errors=pr.get("parse_errors", 0), process_stop_reason=pr.get("stop_reason"))
        if "low_disk_space" in (f.get("stop_reason"), pr.get("stop_reason")):
            step.update(status="stopped", stop_reason="low_disk_space")
        elif f.get("stop_reason") == "network_unavailable":
            step.update(status="stopped", stop_reason="network_unavailable")
        elif failed and step["status"] in ("completed", "running"):
            # a worker crashed or failed: the run failed, whatever discovery did (agent: "job failed" rule)
            step.update(status="error", stop_reason=failed)
            log(f"run failed: {failed}", level="ERROR", kind="run_error", workers=step["workers"])
        elif step["status"] == "completed":
            # one reason for the run: a cap reached, else discovery's own stop, else how the docket ended
            step["stop_reason"] = capped or step["discovery_stop_reason"] or f.get("stop_reason") \
                or pr.get("stop_reason") or f"{stage}_done"
        step["finished_at"] = now_iso()
        save_state(state)
        step["records_after"] = master_count()
        dk = docket.open_docket()
        step["harvest_backlog"] = docket_backlog(dk)
        dk.write_stats()
        write_summary()
        try:
            meta = build_metadata.build()
        except Exception as e:
            meta = {"error": str(e)}
        log(f"run end: status={step['status']} queries={step['queries_done']} fetched={step['fetched_ok']} "
            f"new_records={step['new_records']}", kind="run_end",
            **{k: step[k] for k in ("status", "stop_reason", "stage", "queries_done", "fetched_ok", "quarantined",
                                    "new_records", "duration_s")})

    emit({k: step.get(k) for k in ("run_id", "status", "stop_reason", "discovery_stop_reason", "stage", "limits", "workers",
                               "queries_done", "queries_failed",
                               "urls_found", "filed_new", "fetched_ok", "fetch_failed", "fetch_dead", "harvested_ok",
                               "harvest_backlog", "fetch_stop_reason", "parsed_rows", "records_before",
                               "records_after", "new_records", "duration_s", "newly_blocked_domains")}
         | {"summary_file": str(summary_path.relative_to(ROOT)),
            "indicators": meta.get("indicators") if isinstance(meta, dict) else None})
    return 0 if step["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
