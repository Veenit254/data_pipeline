"""Regression tests: domain failures and blocks, retry policy, network outages, docket starvation, company tags,
search fallback, corrupt cache, download size cap, soft blocks, disk protection."""
import json
import os
import time
from collections import Counter

import pytest
import requests

import common
import docket
import fetch_worker
import polite_fetcher as pf
import process_worker
import web_search as ws
from search_providers.base import Answer, SearchProvider


@pytest.fixture
def dk(tmp_path, monkeypatch):
    monkeypatch.setattr(docket, "DB", tmp_path / "docket.sqlite")
    monkeypatch.setattr(docket, "STATS", tmp_path / "docket_stats.json")
    monkeypatch.setattr(docket, "import_existing", lambda d: None)
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    return docket.Docket()


KNOWN = ["nseindia.com", "bseindia.com", "sebi.gov.in"]


def file(dk, urls, **kw):
    dk.file([{"url": u, "kind": "search", "year_tag": "2017", **kw} for u in urls], KNOWN)


# ---------- O1: which failures block a domain, and for how long --------------------------------------------
def test_timeouts_and_connection_errors_never_block_a_domain(dk):
    for _ in range(10):
        assert not dk.domain_result("nseindia.com", False, None, 3)
    assert "nseindia.com" not in dk.blocked()


def test_server_refusals_block_temporarily_and_blocks_expire(dk):
    for st in (403, 429, 503):
        blocked = dk.domain_result("nseindia.com", False, st, 3)
    assert blocked and "nseindia.com" in dk.blocked()
    until = dk.db.execute("SELECT blocked_until FROM domains").fetchone()["blocked_until"]
    assert 5 * 3600 < until - time.time() <= 6 * 3600 + 5
    dk.db.execute("UPDATE domains SET blocked_until=?", (time.time() - 1,))
    assert "nseindia.com" not in dk.blocked()                       # expired: the domain is tried again


def test_repeated_blocks_last_longer_and_success_clears(dk):
    for _ in range(2):
        for _ in range(3):
            dk.domain_result("nseindia.com", False, 429, 3)
        dk.db.execute("UPDATE domains SET blocked_until=?", (time.time() - 1,))
    for _ in range(3):
        dk.domain_result("nseindia.com", False, 429, 3)
    assert dk.db.execute("SELECT blocked_until FROM domains").fetchone()["blocked_until"] - time.time() > 70 * 3600
    dk.domain_result("nseindia.com", True, 200, 3)
    row = dk.db.execute("SELECT blocked, consecutive_fails FROM domains").fetchone()
    assert (row["blocked"], row["consecutive_fails"]) == (0, 0)


def test_legacy_blocks_without_an_expiry_expire_after_a_day(dk):
    dk.block("bseindia.com", "blocked before the docket existed")
    dk.db.execute("UPDATE domains SET blocked_until=NULL, blocked_at=?", (time.time() - 2 * 86400,))
    assert "bseindia.com" not in dk.blocked()


# ---------- O2: retry policy per failure class ---------------------------------------------------------------
@pytest.mark.parametrize("status", [403, 429])
def test_throttling_is_retried_with_long_backoff_not_killed(dk, status):
    u = "https://www.nseindia.com/x.pdf"
    file(dk, [u])
    for _ in range(docket.MAX_ATTEMPTS + 2):
        dk.db.execute("UPDATE items SET next_attempt_at=0 WHERE url=?", (u,))
        st = dk.done_fetch(u, {"http_status": status, "quarantined": True})
    assert st == "failed"
    nxt = dk.db.execute("SELECT next_attempt_at FROM items").fetchone()["next_attempt_at"]
    assert nxt - time.time() >= 8 * 3600 - 5
    for _ in range(docket.MAX_THROTTLED_ATTEMPTS):
        st = dk.done_fetch(u, {"http_status": status, "quarantined": True})
    assert st == "dead"                                             # bounded: not retried forever


@pytest.mark.parametrize("status", [400, 401, 404, 410])
def test_definitive_client_errors_are_dead_at_once(dk, status):
    u = "https://www.nseindia.com/gone.pdf"
    file(dk, [u])
    assert dk.done_fetch(u, {"http_status": status, "quarantined": True}) == "dead"


def test_permanent_problems_are_dead_at_once(dk):
    u = "https://www.nseindia.com/huge.pdf"
    file(dk, [u])
    assert dk.done_fetch(u, {"http_status": 200, "quarantined": True, "permanent": True, "error": "too large"}) == "dead"


# ---------- O3: one slow or over-budget domain cannot starve the others ------------------------------------------
def test_candidates_include_every_ready_domain(dk):
    file(dk, [f"https://www.nseindia.com/delisting/{i}.pdf" for i in range(305)])
    file(dk, [f"https://example.org/news/{i}.html" for i in range(5)])
    domains = Counter(c["domain"] for c in dk.candidates())
    assert domains["example.org"] == 5 and domains["www.nseindia.com"] <= docket.CANDIDATES_PER_DOMAIN


POLICY = {"known": KNOWN, "deny": [], "open_web": True,
          "limits": {"default": {"max_per_run": 50, "delay_s": [0, 0]}, "nseindia.com": {"max_per_run": 2, "delay_s": [0, 0]}}}


def worker_env(monkeypatch, dk, fetch):
    monkeypatch.setattr(docket, "open_docket", lambda: dk)
    monkeypatch.setattr(pf, "fetch_one", fetch)
    monkeypatch.setattr(pf, "fetch_policy", lambda: POLICY)
    monkeypatch.setattr(pf, "already_fetched", lambda: {})
    monkeypatch.setattr(pf, "RUN_COUNTS", Counter())
    monkeypatch.setattr(fetch_worker, "plan", lambda: {"block_domain_after_failures": 3})
    monkeypatch.setattr(fetch_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(fetch_worker, "check_disk", lambda s: {"level": "ok"})
    monkeypatch.setattr(fetch_worker.time, "sleep", lambda s: None)


def test_over_budget_domain_does_not_end_the_run_early(dk, monkeypatch):
    file(dk, [f"https://www.nseindia.com/delisting/{i}.pdf" for i in range(305)])
    file(dk, [f"https://example.org/news/{i}.html" for i in range(5)])
    worker_env(monkeypatch, dk, lambda s, u, t, h=None: {"url": u, "http_status": 200, "quarantined": False,
                                                          "local_path": "data/raw/x.pdf"})
    out = fetch_worker.run(max_files=100)
    assert out["fetched_ok"] == 7 and out["stop_reason"] == "sources_exhausted"   # 2 NSE (its budget) + 5 others


# ---------- network outage: stop, don't burn attempts --------------------------------------------------------------
def test_network_outage_stops_the_worker_without_using_up_attempts(dk, monkeypatch):
    file(dk, [f"https://www.nseindia.com/{i}.pdf" for i in range(3)] + [f"https://www.bseindia.com/{i}.pdf" for i in range(3)])

    def down(s, u, t, h=None):
        return {"url": u, "http_status": None, "quarantined": True, "error": "ConnectionError: Failed to resolve host"}
    worker_env(monkeypatch, dk, down)
    out = fetch_worker.run(max_files=100)
    assert out["stop_reason"] == "network_unavailable"
    rows = dk.db.execute("SELECT status, attempts FROM items WHERE attempts > 0").fetchall()
    assert rows == []                                                # attempts given back
    assert not dk.blocked()


# ---------- O4: re-filing a URL keeps the better metadata --------------------------------------------------------
def test_refiling_adds_the_company_tag(dk):
    u = "https://www.bseindia.com/doc.pdf"
    file(dk, [u])
    file(dk, [u], company="acme", company_name="Acme Ltd", title="Acme delisting order")
    row = dk.db.execute("SELECT company, company_name, evidence FROM items").fetchone()
    assert (row["company"], row["company_name"], row["evidence"]) == ("acme", "Acme Ltd", 1)
    file(dk, [u], company="other", company_name="Other Ltd")
    assert dk.db.execute("SELECT company FROM items").fetchone()["company"] == "acme"   # never overwritten


# ---------- O5/O6: search fallback and corrupt cache ----------------------------------------------------------------
class Fake(SearchProvider):
    def __init__(self, name, answer):
        super().__init__({})
        self.name, self.answer = name, answer

    def search(self, session, query, page, units):
        return self.answer


def make_search(monkeypatch, tmp_path, answers, **settings):
    monkeypatch.setattr(ws, "ENGINE_STATE", tmp_path / "engine_state.json")
    monkeypatch.setattr(ws, "log", lambda *a, **k: None)
    monkeypatch.setattr(ws, "polite_sleep", lambda *a, **k: None)
    s = dict(ws.SEARCH_DEFAULTS, providers=list(answers), provider_settings={}, official_domains=["nseindia.com"], **settings)
    return ws.Search(s, registry={n: (lambda a, n=n: (lambda st: Fake(n, a)))(a) for n, a in answers.items()})


def test_empty_answer_falls_back_to_the_next_provider_when_enabled(monkeypatch, tmp_path):
    srch = make_search(monkeypatch, tmp_path, {"one": Answer([], []), "two": Answer([{"url": "https://www.nseindia.com/a.pdf"}], [])},
                       fallback_on_empty=True)
    ans, prov, _ = srch.page(None, "q", 1)
    assert prov == "two" and ans.results


def test_empty_fallback_is_off_by_default_to_protect_paid_credits(monkeypatch, tmp_path):
    srch = make_search(monkeypatch, tmp_path, {"one": Answer([], []), "two": Answer([{"url": "https://x.in/a"}], [])})
    ans, prov, _ = srch.page(None, "q", 1)
    assert prov == "one" and ans.results == []


def test_corrupt_cache_file_is_a_miss(monkeypatch, tmp_path):
    cache = tmp_path / "data" / "search_cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(ws, "SEARCH_CACHE", cache)
    srch = make_search(monkeypatch, tmp_path, {"one": Answer([{"url": "https://www.nseindia.com/a.pdf"}], [])})
    ws.cache_path("nse delisting 2017", 1).write_text('{"query": "nse delisting 2017", "results": [{"url": "htt')
    out = ws.run("nse delisting 2017", 1, "2017", srch)
    assert out["result_count"] == 1
    assert json.loads(ws.cache_path("nse delisting 2017", 1).read_text())["results"]   # repaired


# ---------- O8: size cap and soft blocks ------------------------------------------------------------------------------
class Resp:
    def __init__(self, status, body=b"", headers=None, chunks=None):
        self.status_code, self.headers, self._body = status, headers or {}, body
        self.chunks = chunks

    @property
    def content(self):
        return self._body

    def iter_content(self, n):
        yield from (self.chunks if self.chunks is not None else [self._body[i:i + n] for i in range(0, len(self._body), n)])

    def close(self):
        pass


class Session:
    def __init__(self, resp):
        self.resp, self.kw = resp, None

    def get(self, url, **kw):
        self.kw = kw
        return self.resp


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    monkeypatch.setattr(pf, "log", lambda *a, **k: None)
    monkeypatch.setattr(pf, "append_jsonl", lambda *a, **k: None)
    monkeypatch.setattr(pf, "QUARANTINE", tmp_path / "q")
    monkeypatch.setattr(pf, "raw_dir_for_tag", lambda t: tmp_path / "raw")
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "max_download_bytes", lambda: 1000)
    return tmp_path


def test_oversized_download_is_refused_while_streaming(quiet):
    s = Session(Resp(200, headers={"Content-Type": "application/pdf"}, chunks=[b"%PDF" + b"0" * 600] * 3))
    e = pf.fetch_one(s, "https://www.nseindia.com/huge.pdf", "2017")
    assert s.kw.get("stream") is True
    assert e["quarantined"] and e["permanent"] and "too large" in e["error"] and e["bytes"] == 0
    assert not list((quiet / "raw").glob("*")) if (quiet / "raw").exists() else True


def test_declared_oversize_is_refused_before_reading(quiet):
    s = Session(Resp(200, headers={"Content-Type": "application/pdf", "Content-Length": "5000000"}, chunks=[]))
    e = pf.fetch_one(s, "https://www.nseindia.com/huge.pdf", "2017")
    assert e["permanent"] and "too large" in e["error"]


@pytest.mark.parametrize("body,ctype", [
    (b"<html><title>Access Denied</title>Reference #18.abc</html>", "text/html"),
    (b"<!DOCTYPE html><html><body>Request unsuccessful. Incapsula incident ID</body></html>", "text/html"),
    (b"<html><body>hello</body></html>", "application/pdf"),
])
def test_soft_block_pages_are_not_documents(quiet, body, ctype):
    e = pf.fetch_one(Session(Resp(200, body, {"Content-Type": ctype})), "https://nsearchives.nseindia.com/content/delisted.pdf", "2017")
    assert e["quarantined"] and e["soft_block"] and e["http_status"] == 200


def test_real_html_page_is_still_fine(quiet):
    body = b"<html><title>Orders of delisting</title>" + b"<a href='x.pdf'>order</a>" * 20 + b"</html>"
    e = pf.fetch_one(Session(Resp(200, body, {"Content-Type": "text/html"})), "https://www.nseindia.com/delisting-orders", "2017")
    assert not e["quarantined"] and not e.get("soft_block")


def test_soft_block_counts_as_a_refusal(dk):
    u = "https://www.nseindia.com/x.pdf"
    file(dk, [u])
    assert dk.done_fetch(u, {"http_status": 200, "quarantined": True, "soft_block": True}) == "failed"


# ---------- O7: disk protection in the process worker; leftovers cleaned ------------------------------------------
def test_process_worker_stops_on_critical_disk(dk, monkeypatch):
    monkeypatch.setattr(process_worker.docket, "open_docket", lambda: dk)
    monkeypatch.setattr(process_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(dk, "fetched_count", lambda: 5)
    monkeypatch.setattr(process_worker, "check_disk", lambda s: {"level": "critical"})
    monkeypatch.setattr(process_worker, "process_once", lambda d: pytest.fail("must not parse/OCR on a full disk"))
    assert process_worker.run(batch=1, follow=None)["stop_reason"] == "low_disk_space"


def test_stale_partial_files_are_removed(tmp_path):
    old, new, keep = tmp_path / ".a.pdf.part", tmp_path / ".b.pdf.part", tmp_path / "c.pdf"
    for f in (old, new, keep):
        f.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    stale = tmp_path / ".master.csv.123.tmp"
    stale.write_text("x")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    assert common.cleanup_partials([tmp_path]) == 2
    assert not old.exists() and not stale.exists() and new.exists() and keep.exists()


def test_non_numeric_disk_setting_falls_back_to_the_default(tmp_path, monkeypatch):
    (tmp_path / "query_plan.json").write_text(json.dumps({"disk": {"min_free_gb": "twenty"}}))
    monkeypatch.setattr(common, "CONFIG", tmp_path)
    assert common.disk_settings()["min_free_gb"] == common.DISK_DEFAULTS["min_free_gb"]


def test_requests_exceptions_still_import():
    assert issubclass(requests.ConnectionError, requests.RequestException)
