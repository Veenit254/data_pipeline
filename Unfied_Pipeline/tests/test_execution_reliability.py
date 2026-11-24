"""Regression tests: worker crashes, exit-code propagation, poison items, Retry-After."""
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

import docket
import fetch_worker
import polite_fetcher as pf
import process_worker

REPO = Path(__file__).resolve().parent.parent


# ---------- Retry-After (S7) ------------------------------------------------------------------------------
def test_retry_after_parsing():
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    assert pf.retry_after("30") == 30
    assert 80 <= pf.retry_after(soon) <= 91
    assert pf.retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0      # in the past
    assert pf.retry_after("soon-ish") is None and pf.retry_after("") is None and pf.retry_after(None) is None
    assert pf.retry_after("-5") is None


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}


class Session:
    def __init__(self, seq):
        self.seq, self.calls = list(seq), 0

    def get(self, url, **kw):
        self.calls += 1
        x = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        if isinstance(x, Exception):
            raise x
        return x


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(pf.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(pf, "log", lambda *a, **k: None)
    monkeypatch.setattr(pf, "append_jsonl", lambda *a, **k: None)
    monkeypatch.setattr(pf, "QUARANTINE", tmp_path / "q")
    monkeypatch.setattr(pf, "raw_dir_for_tag", lambda t: tmp_path / "raw")
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    return slept


def test_http_date_retry_after_does_not_crash(quiet):
    s = Session([Resp(503, b"", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})])
    e = pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")
    assert e["quarantined"] and e["http_status"] == 503 and s.calls == 4


def test_no_sleep_after_the_last_attempt_and_retry_after_is_capped(quiet):
    pf.fetch_one(Session([Resp(500, b"err")]), "https://www.nseindia.com/x.pdf", "2017")
    assert quiet == [4, 8, 16]
    quiet.clear()
    pf.fetch_one(Session([Resp(429, b"", {"Retry-After": "3600"})]), "https://www.nseindia.com/x.pdf", "2017")
    assert quiet == [120, 120, 120]


def test_last_network_error_is_kept_after_an_earlier_5xx(quiet):
    s = Session([Resp(502, b"bad gateway")] + [requests.ConnectionError("connection reset")] * 3)
    e = pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")
    assert e["quarantined"] and "connection reset" in e["error"] and e["http_status"] is None
    assert e["last_http_status"] == 502


# ---------- fetch worker: one bad item never kills the worker (S5, S8) ---------------------------------------
@pytest.fixture
def dk(tmp_path, monkeypatch):
    monkeypatch.setattr(docket, "DB", tmp_path / "docket.sqlite")
    monkeypatch.setattr(docket, "STATS", tmp_path / "docket_stats.json")
    monkeypatch.setattr(docket, "import_existing", lambda d: None)
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    return docket.Docket()


POLICY = {"known": ["nseindia.com", "bseindia.com"], "deny": [], "open_web": True,
          "limits": {"default": {"max_per_run": 50, "delay_s": [0, 0]}}}


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


def ok_entry(url):
    return {"url": url, "http_status": 200, "quarantined": False, "local_path": "data/raw/x.pdf"}


def test_unexpected_exception_is_recorded_and_the_worker_goes_on(dk, monkeypatch):
    dk.file([{"url": "https://www.nseindia.com/bad.pdf", "kind": "search", "year_tag": "2017"},
             {"url": "https://www.bseindia.com/good.pdf", "kind": "search", "year_tag": "2017"}], POLICY["known"])

    def fetch(session, url, tag, hf=None):
        if "bad" in url:
            raise ValueError("invalid literal for int() with base 10: 'Wed, 21 Oct 2026'")
        return ok_entry(url)
    worker_env(monkeypatch, dk, fetch)
    out = fetch_worker.run(max_files=10)
    assert out["fetched_ok"] == 1 and out["worker_errors"] == 1
    bad = dk.db.execute("SELECT status, attempts, last_error FROM items WHERE url LIKE '%bad%'").fetchone()
    assert bad["status"] == "failed" and bad["attempts"] == 1 and "ValueError" in bad["last_error"]


def test_repeated_unexpected_errors_stop_the_worker_with_a_reason(dk, monkeypatch):
    dk.file([{"url": f"https://www.nseindia.com/{i}.pdf", "kind": "search", "year_tag": "2017"} for i in range(6)],
            POLICY["known"])
    worker_env(monkeypatch, dk, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    out = fetch_worker.run(max_files=10)
    assert out["stop_reason"] == "worker_errors" and out["worker_errors"] == fetch_worker.MAX_CONSECUTIVE_ERRORS


def test_disk_full_stops_the_run_as_low_disk(dk, monkeypatch):
    dk.file([{"url": "https://www.nseindia.com/a.pdf", "kind": "search", "year_tag": "2017"}], POLICY["known"])
    worker_env(monkeypatch, dk, lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    out = fetch_worker.run(max_files=10)
    assert out["stop_reason"] == "low_disk_space"
    assert dk.db.execute("SELECT status FROM items").fetchone()["status"] == "failed"   # not stuck 'fetching'


# ---------- poison items: a crash while fetching counts as an attempt (S6) ---------------------------------
def test_expired_lease_counts_as_a_failed_attempt_and_ends_dead(dk):
    u = "https://www.nseindia.com/poison.pdf"
    dk.file([{"url": u, "kind": "search", "year_tag": "2017"}], POLICY["known"])
    for n in range(docket.MAX_LEASE_CRASHES):
        assert dk.lease(u)
        dk.db.execute("UPDATE items SET lease_until=?, next_attempt_at=0 WHERE url=?", (time.time() - 1, u))
        assert u in [c["url"] for c in dk.candidates()]
    assert not dk.lease(u)          # the worker died on it MAX_ATTEMPTS-1 times: give up instead of crashing again
    row = dk.db.execute("SELECT status, attempts, last_error FROM items WHERE url=?", (u,)).fetchone()
    assert row["status"] == "dead" and "worker stopped" in row["last_error"]


def test_done_fetch_on_unknown_url_is_reported(dk):
    assert dk.done_fetch("https://unknown.example/x.pdf", {"http_status": 200, "local_path": "x"}) == "unknown"


def test_graceful_stop_releases_the_lease_without_an_attempt(dk):
    u = "https://www.nseindia.com/a.pdf"
    dk.file([{"url": u, "kind": "search", "year_tag": "2017"}], POLICY["known"])
    assert dk.lease(u)
    dk.release(u)
    row = dk.db.execute("SELECT status, attempts, lease_until FROM items").fetchone()
    assert (row["status"], row["attempts"], row["lease_until"]) == ("pending", 0, 0)


# ---------- process worker: any exception is an error with a non-zero exit -------------------------------
def test_process_worker_reports_any_exception(dk, monkeypatch):
    monkeypatch.setattr(process_worker.docket, "open_docket", lambda: dk)
    monkeypatch.setattr(process_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(dk, "fetched_count", lambda: 5)
    monkeypatch.setattr(process_worker, "process_once", lambda d: (_ for _ in ()).throw(
        __import__("sqlite3").OperationalError("database is locked")))
    out = process_worker.run(batch=1, follow=None)
    assert out["stop_reason"].startswith("error: OperationalError")


# ---------- collect.py: a crashed worker makes the run fail (S5) -------------------------------------------
def sandbox(tmp_path: Path) -> Path:
    for d in ("scripts", "config"):
        shutil.copytree(REPO / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("logs/runs", "logs/jobs", "data/metadata", "data/processed/parsed", "data/raw", "data/search_cache"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "fetch_manifest.jsonl").write_text("")
    (tmp_path / ".venv").symlink_to(REPO / ".venv")
    return tmp_path


@pytest.mark.parametrize("crash", ["raise ValueError('Retry-After: Wed, 21 Oct 2026')", "import os; os._exit(9)"])
def test_collect_fails_when_a_worker_crashes(tmp_path, crash):
    sb = sandbox(tmp_path)
    (sb / "scripts" / "fetch_worker.py").write_text(crash + "\n")
    r = subprocess.run([sys.executable, "scripts/collect.py", "--stage", "fetch", "--max-files", "5"], cwd=sb,
                       capture_output=True, text=True, timeout=120)
    out = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1])
    assert r.returncode == 1, r.stdout[-500:] + r.stderr[-500:]
    assert out["status"] == "error" and out["stop_reason"].startswith("fetch_worker_failed"), out
    summary = json.loads((sb / out["summary_file"]).read_text())
    assert summary["status"] == "error" and summary["workers"]["fetch_worker"]["exit_code"] != 0


def test_collect_still_completes_when_workers_are_fine(tmp_path):
    sb = sandbox(tmp_path)
    r = subprocess.run([sys.executable, "scripts/collect.py", "--stage", "fetch", "--max-files", "5"], cwd=sb,
                       capture_output=True, text=True, timeout=120)
    out = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1])
    assert r.returncode == 0 and out["status"] == "completed" and out["stop_reason"] == "docket_exhausted", out
