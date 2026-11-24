"""A network outage with a single queued domain must not use up item attempts; a genuine URL failure
(network up, site not answering) still counts. No real network access: the probe is replaced or pointed at
a local socket."""
import socket
from collections import Counter

import pytest

import docket
import fetch_worker
import polite_fetcher as pf


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def dk(tmp_path, monkeypatch):
    monkeypatch.setattr(docket, "DB", tmp_path / "docket.sqlite")
    monkeypatch.setattr(docket, "STATS", tmp_path / "docket_stats.json")
    monkeypatch.setattr(docket, "import_existing", lambda d: None)
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    return docket.Docket()


POLICY = {"known": ["nseindia.com"], "deny": [], "open_web": True,
          "limits": {"default": {"max_per_run": 1000, "delay_s": [0, 0]}}}


def env(monkeypatch, dk, fetch, probe=("127.0.0.1:9",)):
    monkeypatch.setattr(docket, "open_docket", lambda: dk)
    monkeypatch.setattr(pf, "fetch_one", fetch)
    monkeypatch.setattr(pf, "fetch_policy", lambda: POLICY)
    monkeypatch.setattr(pf, "RUN_COUNTS", Counter())
    monkeypatch.setattr(fetch_worker, "plan", lambda: {"block_domain_after_failures": 3,
                                                       "network_probe_hosts": list(probe)})
    monkeypatch.setattr(fetch_worker, "log", lambda *a, **k: None)
    monkeypatch.setattr(fetch_worker, "check_disk", lambda s: {"level": "ok"})
    monkeypatch.setattr(fetch_worker.time, "sleep", lambda s: None)


def no_response(s, u, t, h=None):
    return {"url": u, "http_status": None, "quarantined": True, "error": "ConnectionError: Failed to resolve host"}


def single_domain(dk, n=3):
    dk.file([{"url": f"https://www.nseindia.com/{i}.pdf", "kind": "search", "year_tag": "2017"} for i in range(n)],
            ["nseindia.com"])


def attempts(dk):
    return [(r["status"], r["attempts"]) for r in dk.db.execute("SELECT status, attempts FROM items ORDER BY url")]


def test_single_domain_outage_costs_no_attempts(dk, monkeypatch):
    single_domain(dk)
    env(monkeypatch, dk, no_response)
    monkeypatch.setattr(fetch_worker, "network_up", lambda hosts: False)
    out = fetch_worker.run(max_files=10)
    assert out["stop_reason"] == "network_unavailable"
    assert out["outage_released"] == 1 and out["failed"] == 0
    assert attempts(dk) == [("pending", 0)] * 3
    assert not dk.blocked()


def test_single_domain_genuine_failure_still_counts(dk, monkeypatch):
    single_domain(dk)
    env(monkeypatch, dk, no_response)
    monkeypatch.setattr(fetch_worker, "network_up", lambda hosts: True)
    out = fetch_worker.run(max_files=10)
    assert out["stop_reason"] in ("sources_exhausted", "docket_exhausted")
    assert out["failed"] == 3 and out["outage_released"] == 0
    assert attempts(dk) == [("failed", 1)] * 3          # retried later with backoff, as before


def test_repeated_outage_runs_never_kill_an_item(dk, monkeypatch):
    single_domain(dk, 1)
    env(monkeypatch, dk, no_response)
    monkeypatch.setattr(fetch_worker, "network_up", lambda hosts: False)
    for _ in range(20):                                     # 20 runs during a long outage
        fetch_worker.run(max_files=10)
    assert attempts(dk) == [("pending", 0)]


def test_http_errors_never_trigger_the_probe(dk, monkeypatch):
    single_domain(dk, 1)
    env(monkeypatch, dk, lambda s, u, t, h=None: {"url": u, "http_status": 404, "quarantined": True})
    calls = []
    monkeypatch.setattr(fetch_worker, "network_up", lambda hosts: calls.append(1) or False)
    out = fetch_worker.run(max_files=10)
    assert calls == [] and out["dead"] == 1


def test_probe_reports_down_when_nothing_listens():
    # 127.0.0.1:9 (discard) is closed on a normal machine: connection refused, no external traffic
    assert fetch_worker.network_up([("127.0.0.1", 9)], {}) is False


def test_probe_reports_up_when_a_host_accepts():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        assert fetch_worker.network_up([("127.0.0.1", 9), ("127.0.0.1", srv.getsockname()[1])], {}) is True
    finally:
        srv.close()


def test_no_probe_hosts_means_unknown_and_the_old_rule_applies():
    assert fetch_worker.network_up([], {}) is None
    assert fetch_worker.probe_hosts({}) == []
    assert fetch_worker.probe_hosts({"network_probe_hosts": ["1.1.1.1:443", "bad:port", "9.9.9.9"]}) == \
        [("1.1.1.1", 443), ("9.9.9.9", 443)]


def test_shipped_config_enables_the_probe():
    import json
    from common import CONFIG
    plan = json.loads((CONFIG / "query_plan.json").read_text())
    assert fetch_worker.probe_hosts(plan)


def test_probe_goes_to_the_proxy_when_one_is_configured(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:18080")
    assert fetch_worker.probe_hosts({"network_probe_hosts": ["1.1.1.1:443"]}) == [("127.0.0.1", 18080)]
    assert fetch_worker.probe_hosts({}) == []                    # probe off stays off
