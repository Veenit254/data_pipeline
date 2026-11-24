"""Fetch worker: downloads docket items (scripts/docket.py), best first, at each domain's pace.

- Per-domain budget and delay from config/query_plan.json "domain_limits" (NSE: few requests, long
  pauses). Domains are interleaved: while one domain waits out its delay, another is fetched.
  Items over a domain's budget stay pending for the next run (nothing is lost).
- A failed download is retried later from the docket with backoff (30 min, 2 h, 8 h), never by searching
  again; 404/410 and other definitive 4xx are marked dead at once. Domains that fail `block_domain_after_failures`
  times in a row are blocked; their items wait.
- Downloaded HTML pages are scanned for linked documents, which are filed in the docket ("harvested";
  the first harvest_limit_per_page per page pending, the rest parked).
- Caps (run_limits.py): --max-files (files downloaded OK; "all" = no cap) and --max-docs-per-company
  (company-tagged documents beyond it are parked). A cap reached, or another stage's stop file, ends the run.
- Stop reasons: max_files_reached, docket_exhausted (nothing left to download), sources_exhausted (items left,
  but each is over its domain's per-run budget, on a blocked domain or waiting out a retry backoff).
- Network outages: a download that got no HTTP response triggers a connectivity probe (TCP connect, no data, to
  config "network_probe_hosts", e.g. public DNS resolvers on :443). If no probe host answers either, our own
  connection is down: the item is handed back without counting an attempt, its domain is not penalised, and the
  worker stops with network_unavailable. If the probe succeeds, the failure is the URL's/site's and counts. Without
  probe hosts configured, 4 network errors in a row on 2+ domains mean an outage.
- Official URLs from the focus window (15-20 years ago) that are gone (404/410) are looked up in the Wayback
  Machine; a snapshot found is filed as an "archive" item.
- --follow FILE: keep waiting for new items while FILE exists (Discovery creates it and deletes it when
  done; --follow-pid stops the wait if that process dies without deleting it). Otherwise stop when
  nothing is ready.

  python scripts/fetch_worker.py --max-files 40 --max-docs-per-company 5
  python scripts/fetch_worker.py --follow logs/runs/.x.discovering --follow-pid 12345
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import random
import socket
import sys
import time
import traceback
from pathlib import Path

import requests

import common
import docket
import historical
import polite_fetcher
import run_limits
from common import atomic_write_text, CONFIG, ROOT, USER_AGENT, check_disk, domain_matches, domain_of, emit, log

IDLE_POLL_S = 5
DISK_CHECK_S = 60
NETWORK_DOWN_AFTER = 4       # network errors in a row on 2+ domains: our connection is down, not the sites
MAX_CONSECUTIVE_ERRORS = 3   # unexpected errors in a row: something is broken, stop and say so
FAILURE_REASONS = ("worker_errors",)
PROBE_TIMEOUT_S = 3
PROBE_CACHE_S = 30           # one probe answers for every failure within 30 s


def probe_hosts(p: dict) -> list[tuple[str, int]]:
    """Hosts the connectivity probe tries. With an HTTP(S) proxy configured, downloads go through it, so the
    proxy is what must be reachable (and the probe then never leaves through any other route)."""
    from urllib.parse import urlparse
    proxy = next((os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                  if os.environ.get(k)), None)
    if proxy and p.get("network_probe_hosts"):
        u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        if u.hostname:
            return [(u.hostname, u.port or (443 if u.scheme == "https" else 80))]
    out = []
    for h in p.get("network_probe_hosts") or []:
        host, _, port = str(h).rpartition(":") if ":" in str(h) else (str(h), "", "443")
        try:
            out.append((host, int(port)))
        except ValueError:
            continue
    return out


def network_up(hosts: list[tuple[str, int]], _cache: dict = {}) -> bool | None:  # noqa: B006 (deliberate cache)
    """True if any probe host accepts a TCP connection (nothing is sent), False if none does, None if no probe
    hosts are configured (unknown)."""
    if not hosts:
        return None
    key = tuple(hosts)
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < PROBE_CACHE_S:
        return hit[0]
    up = False
    for host, port in hosts:
        try:
            socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S).close()
            up = True
            break
        except OSError:
            continue
    _cache[key] = (up, time.time())
    return up


def network_failure(e: dict) -> bool:
    """No HTTP response at all (DNS, connect, timeout): could be the site or our own connection."""
    return e.get("http_status") is None and bool(e.get("error")) and not e.get("permanent") and not e.get("soft_block")


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def discovering(marker: Path | None, pid: int | None) -> bool:
    return bool(marker) and marker.exists() and (pid is None or alive(pid))


def plan() -> dict:
    try:
        return json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def harvest(dk: docket.Docket, entry: dict, policy: dict, per_page: int, run_id: str | None) -> dict:
    links = polite_fetcher.harvest_links(ROOT / entry["local_path"], entry["url"], policy)
    items = [{"url": u, "kind": "harvested", "year_tag": entry.get("year_tag"), "harvested_from": entry["url"],
              "rank": i, "parked": i > per_page, "run_id": run_id} for i, u in enumerate(links, 1)]
    return dk.file(items, policy["known"], polite_fetcher.already_fetched()) if items else {"new": 0}


def archive_fallback(dk: docket.Docket, session, c, policy: dict, used) -> bool:
    """File the Wayback copy of a gone official document from the focus window. True if one was filed."""
    if not (domain_matches(c["url"], historical.OFFICIAL_DOMAINS) and historical.in_window(c["year_tag"], dk.focus)):
        return False
    key = polite_fetcher.limit_key("https://archive.org/", policy)
    if used[key] >= polite_fetcher.limits_for(key, policy)["max_per_run"]:
        return False
    used[key] += 1
    snap = historical.archive_copy(session, c["url"], c["year_tag"])
    if not snap:
        return False
    dk.file([{"url": snap, "kind": "archive", "year_tag": c["year_tag"], "harvested_from": c["url"],
              "company": c["company"], "company_name": c["company_name"], "title": c["title"]}], policy["known"])
    log(f"archive copy filed for gone {c['url']}: {snap}", kind="archive_copy", url=c["url"], snapshot=snap)
    return True


def run(max_files: int | None = 60, follow: Path | None = None, follow_pid: int | None = None,
        max_seconds: float | None = None, max_docs_per_company: int | None = None,
        stop: run_limits.StopFile | None = None) -> dict:
    dk = docket.open_docket()
    p = plan()
    policy = polite_fetcher.fetch_policy()
    block_after = p.get("block_domain_after_failures", 3)
    per_page = p.get("harvest_limit_per_page", 20)
    run_id = os.environ.get("NSE_RUN_ID")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    next_ok: dict[str, float] = {}           # domain_limits key -> earliest next request
    used = polite_fetcher.RUN_COUNTS          # requests per key in this run
    disk_state = dk.get_meta("disk_state", {})
    stop = stop or run_limits.StopFile(None)
    hosts = probe_hosts(p)
    out = {"fetched_ok": 0, "failed": 0, "dead": 0, "harvested_new": 0, "archive_filed": 0, "outage_released": 0,
           "parked_company_cap": 0, "newly_blocked_domains": [], "over_budget_domains": [], "worker_errors": 0,
           "last_worker_error": None, "stop_reason": None}
    t0, last_disk, consecutive, inflight = time.time(), 0.0, 0, None
    net_streak: list[tuple[str, str]] = []
    out["partials_removed"] = common.cleanup_partials([common.RAW, common.QUARANTINE])
    try:
        while True:
            if max_files and out["fetched_ok"] >= max_files:
                out["stop_reason"] = "max_files_reached"
                stop.stop("max_files_reached")
                break
            if stop.reason():
                out["stop_reason"] = stop.reason()
                break
            if max_seconds and time.time() - t0 > max_seconds:
                out["stop_reason"] = "time_limit"
                break
            if time.time() - last_disk >= DISK_CHECK_S:  # sizing data/ takes a while: once a minute
                last_disk = time.time()
                if check_disk(disk_state)["level"] == "critical":
                    out["stop_reason"] = "low_disk_space"
                    break
            now = time.time()
            cands = dk.candidates()
            pick, soonest, over = None, None, set()
            for c in cands:
                key = polite_fetcher.limit_key(c["url"], policy)
                lim = polite_fetcher.limits_for(key, policy)
                if used[key] >= lim["max_per_run"]:
                    over.add(key)
                    continue
                if next_ok.get(key, 0) > now:
                    soonest = min(soonest or next_ok[key], next_ok[key])
                    continue
                if max_docs_per_company and c["company"] and dk.company_docs(c["company"]) >= max_docs_per_company:
                    dk.park(c["url"], f"max_docs_per_company ({max_docs_per_company}) reached")
                    out["parked_company_cap"] += 1
                    continue
                if not polite_fetcher.allowed(c["url"], policy):  # policy changed since it was filed
                    dk.done_fetch(c["url"], {"http_status": None, "quarantined": True,
                                             "error": "not allowed by fetch policy (deny_domains/open_web)"})
                    continue
                pick = (c, key, lim)
                break
            out["over_budget_domains"] = sorted(over)
            if not pick:
                if soonest:                       # only domain delays in the way: wait for the first
                    time.sleep(max(0.2, min(soonest - now, IDLE_POLL_S)))
                    continue
                if discovering(follow, follow_pid):  # Discovery is still filing: wait for more
                    time.sleep(IDLE_POLL_S)
                    continue
                out["stop_reason"] = "docket_exhausted" if not dk.remaining() else "sources_exhausted"
                break
            c, key, lim = pick
            if not dk.lease(c["url"]):            # another worker took it, or it is a poison item (now dead)
                continue
            used[key] += 1
            inflight = c["url"]
            try:
                e = polite_fetcher.fetch_one(session, c["url"], c["year_tag"], c["harvested_from"])
            except Exception as ex:  # one bad response must not kill the worker (and every later run)
                inflight = None
                next_ok[key] = time.time() + random.uniform(*lim["delay_s"])
                msg = f"worker error: {type(ex).__name__}: {ex}"[:300]
                dk.done_fetch(c["url"], {"http_status": None, "quarantined": True, "error": msg})
                out["worker_errors"] += 1
                out["last_worker_error"] = msg
                consecutive += 1
                log(f"fetch worker: {msg} on {c['url']}", level="ERROR", kind="fetch_worker_error", url=c["url"],
                    traceback=traceback.format_exc()[-800:])
                if isinstance(ex, OSError) and ex.errno == errno.ENOSPC:
                    out["stop_reason"] = "low_disk_space"
                    break
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    out["stop_reason"] = "worker_errors"
                    break
                continue
            inflight, consecutive = None, 0
            next_ok[key] = time.time() + random.uniform(*lim["delay_s"])
            if network_failure(e) and network_up(hosts) is False:
                dk.release(c["url"])          # our connection is down: the site was never reached
                used[key] -= 1
                out["outage_released"] += 1
                out["stop_reason"] = "network_unavailable"
                log(f"fetch worker: no response from {domain_of(c['url'])} and no probe host reachable: "
                    "our network is down; item handed back, no attempt counted", level="ERROR",
                    kind="network_unavailable", url=c["url"])
                break
            status = dk.done_fetch(c["url"], e)
            ok = status == "fetched"
            out["fetched_ok" if ok else status] += 1
            if not ok and e.get("http_status") is None and e.get("error"):
                net_streak.append((c["url"], domain_of(c["url"])))
            else:
                net_streak = []
            if len(net_streak) >= NETWORK_DOWN_AFTER and len({d for _, d in net_streak}) >= 2:
                for u, _ in net_streak:       # the sites were never reached: those attempts don't count
                    dk.refund_attempt(u)
                out["stop_reason"] = "network_unavailable"
                log(f"fetch worker: {len(net_streak)} network errors in a row on {len({d for _, d in net_streak})} "
                    f"domains: stopping (network down?)", level="ERROR", kind="network_unavailable")
                break
            if dk.domain_result(domain_of(c["url"]), ok, 403 if e.get("soft_block") else e.get("http_status"), block_after):
                out["newly_blocked_domains"].append(domain_of(c["url"]))
                log(f"domain blocked after {block_after} consecutive failures: {domain_of(c['url'])}",
                    level="WARN", kind="domain_blocked", domain=domain_of(c["url"]))
            try:
                if status == "dead" and e.get("http_status") in (404, 410):
                    out["archive_filed"] += archive_fallback(dk, session, c, policy, used)
                if ok and (e.get("local_path") or "").endswith((".html", ".htm")):
                    out["harvested_new"] += harvest(dk, e, policy, per_page, run_id).get("new", 0)
            except Exception as ex:  # the download itself is recorded; a failed link scan is only logged
                out["worker_errors"] += 1
                out["last_worker_error"] = f"after download: {type(ex).__name__}: {ex}"[:300]
                log(f"fetch worker: harvest/archive step failed for {c['url']}: {ex}", level="ERROR",
                    kind="fetch_worker_error", url=c["url"], traceback=traceback.format_exc()[-800:])
            print(f"  ⇣ {status:<8} {e.get('http_status')} {c['url'][:90]}", flush=True)
    except KeyboardInterrupt:
        out["stop_reason"] = "keyboard_interrupt"
        if inflight:
            dk.release(inflight)
    except Exception as ex:  # e.g. the docket database itself failing: stop with the reason, summary still written
        out["stop_reason"] = f"error: {type(ex).__name__}: {ex}"[:300]
        log(f"fetch worker stopped: {out['stop_reason']}", level="ERROR", kind="run_error",
            traceback=traceback.format_exc()[-800:])
    finally:
        dk.set_meta("disk_state", disk_state)
        try:
            out["docket"] = dk.write_stats()
        except Exception as ex:
            out["docket"] = {"error": str(ex)[:200]}
        out["duration_s"] = round(time.time() - t0, 1)
        log(f"fetch worker: {out['fetched_ok']} ok, {out['failed']} failed (will retry), {out['dead']} dead, "
            f"+{out['harvested_new']} linked docs, +{out['archive_filed']} archive copies filed; "
            f"stop: {out['stop_reason']}", kind="fetch_worker",
            **{k: out[k] for k in ("fetched_ok", "failed", "dead", "harvested_new", "archive_filed",
                                   "parked_company_cap", "stop_reason")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--max-files", default="60", help="files downloaded OK this run; 0 or all = no cap")
    ap.add_argument("--max-fetch", dest="max_files", help=argparse.SUPPRESS)  # old name
    ap.add_argument("--max-docs-per-company", default="all", help="per company, all runs; 0 or all = no cap")
    ap.add_argument("--stop-file", type=Path, help="run-wide stop signal shared with the other stages")
    ap.add_argument("--follow", type=Path, metavar="FILE", help="keep waiting for new items while FILE exists")
    ap.add_argument("--follow-pid", type=int, metavar="PID", help="...and while this process is alive")
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--summary-out", type=Path)
    a = ap.parse_args()
    common.graceful_sigterm()
    out = run(run_limits.parse_cap(a.max_files), a.follow, a.follow_pid, a.max_seconds,
              run_limits.parse_cap(a.max_docs_per_company), run_limits.StopFile(a.stop_file))
    if a.summary_out:
        atomic_write_text(a.summary_out, json.dumps(out, indent=1, default=str))
    emit({k: v for k, v in out.items() if k != "docket"} | {"docket_ready": (out.get("docket") or {}).get("fetch_ready_now")})
    reason = str(out["stop_reason"])
    return 1 if reason in FAILURE_REASONS or reason.startswith("error") else 0


if __name__ == "__main__":
    sys.exit(main())
