import json
from collections import Counter
from datetime import date
from types import SimpleNamespace

import pytest

import collect
import docket
import fetch_worker
import historical
import polite_fetcher
import process_worker
import run_limits
import tool_guard

KNOWN = ["nseindia.com"]
POLICY = {"open_web": True, "known": KNOWN, "deny": [],
          "limits": {"default": {"max_per_run": 50, "delay_s": [0, 0]}}}


# --- run limits ----------------------------------------------------------------------------------
@pytest.mark.parametrize("v,want", [(0, None), (None, None), ("all", None), ("ALL", None), ("0", None),
                                    ("", None), (5, 5), ("12", 12)])
def test_parse_cap(v, want):
    assert run_limits.parse_cap(v) == want


def test_parse_cap_rejects_nonsense():
    for v in ("lots", -1):
        with pytest.raises(ValueError):
            run_limits.parse_cap(v)


def args(**kw):
    return SimpleNamespace(**{"max_files": None, "max_companies": None, "max_docs_per_company": None,
                              "unlimited": False, "capped": False, **kw})


@pytest.fixture
def cfg(monkeypatch):
    holder = {}
    monkeypatch.setattr(run_limits, "from_config", lambda: {**run_limits.DEFAULTS, **holder})
    return holder


def test_config_caps_and_flag_overrides(cfg):
    cfg.update(mode="capped", max_files=60, max_companies=0)
    assert run_limits.resolve(args(), False) == {"mode": "capped", "max_files": 60, "max_companies": None,
                                                 "max_docs_per_company": 5}
    assert run_limits.resolve(args(max_files="all", max_companies="3"), False)["max_files"] is None
    assert run_limits.resolve(args(unlimited=True), False) == {"mode": "unlimited", "max_files": None,
                                                              "max_companies": None, "max_docs_per_company": None}


def test_unlimited_config_with_one_flag_cap(cfg):
    cfg.update(mode="unlimited")
    lim = run_limits.resolve(args(max_files="10"), False)
    assert lim["mode"] == "capped" and lim["max_files"] == 10


def test_ask_mode_asks_only_on_a_terminal(cfg):
    cfg.update(mode="ask")
    asked = []
    assert run_limits.resolve(args(), True, ask=lambda p: asked.append(p) or "2")["mode"] == "unlimited"
    assert run_limits.resolve(args(), True, ask=lambda p: "")["mode"] == "capped"   # Enter = recommended
    assert run_limits.resolve(args(), False, ask=lambda p: pytest.fail("unattended run asked"))["mode"] == "capped"
    assert run_limits.resolve(args(max_files="5"), True, ask=lambda p: pytest.fail("flag given"))["max_files"] == 5
    assert len(asked) == 1


def test_stop_file(tmp_path):
    s = run_limits.StopFile(tmp_path / ".stop")
    assert s.reason() is None
    s.stop("max_files_reached")
    s.stop("max_companies_reached")  # first reason wins
    assert s.reason() == "max_files_reached"
    assert run_limits.StopFile(None).reason() is None


# --- docket + fetch worker -----------------------------------------------------------------------
@pytest.fixture
def dk(tmp_path, monkeypatch):
    monkeypatch.setattr(docket, "DB", tmp_path / "docket.sqlite")
    monkeypatch.setattr(docket, "STATS", tmp_path / "docket_stats.json")
    monkeypatch.setattr(docket, "import_existing", lambda d: None)
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    monkeypatch.setattr(historical, "window", lambda today=None: (2006, 2011))
    monkeypatch.setattr(historical, "companies", lambda win=None, master_rows=None: [])
    return docket.Docket()


@pytest.fixture
def worker(dk, monkeypatch):
    fetched = []

    def fake_fetch(session, url, tag, harvested_from=None):
        fetched.append(url)
        gone = "gone" in url
        return {"url": url, "http_status": 404 if gone else 200, "quarantined": gone,
                "local_path": None if gone else f"data/raw/x/{len(fetched)}.pdf", "year_tag": tag}
    monkeypatch.setattr(polite_fetcher, "fetch_one", fake_fetch)
    monkeypatch.setattr(polite_fetcher, "fetch_policy", lambda: POLICY)
    monkeypatch.setattr(polite_fetcher, "already_fetched", lambda: {})
    monkeypatch.setattr(polite_fetcher, "RUN_COUNTS", Counter())
    monkeypatch.setattr(fetch_worker, "plan", lambda: {})
    monkeypatch.setattr(fetch_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(fetch_worker, "check_disk", lambda s: {"level": "ok"})
    monkeypatch.setattr(fetch_worker.time, "sleep", lambda s: None)
    return fetched


def item(url, **kw):
    return {"url": url, "kind": "search", "year_tag": "2008", **kw}


def test_max_files_reached_stops_every_stage(dk, worker, tmp_path):
    dk.file([item(f"https://a.in/{i}.pdf") for i in range(5)], KNOWN)
    stop = run_limits.StopFile(tmp_path / ".stop")
    out = fetch_worker.run(max_files=2, stop=stop)
    assert out["stop_reason"] == "max_files_reached" and len(worker) == 2 and stop.reason() == "max_files_reached"


def test_another_stages_stop_is_honoured(dk, worker, tmp_path):
    dk.file([item("https://a.in/1.pdf")], KNOWN)
    stop = run_limits.StopFile(tmp_path / ".stop")
    stop.stop("max_companies_reached")
    assert fetch_worker.run(max_files=None, stop=stop)["stop_reason"] == "max_companies_reached" and worker == []


def test_unlimited_run_ends_when_the_docket_is_exhausted(dk, worker):
    dk.file([item(f"https://a.in/{i}.pdf") for i in range(7)], KNOWN)
    out = fetch_worker.run(max_files=None)
    assert len(worker) == 7 and out["stop_reason"] == "docket_exhausted"


def test_docs_per_company_cap_parks_the_rest(dk, worker):
    dk.file([item(f"https://a.in/{i}.pdf", company="abc", company_name="ABC Ltd") for i in range(4)]
            + [item("https://a.in/other.pdf")], KNOWN)
    out = fetch_worker.run(max_files=None, max_docs_per_company=2)
    assert out["parked_company_cap"] == 2 and dk.company_docs("abc") == 2 and len(worker) == 3
    assert dk.stats()["by_status"]["parked"] == 2


def test_gone_official_document_from_the_window_gets_its_archive_copy(dk, worker, monkeypatch):
    monkeypatch.setattr(historical, "archive_copy",
                        lambda s, url, year: f"https://web.archive.org/web/20080701000000id_/{url}")
    dk.file([item("https://www.nseindia.com/gone.pdf", company="abc")], KNOWN)
    out = fetch_worker.run(max_files=None)
    assert out["archive_filed"] == 1
    assert worker[-1].startswith("https://web.archive.org/web/20080701000000id_/")   # and it was fetched next
    row = dk.db.execute("SELECT kind, company FROM items WHERE url LIKE 'https://web.archive.org/%'").fetchone()
    assert tuple(row) == ("archive", "abc")


def test_historical_records_rank_ahead_of_news(dk):
    focus = (2006, 2011)
    archive = docket.priority("https://web.archive.org/web/2008id_/https://nseindia.com/a.pdf", "archive", "2008",
                              True, KNOWN, 3, focus)
    official = docket.priority("https://www.nseindia.com/circular.pdf", "search", "2008", True, KNOWN, 3, focus)
    official_recent = docket.priority("https://www.nseindia.com/circular.pdf", "search", "2024", True, KNOWN, 3, focus)
    news = docket.priority("https://economictimes.indiatimes.com/x.pdf", "search", "2008", True,
                           KNOWN + ["economictimes.indiatimes.com"], 1, focus)
    assert archive >= official > official_recent > news


def test_process_worker_stops_the_run_at_max_companies(dk, monkeypatch, tmp_path):
    waiting = iter([5, 0])
    monkeypatch.setattr(process_worker.docket, "open_docket", lambda: dk)
    monkeypatch.setattr(dk, "fetched_count", lambda: next(waiting))
    monkeypatch.setattr(process_worker, "alive", lambda pid: False)
    monkeypatch.setattr(process_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(process_worker, "process_once", lambda d: {
        "parsed_files": 5, "parsed_rows": 9, "parse_errors": 0, "records_after": 110,
        "docket": {"parsed": 5, "parse_error": 0, "waiting": 0}})
    stop = run_limits.StopFile(tmp_path / ".stop")
    out = process_worker.run(5, None, max_companies=10, records_before=100, stop=stop)
    assert stop.reason() == "max_companies_reached" and out["max_companies_reached"]


# --- historical focus ----------------------------------------------------------------------------
MASTER = [
    {"company_name": "Old Proven Ltd", "calendar_year": "2008", "isin": "", "nse_symbol": "OLDP",
     "delisting_type": "COMPULSORY", "source_url": "https://www.nseindia.com/delisted.xlsx"},
    {"company_name": "Old Unproven Ltd", "calendar_year": "2007", "isin": "INE000A01019", "nse_symbol": "",
     "delisting_type": "VOLUNTARY_NO_EXIT", "source_url": "https://some-blog.example/post"},
    {"company_name": "Recent Ltd", "calendar_year": "2022", "isin": "", "nse_symbol": "",
     "delisting_type": "COMPULSORY", "source_url": "https://www.nseindia.com/x"},
]


def test_window_is_15_to_20_years_ago(monkeypatch):
    monkeypatch.setattr(historical, "CONFIG", historical.CONFIG)  # real config: years_ago [15, 20]
    assert historical.window(date(2026, 11, 23)) == (2006, 2011)


def test_company_queries_prove_first_then_support():
    comps = {c["name"]: c for c in historical.companies((2006, 2011), MASTER)}
    assert set(comps) == {"Old Proven Ltd", "Old Unproven Ltd"}           # Recent Ltd is outside the window
    assert comps["Old Proven Ltd"]["proven"] and not comps["Old Unproven Ltd"]["proven"]
    items = historical.items({}, (2006, 2011), MASTER)
    phases = [it["phase"] for it in items]
    assert phases == sorted(phases, key=["historical_proof", "historical_company_proof",
                                         "historical_company_support"].index)  # proof before support
    unproven = [it["query"] for it in items if it.get("company") == historical.company_key("Old Unproven Ltd")]
    assert '"Old Unproven" delisted 2007 site:nseindia.com' in unproven and "INE000A01019 delisting" in unproven
    support = [it for it in items if it["phase"] == "historical_company_support"]
    assert {it["company_name"] for it in support} == {"Old Proven Ltd"}
    assert any("compulsory delisting order" in it["query"] for it in support)
    assert {it["year_tag"] for it in items if it["phase"] == "historical_proof"} == {str(y) for y in range(2006, 2012)}


def test_archive_copy_uses_the_raw_snapshot():
    class S:
        def get(self, url, params, timeout):
            assert params["timestamp"] == "20080701"
            return SimpleNamespace(json=lambda: {"archived_snapshots": {"closest": {
                "available": True, "status": "200", "url": "http://web.archive.org/web/20080512/http://nseindia.com/a.pdf"}}})
    assert historical.archive_copy(S(), "http://nseindia.com/a.pdf", "2008") == \
        "https://web.archive.org/web/20080512id_/http://nseindia.com/a.pdf"


def test_discovery_files_official_first_with_the_company_and_honours_stop(dk, tmp_path, monkeypatch):
    cache = tmp_path / "data" / "search_cache" / "q.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"query": "q", "provider": "searxng", "results": [
        {"url": "https://economictimes.indiatimes.com/news.html", "title": "delisting"},
        {"url": "https://www.nseindia.com/order.pdf", "title": "delisting order"}]}))
    monkeypatch.setattr(collect, "ROOT", tmp_path)
    for name, fn in [("save_state", lambda s: None), ("log", lambda *a, **k: None), ("polite_sleep", lambda *a: None),
                     ("check_disk", lambda s: {"level": "ok"})]:
        monkeypatch.setattr(collect, name, fn)
    monkeypatch.setattr(polite_fetcher, "fetch_policy", lambda: POLICY)
    monkeypatch.setattr(polite_fetcher, "already_fetched", lambda: {})
    monkeypatch.setattr(collect.web_search, "Search", lambda: None)
    monkeypatch.setattr(collect.web_search, "search_settings", lambda: {"query_delay_s": [0, 0]})
    monkeypatch.setattr(collect.web_search, "run", lambda q, p, t, s: {
        "result_count": 2, "cache_files": ["data/search_cache/q.json"], "unresponsive_engines": []})
    step = {"phases": {}, "queries_done": 0, "queries_failed": 0, "queries_no_results": 0, "urls_found": 0,
            "filed_new": 0, "stop_reason": None}
    todo = [{"query": "q", "year_tag": "2008", "phase": "historical_company_support", "pages": 1,
             "company": "abc", "company_name": "ABC Ltd"},
            {"query": "q2", "year_tag": "2008", "phase": "historical_proof", "pages": 1}]
    stop = run_limits.StopFile(tmp_path / ".stop")
    real_file = dk.file

    def file_then_stop(items, known, fetched=None):  # a cap is reached right after the first query
        stop.stop("max_files_reached")
        return real_file(items, known, fetched)
    monkeypatch.setattr(dk, "file", file_then_stop)
    monkeypatch.setattr(collect.docket, "open_docket", lambda: dk)
    collect.discover(todo, {"queries": {}}, step, {"fetch_limit_per_query": 1}, "r", SimpleNamespace(fetch_limit=None), stop)
    rows = dk.db.execute("SELECT url, status, company FROM items ORDER BY rank").fetchall()
    assert [tuple(r) for r in rows] == [("https://www.nseindia.com/order.pdf", "pending", "abc"),
                                        ("https://economictimes.indiatimes.com/news.html", "parked", "abc")]
    assert step["stop_reason"] == "max_files_reached" and step["queries_done"] == 1


def test_agent_may_not_change_limits_or_focus():
    plan = f"{tool_guard.ROOT}/config/query_plan.json"
    for txt in ['"max_files": 100000', '"mode": "unlimited"', '"years_ago": [1, 2]']:
        assert tool_guard.check("editor", {"path": plan, "new_text": txt}), txt
