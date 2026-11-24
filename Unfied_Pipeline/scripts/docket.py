"""The docket: a persistent queue between Discovery and Fetching / Parsing.

Discovery (collect.py --stage discover) searches and *files* candidate URLs here with their evidence
(query, provider, rank, title, snippet). The fetch worker (fetch_worker.py) downloads them at each
domain's pace and files the document links it finds on downloaded pages; the process worker
(process_worker.py) parses / OCRs what was downloaded and merges records. Each stage runs on its own,
so a search never waits for a slow download or OCR, and a failed download is retried from the docket
without searching again.

Item status:
  parked    found, but below the per-query fetch cut (fetch_limit_per_query) - kept as evidence;
            `--promote` makes it pending
  pending   waiting for the fetch worker (next_attempt_at may be in the future: retry backoff)
  fetching  leased by a fetch worker (a crashed worker's lease expires after 15 min; re-leasing an expired
            lease counts as a failed attempt, and after MAX_LEASE_CRASHES the item is dead: a URL that
            kills the worker must not kill every later run too)
  fetched   downloaded, waiting for the process worker
  parsed    parsed (rows, relevance recorded)
  failed    download failed; retried with backoff (30 min, 2 h, 8 h) until MAX_ATTEMPTS
  dead      gave up: missing (404/410/other 4xx), or MAX_ATTEMPTS failures
  parse_error  the parser raised on the file (reported, not retried until the parser changes)
Failure classes: 400/401/404/410/… dead at once; 403, 429 and soft-block pages are refusals, retried with long
backoff (2 h, 8 h, then daily, up to MAX_THROTTLED_ATTEMPTS); network errors and 5xx are retried with backoff
(30 min, 2 h, 8 h, up to MAX_ATTEMPTS). A domain that refuses (403/429/5xx/soft block) `block_domain_after_failures`
times in a row is blocked for 6 h, then 24 h, then 72 h: blocks expire. Timeouts and connection errors never block
a domain (a local network outage is not the site's fault; the fetch worker stops with network_unavailable).

Stored in data/docket.sqlite (git-ignored; data/metadata/docket_stats.json is the tracked summary).

  python scripts/docket.py --stats                # counts by status / kind, ready now, top domains, dead reasons
  python scripts/docket.py --list failed --limit 20
  python scripts/docket.py --promote [--query TEXT] [--limit N]   # parked -> pending
  python scripts/docket.py --retry dead|failed|parse_error [--domain D]   # back to pending (user)
  python scripts/docket.py --unblock DOMAIN       # (user)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import historical
from common import atomic_write_text, DATA, METADATA, PARSED, PRIORITY_YEARS, domain_matches, domain_of, emit, log, now_iso, read_jsonl

DB = DATA / "docket.sqlite"
STATS = METADATA / "docket_stats.json"
MAX_ATTEMPTS = 4                    # network errors, 5xx, empty bodies
MAX_THROTTLED_ATTEMPTS = 10         # 403 / 429 / soft blocks: the server is refusing for now, not "missing"
THROTTLE_BACKOFF_MIN = [120, 480, 1440]   # 2 h, 8 h, then daily
BLOCK_HOURS = [6, 24, 72]           # a domain's 1st, 2nd, 3rd+ block (expires; the domain is then tried again)
LEGACY_BLOCK_S = 86400              # blocks recorded without an expiry (older dockets) end a day after they began
CANDIDATES_PER_DOMAIN = 25          # every ready domain gets a place in the candidate list (no starvation)
DEFINITIVE_STATUSES = {400, 401, 404, 405, 410, 414, 451}   # the URL is wrong or gone: dead at once
THROTTLE_STATUSES = {403, 429}
MAX_LEASE_CRASHES = 2      # a worker died while fetching it this often: a poison item, give up
BACKOFF_MIN = [30, 120, 480]       # after failure 1, 2, 3
LEASE_S = 15 * 60
DOC_RE = re.compile(r"\.(pdf|xlsx?|csv)(\?|#|$)", re.I)
EVIDENCE_RE = re.compile(r"delist|exit offer|reverse book|compulsor|suspen|liquidat|nclt", re.I)
STATUSES = ("parked", "pending", "fetching", "fetched", "parsed", "failed", "dead", "parse_error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  url TEXT PRIMARY KEY, domain TEXT, kind TEXT, status TEXT, priority INTEGER, year_tag TEXT,
  query TEXT, provider TEXT, rank INTEGER, title TEXT, snippet TEXT, evidence INTEGER DEFAULT 0,
  harvested_from TEXT, discovered_at REAL, discovered_run TEXT,
  attempts INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0, lease_until REAL DEFAULT 0,
  http_status INTEGER, last_error TEXT, local_path TEXT, fetched_at REAL,
  parsed_at REAL, rows INTEGER, relevance TEXT, updated_at REAL, company TEXT, company_name TEXT,
  crashes INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS items_queue ON items (status, priority DESC, discovered_at);
CREATE INDEX IF NOT EXISTS items_path ON items (local_path);
CREATE TABLE IF NOT EXISTS domains (
  domain TEXT PRIMARY KEY, blocked INTEGER DEFAULT 0, consecutive_fails INTEGER DEFAULT 0,
  reason TEXT, blocked_at REAL, blocked_until REAL, block_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def priority(url: str, kind: str, year_tag: str | None, evidence: bool, known: list[str], rank: int | None,
             focus: tuple[int, int] | None = None) -> int:
    """Higher = fetched sooner. Historical records first: official and archive sources, documents, years in
    the focus window (15-20 years ago) and delisting evidence; recent news last."""
    p = 50
    t = historical.tier(url)
    p += 25 if t == 0 else 0                       # official (NSE/BSE/SEBI/...) or archive (Wayback)
    p -= 15 if t == 2 else 0                       # news sites
    p += 5 if t == 1 and domain_matches(url, known) else 0
    p += 20 if DOC_RE.search(url) else 0
    p += 15 if kind in ("harvested", "archive") else 0
    p += 15 if evidence else 0
    p += 15 if focus and historical.in_window(year_tag, focus) else 0
    p += 5 if str(year_tag or "").isdigit() and int(year_tag) in PRIORITY_YEARS else 0
    p -= min(rank or 0, 20)
    return p


# domains blocked right now (expired blocks don't count); one parameter: now
BLOCKED_NOW = ("SELECT domain FROM domains WHERE blocked=1 AND "
               f"COALESCE(blocked_until, COALESCE(blocked_at, 0) + {LEGACY_BLOCK_S}) > ?")


class Docket:
    def __init__(self, path: Path | None = None):
        self.path = path or DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60, isolation_level=None)  # autocommit; explicit txns
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        for col, typ in (("company", "TEXT"), ("company_name", "TEXT"), ("crashes", "INTEGER DEFAULT 0")):
            if col not in cols:  # dockets created by older versions
                self.db.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
        dcols = {r["name"] for r in self.db.execute("PRAGMA table_info(domains)")}
        for col, typ in (("blocked_until", "REAL"), ("block_count", "INTEGER DEFAULT 0")):
            if col not in dcols:
                self.db.execute(f"ALTER TABLE domains ADD COLUMN {col} {typ}")
        self.db.execute("CREATE INDEX IF NOT EXISTS items_company ON items (company)")
        self.focus = historical.window()

    @contextmanager
    def txn(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # --- meta -----------------------------------------------------------------------------------
    def get_meta(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v)))

    # --- discovery side -------------------------------------------------------------------------
    def file(self, items: list[dict], known: list[str], fetched: dict | None = None) -> dict:
        """Add candidate URLs. Each item: url, kind (search|harvested|deferred), year_tag, and optionally
        query, provider, rank, title, snippet, harvested_from, parked (bool), run_id, company, company_name.
        A URL already in the docket keeps its row; a parked one becomes pending if filed as not parked.
        URLs already in the fetch manifest (`fetched`: url -> entry) are filed as fetched / dead."""
        now = time.time()
        out = {"new": 0, "pending": 0, "parked": 0, "already_known": 0, "promoted": 0}
        with self.txn() as db:
            for it in items:
                url = it["url"]
                row = db.execute("SELECT status FROM items WHERE url=?", (url,)).fetchone()
                text = f"{it.get('title') or ''} {it.get('snippet') or ''} {url}"
                ev = bool(EVIDENCE_RE.search(text))
                if row:
                    out["already_known"] += 1
                    # keep the better metadata: a company tag, delisting evidence, a higher priority (never overwrite)
                    db.execute("UPDATE items SET company=COALESCE(company, ?), company_name=COALESCE(company_name, ?),"
                               " evidence=MAX(evidence, ?), priority=MAX(priority, ?), updated_at=? WHERE url=?",
                               (it.get("company"), it.get("company_name"), int(ev),
                                priority(url, it.get("kind", "search"), it.get("year_tag"), ev, known, it.get("rank"),
                                         self.focus), now, url))
                    if row["status"] == "parked" and not it.get("parked"):
                        db.execute("UPDATE items SET status='pending', updated_at=? WHERE url=?", (now, url))
                        out["promoted"] += 1
                    continue
                status, http, path = ("parked" if it.get("parked") else "pending"), None, None
                m = (fetched or {}).get(url)
                if m:
                    http, path = m.get("http_status"), m.get("local_path")
                    status = "fetched" if http == 200 else "dead"
                db.execute(
                    "INSERT INTO items (url, domain, kind, status, priority, year_tag, query, provider, rank, title,"
                    " snippet, evidence, harvested_from, discovered_at, discovered_run, http_status, local_path,"
                    " last_error, updated_at, company, company_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (url, domain_of(url), it.get("kind", "search"), status,
                     priority(url, it.get("kind", "search"), it.get("year_tag"), ev, known, it.get("rank"), self.focus),
                     it.get("year_tag"), it.get("query"), it.get("provider"), it.get("rank"),
                     (it.get("title") or "")[:300], (it.get("snippet") or "")[:500], int(ev),
                     it.get("harvested_from"), now, it.get("run_id"), http, path,
                     f"http {http}" if status == "dead" else None, now, it.get("company"), it.get("company_name")))
                out["new"] += 1
                out[status if status in ("pending", "parked") else "already_fetched"] = \
                    out.get(status if status in ("pending", "parked") else "already_fetched", 0) + 1
        return out

    # --- fetch side -----------------------------------------------------------------------------
    def blocked(self) -> set[str]:
        """Domains blocked right now (expired blocks are over)."""
        return {r["domain"] for r in self.db.execute(BLOCKED_NOW, (time.time(),))}

    def candidates(self, limit: int = 1000, per_domain: int = CANDIDATES_PER_DOMAIN) -> list[sqlite3.Row]:
        """Items a fetch worker may take now, best first, at most per_domain per domain, so that a domain waiting
        out its delay or over its budget never hides the ready items of other domains."""
        now = time.time()
        return self.db.execute(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY domain ORDER BY priority DESC, discovered_at) rn"
            " FROM items WHERE ((status IN ('pending','failed') AND next_attempt_at<=?)"
            " OR (status='fetching' AND lease_until<?))"
            f" AND domain NOT IN ({BLOCKED_NOW})) WHERE rn<=?"
            " ORDER BY priority DESC, discovered_at LIMIT ?", (now, now, now, per_domain, limit)).fetchall()

    def next_retry_at(self) -> float | None:
        now = time.time()
        r = self.db.execute("SELECT MIN(next_attempt_at) t FROM items WHERE status IN ('pending','failed')"
                            f" AND domain NOT IN ({BLOCKED_NOW})", (now,)).fetchone()
        return r["t"]

    def lease(self, url: str) -> bool:
        """Take an item for fetching. An expired lease means the worker that held it died mid-fetch: that
        counts as a failed attempt, and an item that killed MAX_LEASE_CRASHES workers is marked dead."""
        now = time.time()
        with self.txn() as db:
            row = db.execute("SELECT status, attempts, crashes, lease_until, next_attempt_at FROM items WHERE url=?",
                             (url,)).fetchone()
            if not row:
                return False
            if row["status"] == "fetching" and row["lease_until"] < now:
                attempts, crashes = row["attempts"] + 1, (row["crashes"] or 0) + 1
                if crashes >= MAX_LEASE_CRASHES or attempts >= MAX_ATTEMPTS:
                    db.execute("UPDATE items SET status='dead', attempts=?, crashes=?, lease_until=0, updated_at=?,"
                               " last_error=? WHERE url=?",
                               (attempts, crashes, now, f"worker stopped while fetching it {crashes} time(s) "
                                                        "(crash or kill): given up", url))
                    log(f"docket: {url} marked dead after {crashes} worker crash(es) while fetching it",
                        level="WARN", kind="docket_poison_item", url=url, crashes=crashes)
                    return False
                db.execute("UPDATE items SET status='fetching', attempts=?, crashes=?, lease_until=?, updated_at=?,"
                           " last_error='worker stopped while fetching it (crash or kill)' WHERE url=?",
                           (attempts, crashes, now + LEASE_S, now, url))
                return True
            if row["status"] in ("pending", "failed") and row["next_attempt_at"] <= now:
                db.execute("UPDATE items SET status='fetching', lease_until=?, updated_at=? WHERE url=?",
                           (now + LEASE_S, now, url))
                return True
        return False

    def release(self, url: str) -> None:
        """Give a leased item back without counting an attempt (graceful stop mid-fetch)."""
        self.db.execute("UPDATE items SET status=CASE WHEN attempts>0 THEN 'failed' ELSE 'pending' END, lease_until=0,"
                        " updated_at=? WHERE url=? AND status='fetching'", (time.time(), url))

    def done_fetch(self, url: str, entry: dict) -> str:
        """Record a fetch attempt (a fetch_manifest entry). Returns the new status, or "unknown" (nothing
        recorded) for a URL that is not in the docket."""
        now, ok = time.time(), not entry.get("quarantined")
        st = entry.get("http_status")
        with self.txn() as db:
            row = db.execute("SELECT attempts FROM items WHERE url=?", (url,)).fetchone()
            if not row:
                log(f"docket: fetch result for a URL that is not in the docket: {url}", level="WARN",
                    kind="docket_unknown_url", url=url)
                return "unknown"
            attempts = row["attempts"] + 1
            if ok:
                status, nxt, err = "fetched", 0, None
            else:
                err = entry.get("error") or ("soft block page" if entry.get("soft_block") else
                                             (f"http {st}" if st else "no response") + (" (empty body)" if st == 200 else ""))
                kind = failure_class(entry)
                if kind == "definitive":
                    status, nxt = "dead", 0
                elif kind == "throttled":
                    status, nxt = ("dead", 0) if attempts >= MAX_THROTTLED_ATTEMPTS else \
                        ("failed", now + 60 * THROTTLE_BACKOFF_MIN[min(attempts, len(THROTTLE_BACKOFF_MIN)) - 1])
                elif attempts >= MAX_ATTEMPTS:
                    status, nxt = "dead", 0
                else:
                    status, nxt = "failed", now + 60 * BACKOFF_MIN[min(attempts, len(BACKOFF_MIN)) - 1]
            db.execute("UPDATE items SET status=?, attempts=?, next_attempt_at=?, lease_until=0, http_status=?,"
                       " last_error=?, local_path=COALESCE(?, local_path), fetched_at=?, updated_at=? WHERE url=?",
                       (status, attempts, nxt, st, err, entry.get("local_path") if ok else None,
                        now if ok else None, now, url))
        return status

    def domain_result(self, domain: str, ok: bool, http_status, block_after: int) -> bool:
        """Track consecutive refusals per domain: 403/429/5xx (soft blocks are passed as 403). Missing pages
        (404/410…) and network errors (http_status None) don't count. A block expires (6 h, 24 h, 72 h); a success
        clears it. True if the domain just got blocked."""
        refusal = isinstance(http_status, int) and (http_status in THROTTLE_STATUSES or http_status >= 500)
        if not ok and not refusal:
            return False
        now = time.time()
        with self.txn() as db:
            db.execute("INSERT OR IGNORE INTO domains (domain) VALUES (?)", (domain,))
            if ok:
                db.execute("UPDATE domains SET consecutive_fails=0, blocked=0 WHERE domain=?", (domain,))
                return False
            db.execute("UPDATE domains SET consecutive_fails=consecutive_fails+1 WHERE domain=?", (domain,))
            r = db.execute("SELECT consecutive_fails, blocked, blocked_until, blocked_at, block_count FROM domains"
                           " WHERE domain=?", (domain,)).fetchone()
            active = r["blocked"] and (r["blocked_until"] or (r["blocked_at"] or 0) + LEGACY_BLOCK_S) > now
            if r["consecutive_fails"] >= block_after and not active:
                n = (r["block_count"] or 0) + 1
                hours = BLOCK_HOURS[min(n, len(BLOCK_HOURS)) - 1]
                db.execute("UPDATE domains SET blocked=1, reason=?, blocked_at=?, blocked_until=?, block_count=?,"
                           " consecutive_fails=0 WHERE domain=?",
                           (f"{r['consecutive_fails']} refusals in a row (last: http {http_status}); block #{n} for {hours} h",
                            now, now + hours * 3600, n, domain))
                return True
        return False

    def block(self, domain: str, reason: str, hours: float = LEGACY_BLOCK_S / 3600) -> None:
        now = time.time()
        self.db.execute("INSERT INTO domains (domain, blocked, reason, blocked_at, blocked_until) VALUES (?,1,?,?,?) "
                        "ON CONFLICT(domain) DO UPDATE SET blocked=1, reason=excluded.reason, blocked_at=excluded.blocked_at,"
                        " blocked_until=excluded.blocked_until", (domain, reason, now, now + hours * 3600))

    def unblock(self, domain: str) -> int:
        return self.db.execute("UPDATE domains SET blocked=0, consecutive_fails=0 WHERE domain=?", (domain,)).rowcount

    def refund_attempt(self, url: str, retry_in_s: float = 600) -> None:
        """Undo the attempt a network outage cost an item (the site was never reached): retry it soon."""
        self.db.execute("UPDATE items SET attempts=MAX(attempts-1, 0), status=CASE WHEN attempts-1>0 THEN 'failed'"
                        " ELSE 'pending' END, next_attempt_at=?, updated_at=? WHERE url=? AND status IN ('failed','dead')",
                        (time.time() + retry_in_s, time.time(), url))

    def park(self, url: str, reason: str) -> None:
        self.db.execute("UPDATE items SET status='parked', lease_until=0, last_error=?, updated_at=? WHERE url=?",
                        (reason, time.time(), url))

    def company_docs(self, company: str) -> int:
        """Documents fetched (or being fetched) for a company, all runs."""
        return self.db.execute("SELECT COUNT(*) n FROM items WHERE company=? AND status IN ('fetching','fetched','parsed')",
                               (company,)).fetchone()["n"]

    def docs_by_company(self) -> dict[str, int]:
        return {r["company"]: r["n"] for r in self.db.execute(
            "SELECT company, COUNT(*) n FROM items WHERE company IS NOT NULL AND status IN ('fetched','parsed')"
            " GROUP BY company")}

    def remaining(self) -> int:
        """Items still to download some day: pending or retrying, incl. blocked domains and backoff."""
        return self.db.execute("SELECT COUNT(*) n FROM items WHERE status IN ('pending','failed')").fetchone()["n"]

    # --- process side ---------------------------------------------------------------------------
    def fetched_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) n FROM items WHERE status='fetched'").fetchone()["n"]

    def sync_parsed(self, parse_errors: list[dict] | None = None) -> dict:
        """Mark fetched items whose file has an up-to-date parser output as parsed (rows, relevance)."""
        errors = {e["file"]: e["error"] for e in parse_errors or []}
        rel_path = PARSED / "_relevance.json"
        relevance = json.loads(rel_path.read_text(encoding="utf-8")) if rel_path.exists() else {}
        root = DATA.parent
        out = {"parsed": 0, "rows": 0, "parse_error": 0, "waiting": 0}
        rows = self.db.execute("SELECT url, local_path FROM items WHERE status='fetched'").fetchall()
        now = time.time()
        with self.txn() as db:
            for r in rows:
                lp = r["local_path"]
                if not lp or not (root / lp).exists():
                    db.execute("UPDATE items SET status='dead', last_error='downloaded file missing', updated_at=?"
                               " WHERE url=?", (now, r["url"]))
                    continue
                art = root / lp
                parsed = PARSED / f"{art.parent.name}__{art.stem}.jsonl"
                if lp in errors:
                    db.execute("UPDATE items SET status='parse_error', last_error=?, updated_at=? WHERE url=?",
                               (errors[lp][:300], now, r["url"]))
                    out["parse_error"] += 1
                elif parsed.exists() and parsed.stat().st_mtime >= art.stat().st_mtime:
                    n = len(read_jsonl(parsed))
                    db.execute("UPDATE items SET status='parsed', rows=?, relevance=?, parsed_at=?, updated_at=?"
                               " WHERE url=?", (n, relevance.get(lp), now, now, r["url"]))
                    out["parsed"] += 1
                    out["rows"] += n
                else:
                    out["waiting"] += 1
        return out

    # --- reporting / maintenance ----------------------------------------------------------------
    def stats(self) -> dict:
        q = self.db.execute
        by_status = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM items GROUP BY status")}
        now = time.time()
        ready = q("SELECT COUNT(*) n FROM items WHERE status IN ('pending','failed') AND next_attempt_at<=?"
                  f" AND domain NOT IN ({BLOCKED_NOW})", (now, now)).fetchone()["n"]
        waiting_blocked = q("SELECT COUNT(*) n FROM items WHERE status IN ('pending','failed')"
                            f" AND domain IN ({BLOCKED_NOW})", (now,)).fetchone()["n"]
        top = [dict(r) for r in q("SELECT domain, COUNT(*) n FROM items WHERE status IN ('pending','failed')"
                                  " GROUP BY domain ORDER BY n DESC LIMIT 8")]
        dead = [dict(r) for r in q("SELECT COALESCE(last_error,'?') reason, COUNT(*) n FROM items WHERE status='dead'"
                                   " GROUP BY reason ORDER BY n DESC LIMIT 6")]
        zero = q("SELECT COUNT(*) n FROM items WHERE status='parsed' AND rows=0 AND local_path NOT LIKE '%.htm%'"
                 " AND COALESCE(relevance,'') NOT LIKE 'irrelevant%'").fetchone()["n"]
        return {"total": sum(by_status.values()), "by_status": by_status,
                "by_kind": {r["kind"]: r["n"] for r in q("SELECT kind, COUNT(*) n FROM items GROUP BY kind")},
                "fetch_ready_now": ready, "waiting_on_blocked_domain": waiting_blocked,
                "awaiting_parse": by_status.get("fetched", 0), "parsed_zero_rows_relevant": zero,
                "evidence_unfetched": q("SELECT COUNT(*) n FROM items WHERE evidence=1 AND status IN"
                                        " ('parked','pending','failed')").fetchone()["n"],
                "blocked_domains": sorted(self.blocked()), "top_pending_domains": top, "dead_reasons": dead,
                "historical": historical.coverage(self.focus, self.docs_by_company())}

    def write_stats(self) -> dict:
        s = {"generated_at": now_iso(), **self.stats()}
        METADATA.mkdir(parents=True, exist_ok=True)
        atomic_write_text(STATS, json.dumps(s, indent=1))
        return s

    def listing(self, status: str, limit: int) -> list[dict]:
        cols = "url, kind, status, priority, year_tag, attempts, http_status, last_error, local_path, rows, query"
        return [dict(r) for r in self.db.execute(
            f"SELECT {cols} FROM items WHERE status=? ORDER BY priority DESC, updated_at DESC LIMIT ?", (status, limit))]

    def promote(self, query: str | None, limit: int) -> int:
        sql = "UPDATE items SET status='pending', updated_at=? WHERE url IN (SELECT url FROM items WHERE status='parked'"
        args: list = [time.time()]
        if query:
            sql += " AND query LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY priority DESC LIMIT ?)"
        return self.db.execute(sql, (*args, limit)).rowcount

    def retry(self, status: str, domain: str | None) -> int:
        sql = "UPDATE items SET status=?, attempts=0, next_attempt_at=0, updated_at=? WHERE status=?"
        args: list = ["fetched" if status == "parse_error" else "pending", time.time(), status]
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        return self.db.execute(sql, args).rowcount


def failure_class(entry: dict) -> str:
    """definitive (dead now) | throttled (long backoff) | transient (normal backoff)."""
    st = entry.get("http_status")
    if entry.get("permanent"):
        return "definitive"
    if entry.get("soft_block") or st in THROTTLE_STATUSES:
        return "throttled"
    if isinstance(st, int) and (st in DEFINITIVE_STATUSES or (400 <= st < 500 and st not in (408, 425))):
        return "definitive"
    return "transient"


def import_existing(dk: Docket) -> dict | None:
    """One-time: file what the pipeline knew before the docket existed (fetch manifest, deferred URLs,
    unfetched document links on downloaded pages, unfetched search results, blocked domains)."""
    if dk.get_meta("imported_at"):
        return None
    import polite_fetcher
    from common import MANIFEST, SEARCH_CACHE
    policy = polite_fetcher.fetch_policy()
    known = policy["known"]
    done = polite_fetcher.already_fetched()
    items, out = [], {}
    # 1. everything ever downloaded (or definitively missing)
    items += [{"url": u, "kind": "harvested" if e.get("harvested_from") else "search", "year_tag": e.get("year_tag"),
               "harvested_from": e.get("harvested_from")} for u, e in done.items()]
    # 2. retryable failures that never succeeded
    fails: dict[str, dict] = {}
    for e in read_jsonl(MANIFEST):
        if e["url"] not in done:
            fails[e["url"]] = e
    items += [{"url": u, "kind": "harvested" if e.get("harvested_from") else "search", "year_tag": e.get("year_tag"),
               "harvested_from": e.get("harvested_from")} for u, e in fails.items() if polite_fetcher.allowed(u, policy)]
    # 3. URLs a domain budget deferred
    state_p = METADATA / "collection_state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    items += [{"url": d["url"], "kind": "deferred", "year_tag": d.get("year_tag")} for d in state.get("deferred_urls", [])]
    # 4. document links on downloaded pages
    root = DATA.parent
    for e in done.values():
        lp = e.get("local_path") or ""
        if lp.endswith((".html", ".htm")) and (root / lp).exists() and polite_fetcher.allowed(e["url"], policy):
            items += [{"url": u, "kind": "harvested", "year_tag": e.get("year_tag"), "harvested_from": e["url"]}
                      for u in polite_fetcher.harvest_links(root / lp, e["url"], policy)]
    # 5. search results never fetched: kept as parked evidence
    for p in sorted(SEARCH_CACHE.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rank, r in enumerate(d.get("results", []), 1):
            if r.get("url") and polite_fetcher.allowed(r["url"], policy):
                items.append({"url": r["url"], "kind": "search", "year_tag": d.get("year_tag"), "query": d.get("query"),
                              "provider": d.get("provider", "searxng"), "rank": rank, "title": r.get("title"),
                              "snippet": r.get("content"), "parked": True})
    out["filed"] = dk.file(items, known, done)
    # failed URLs keep their attempt count, so they back off instead of being hammered
    from collections import Counter
    tries = Counter(x["url"] for x in read_jsonl(MANIFEST))
    with dk.txn() as db:
        for u, e in fails.items():
            n = tries[u]
            db.execute("UPDATE items SET status='failed', attempts=?, http_status=?, last_error=? WHERE url=? AND status='pending'",
                       (min(n, MAX_ATTEMPTS - 1), e.get("http_status"), e.get("error") or f"http {e.get('http_status')}", u))
    for d in state.get("blocked_domains", []):
        dk.block(d, "blocked before the docket existed")
    out["blocked_domains"] = len(state.get("blocked_domains", []))
    out["sync"] = dk.sync_parsed()
    dk.set_meta("imported_at", now_iso())
    log(f"docket: imported existing pipeline state {out}", kind="docket_import", **out)
    return out


def open_docket() -> Docket:
    dk = Docket()
    import_existing(dk)
    return dk


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--list", choices=STATUSES)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--retry", choices=("dead", "failed", "parse_error"))
    ap.add_argument("--domain")
    ap.add_argument("--unblock", metavar="DOMAIN")
    a = ap.parse_args()
    dk = open_docket()
    if a.list:
        emit({"status": a.list, "items": dk.listing(a.list, a.limit)})
    elif a.promote:
        emit({"promoted": dk.promote(a.query, a.limit)})
    elif a.retry:
        emit({"requeued": dk.retry(a.retry, a.domain)})
    elif a.unblock:
        emit({"unblocked": dk.unblock(a.unblock)})
    else:
        emit(dk.write_stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
