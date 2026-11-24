import json
import time

import pytest

import collect
import docket
import fetch_worker
import polite_fetcher

KNOWN = ["nseindia.com"]


@pytest.fixture
def dk(tmp_path, monkeypatch):
    monkeypatch.setattr(docket, "DB", tmp_path / "docket.sqlite")
    monkeypatch.setattr(docket, "STATS", tmp_path / "docket_stats.json")
    monkeypatch.setattr(docket, "import_existing", lambda d: None)
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    return docket.Docket()


def item(url, **kw):
    return {"url": url, "kind": "search", "year_tag": "2017", **kw}


def test_filing_dedupes_parks_and_promotes(dk):
    r = dk.file([item("https://a.in/1"), item("https://a.in/2", parked=True), item("https://a.in/1")], KNOWN)
    assert r["new"] == 2 and r["pending"] == 1 and r["parked"] == 1 and r["already_known"] == 1
    assert dk.file([item("https://a.in/2")], KNOWN)["promoted"] == 1
    assert dk.stats()["by_status"] == {"pending": 2}


def test_already_downloaded_urls_are_not_queued_again(dk):
    done = {"https://a.in/x.pdf": {"http_status": 200, "local_path": "data/raw/x.pdf"},
            "https://a.in/gone": {"http_status": 404}}
    dk.file([item("https://a.in/x.pdf"), item("https://a.in/gone")], KNOWN, done)
    assert dk.stats()["by_status"] == {"fetched": 1, "dead": 1}


def test_official_documents_with_evidence_come_first(dk):
    dk.file([item("https://blog.example/post", rank=1),
             item("https://www.nseindia.com/orders/delisting.pdf", rank=5, title="Compulsory delisting order"),
             item("https://www.nseindia.com/page", rank=2)], KNOWN)
    assert [c["url"] for c in dk.candidates()][0] == "https://www.nseindia.com/orders/delisting.pdf"


def test_lease_is_exclusive(dk):
    dk.file([item("https://a.in/1")], KNOWN)
    assert dk.lease("https://a.in/1") and not dk.lease("https://a.in/1")
    assert dk.candidates() == []


def test_failures_back_off_then_die_and_404_dies_at_once(dk):
    dk.file([item("https://a.in/1"), item("https://a.in/2")], KNOWN)
    assert dk.done_fetch("https://a.in/2", {"http_status": 404, "quarantined": True}) == "dead"
    for n in range(1, docket.MAX_ATTEMPTS):
        assert dk.done_fetch("https://a.in/1", {"http_status": 503, "quarantined": True}) == "failed"
        row = dk.db.execute("SELECT next_attempt_at FROM items WHERE url='https://a.in/1'").fetchone()
        assert row[0] == pytest.approx(time.time() + 60 * docket.BACKOFF_MIN[n - 1], abs=5)
        assert dk.candidates() == []  # not before the backoff ends
        dk.db.execute("UPDATE items SET next_attempt_at=0")
    assert dk.done_fetch("https://a.in/1", {"http_status": 503, "quarantined": True}) == "dead"


def test_domain_blocking_parks_its_items(dk):
    dk.file([item("https://bad.in/1"), item("https://ok.in/1")], KNOWN)
    assert not dk.domain_result("bad.in", False, 404, 2)  # missing files don't count
    assert not dk.domain_result("bad.in", False, 403, 2)
    assert dk.domain_result("bad.in", False, 403, 2)
    assert [c["url"] for c in dk.candidates()] == ["https://ok.in/1"]
    assert dk.stats()["waiting_on_blocked_domain"] == 1
    dk.unblock("bad.in")
    assert len(dk.candidates()) == 2


def test_sync_parsed_marks_rows_and_parse_errors(dk, tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(docket, "DATA", root / "data")
    parsed = root / "data" / "processed" / "parsed"
    parsed.mkdir(parents=True)
    monkeypatch.setattr(docket, "PARSED", parsed)
    raw = root / "data" / "raw" / "priority_2017"
    raw.mkdir(parents=True)
    for n in ("a", "b"):
        (raw / f"{n}.pdf").write_bytes(b"%PDF")
    (parsed / "priority_2017__a.jsonl").write_text('{"x": 1}\n{"x": 2}\n')
    done = {f"https://a.in/{n}.pdf": {"http_status": 200, "local_path": f"data/raw/priority_2017/{n}.pdf"} for n in "ab"}
    dk.file([item(u) for u in done], KNOWN, done)
    r = dk.sync_parsed([{"file": "data/raw/priority_2017/b.pdf", "error": "ValueError: bad"}])
    assert r["parsed"] == 1 and r["rows"] == 2 and r["parse_error"] == 1
    assert dk.stats()["by_status"] == {"parsed": 1, "parse_error": 1}


# --- fetch worker --------------------------------------------------------------------------------
POLICY = {"open_web": True, "known": KNOWN, "deny": [],
          "limits": {"default": {"max_per_run": 15, "delay_s": [0, 0]},
                     "nseindia.com": {"max_per_run": 2, "delay_s": [30, 30]}}}


@pytest.fixture
def worker(dk, monkeypatch, tmp_path):
    fetched = []

    def fake_fetch(session, url, tag, harvested_from=None):
        fetched.append(url)
        ok = "fail" not in url
        return {"url": url, "http_status": 200 if ok else 503, "quarantined": not ok,
                "local_path": f"data/raw/x/{len(fetched)}.pdf" if ok else None, "year_tag": tag}
    monkeypatch.setattr(polite_fetcher, "fetch_one", fake_fetch)
    monkeypatch.setattr(polite_fetcher, "fetch_policy", lambda: POLICY)
    monkeypatch.setattr(polite_fetcher, "already_fetched", lambda: {})
    monkeypatch.setattr(polite_fetcher, "RUN_COUNTS", __import__("collections").Counter())
    monkeypatch.setattr(fetch_worker, "plan", lambda: {"block_domain_after_failures": 3})
    monkeypatch.setattr(fetch_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(fetch_worker, "check_disk", lambda s: {"level": "ok"})
    return fetched


def test_worker_interleaves_domains_and_respects_budgets(dk, worker, monkeypatch):
    clock = [time.time()]  # fake clock: sleeping advances it, so domain delays pass instantly

    def sleep(s):
        clock[0] += s
    monkeypatch.setattr(fetch_worker.time, "time", lambda: clock[0])
    monkeypatch.setattr(fetch_worker.time, "sleep", sleep)
    dk.file([item(f"https://www.nseindia.com/{i}.pdf") for i in range(4)]
            + [item(f"https://other.in/{i}.pdf") for i in range(3)], KNOWN)
    out = fetch_worker.run(max_files=50)
    nse = [u for u in worker if "nseindia" in u]
    assert len(nse) == 2 and len(worker) == 5          # NSE budget 2; the rest of NSE stays pending
    assert worker.index(nse[1]) > worker.index(nse[0]) + 1  # other.in fetched while NSE waited out its delay
    assert out["stop_reason"] == "sources_exhausted"   # NSE items left, but over budget
    assert dk.stats()["by_status"]["pending"] == 2


def test_failed_downloads_stay_in_the_docket_for_retry(dk, worker, monkeypatch):
    monkeypatch.setattr(fetch_worker.time, "sleep", lambda s: None)
    dk.file([item("https://a.in/fail.pdf"), item("https://a.in/ok.pdf")], KNOWN)
    out = fetch_worker.run(max_files=10)
    assert out["fetched_ok"] == 1 and out["failed"] == 1 and out["stop_reason"] == "sources_exhausted"  # backoff
    assert dk.stats()["by_status"] == {"fetched": 1, "failed": 1}


# --- discovery -----------------------------------------------------------------------------------
def test_discovery_files_results_without_downloading(dk, tmp_path, monkeypatch):
    cache = tmp_path / "data" / "search_cache" / "q.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"query": "q", "provider": "searxng", "results": [
        {"url": f"https://www.nseindia.com/{i}.pdf", "title": "delisting", "content": ""} for i in range(5)]
        + [{"url": "https://www.youtube.com/watch?v=1"}]}))
    monkeypatch.setattr(collect, "ROOT", tmp_path)
    monkeypatch.setattr(collect, "save_state", lambda s: None)
    monkeypatch.setattr(collect, "log", lambda *a, **k: None)
    monkeypatch.setattr(collect, "polite_sleep", lambda *a: None)
    monkeypatch.setattr(collect, "check_disk", lambda s: {"level": "ok"})
    monkeypatch.setattr(polite_fetcher, "fetch_policy", lambda: {**POLICY, "deny": ["youtube.com"]})
    monkeypatch.setattr(polite_fetcher, "already_fetched", lambda: {})
    monkeypatch.setattr(polite_fetcher, "fetch_one", lambda *a, **k: pytest.fail("discovery must not download"))
    monkeypatch.setattr(collect.web_search, "Search", lambda: None)
    monkeypatch.setattr(collect.web_search, "search_settings", lambda: {"query_delay_s": [0, 0]})
    monkeypatch.setattr(collect.web_search, "run", lambda q, pages, tag, s: {
        "result_count": 6, "cache_files": ["data/search_cache/q.json"], "unresponsive_engines": [], "providers": ["searxng"]})
    state = {"queries": {}}
    step = {"phases": {}, "queries_done": 0, "queries_failed": 0, "queries_no_results": 0, "urls_found": 0,
            "filed_new": 0, "stop_reason": None}
    collect.discover([{"query": "q", "year_tag": "2017", "phase": "p", "pages": 1}], state, step,
                     {"fetch_limit_per_query": 3}, "run1", type("A", (), {"fetch_limit": None})())
    assert state["queries"]["q"]["status"] == "done" and step["filed_new"] == 5   # youtube not filed
    assert dk.stats()["by_status"] == {"pending": 3, "parked": 2}


def test_worker_waits_for_discovery_then_stops_when_it_is_done(dk, worker, tmp_path, monkeypatch):
    marker = tmp_path / ".run.discovering"
    marker.touch()
    naps = []

    def sleep(s):  # Discovery files one more item, then finishes
        naps.append(s)
        if len(naps) == 1:
            dk.file([item("https://a.in/late.pdf")], KNOWN)
        else:
            marker.unlink(missing_ok=True)
    monkeypatch.setattr(fetch_worker.time, "sleep", sleep)
    out = fetch_worker.run(max_files=10, follow=marker, follow_pid=__import__("os").getpid())
    assert worker == ["https://a.in/late.pdf"] and out["stop_reason"] == "docket_exhausted"


def test_worker_does_not_wait_for_a_dead_discovery(dk, worker, tmp_path):
    marker = tmp_path / ".run.discovering"
    marker.touch()  # left behind by a crashed parent
    assert fetch_worker.run(max_files=10, follow=marker, follow_pid=2 ** 22 + 12345)["stop_reason"] == "docket_exhausted"


def test_process_worker_keeps_going_while_downloads_arrive(dk, monkeypatch):
    import process_worker as pw
    waiting = iter([5, 9, 0])          # 4 new downloads landed during the first batch
    alive = iter([True, True, False])
    batches = []
    monkeypatch.setattr(pw.docket, "open_docket", lambda: dk)
    monkeypatch.setattr(dk, "fetched_count", lambda: next(waiting))
    monkeypatch.setattr(pw, "alive", lambda pid: next(alive))
    monkeypatch.setattr(pw, "log", lambda *a, **k: None)
    monkeypatch.setattr(pw.time, "sleep", lambda s: None)
    monkeypatch.setattr(pw, "process_once", lambda d: batches.append(1) or {
        "parsed_files": 5, "parsed_rows": 1, "parse_errors": 0, "records_after": 1,
        "docket": {"parsed": 5, "parse_error": 0, "waiting": 9}})
    out = pw.run(batch=5, follow=123)
    assert len(batches) == 2 and out["stop_reason"] == "idle"
