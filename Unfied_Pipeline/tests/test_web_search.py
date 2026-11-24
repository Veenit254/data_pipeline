import json
import time

import pytest

import credentials
import search_providers
import web_search as ws
from search_providers.base import Answer, SearchProvider, result


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "ENGINE_STATE", tmp_path / "engine_state.json")
    monkeypatch.setattr(ws, "log", lambda *a, **k: None)
    monkeypatch.setattr(ws, "polite_sleep", lambda *a, **k: None)
    monkeypatch.setattr(credentials, "key_files", lambda: [])
    for k in ("SERPER_API_KEY", "BRAVE_API_KEY", "FAKE_KEY"):
        monkeypatch.delenv(k, raising=False)


def settings(providers, **ps):
    return {**ws.SEARCH_DEFAULTS, "providers": providers, "provider_settings": ps,
            "cooldown_min": dict(ws.SEARCH_DEFAULTS["cooldown_min"]), "official_domains": ["nseindia.com"]}


@pytest.fixture
def rot():
    return ws.Rotation("searxng", ["a", "b", "c", "d"], settings([]), {})


# --- rotation ------------------------------------------------------------------------------------
def test_picks_longest_rested_units(rot):
    rot.state["a"]["last_used"] = 5
    rot.state["b"]["last_used"] = 1
    assert rot.pick(2) == ["c", "d"]
    rot.record(["c", "d"], [{"url": "https://www.nseindia.com/x", "engines": ["c"]}], [])
    assert rot.pick(2) == ["b", "a"]


def test_failing_unit_is_benched_by_reason(rot):
    rot.record(["a", "b"], [], [("a", "Suspended: too many requests"), ("b", "CAPTCHA")])
    assert rot.state["a"]["benched_until"] == pytest.approx(time.time() + 3600, abs=5)
    assert rot.state["b"]["benched_until"] == pytest.approx(time.time() + 6 * 3600, abs=5)
    assert set(rot.usable()) == {"c", "d"}


def test_junk_or_empty_answers_bench_a_unit(rot):
    junk = [{"url": "https://steamcommunity.com/x", "engines": ["a"]}]
    for _ in range(3):
        rot.record(["a"], junk, [])
    assert "a" not in rot.usable() and "irrelevant" in rot.state["a"]["reason"]


def test_relevant_results_from_new_sites_do_not_bench(rot):
    news = [{"url": "https://www.capitalmarket.com/x", "title": "Exchange to delist 200 firms", "engines": ["a"]}]
    for _ in range(4):
        rot.record(["a"], news, [])
    assert "a" in rot.usable()


def test_longest_matching_cooldown_wins(rot):
    rot.record(["a"], [], [("a", "Suspended: access denied")])
    assert rot.state["a"]["benched_until"] == pytest.approx(time.time() + 12 * 3600, abs=5)


def test_old_flat_state_file_becomes_searxng_state(tmp_path):
    ws.ENGINE_STATE.write_text(json.dumps({"bing": {**ws.NEW_UNIT, "uses": 7}}))
    assert ws.load_state() == {"searxng": {"bing": {**ws.NEW_UNIT, "uses": 7}}}


# --- providers and fallback ----------------------------------------------------------------------
def fake(name, answers, keys=(), units=None, per_query=1):
    """A provider class that replays `answers` (Answer, or an exception to raise)."""
    calls = []

    class Fake(SearchProvider):
        key_names = tuple(keys)
        units_per_query = per_query

        def units(self):
            return units or [self.name]

        def search(self, session, query, page, us):
            calls.append(us)
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
    Fake.name = name
    Fake.calls = calls
    return Fake


def hit(name, url="https://www.nseindia.com/a"):
    return Answer([result(url, "Delisting order", "", name)])


def test_first_provider_answers():
    a, b = fake("free", [hit("free")]), fake("paid", [hit("paid")])
    s = ws.Search(settings(["free", "paid"]), {"free": a, "paid": b})
    _, prov, _ = s.page(None, "q", 1)
    assert prov == "free" and b.calls == []


def test_falls_back_when_first_provider_is_rate_limited():
    a = fake("free", [Answer(failures=[("free", "too many requests")])])
    b = fake("paid", [hit("paid")])
    s = ws.Search(settings(["free", "paid"]), {"free": a, "paid": b})
    _, prov, _ = s.page(None, "q", 1)
    assert prov == "paid"
    assert s.rot["free"].state["free"]["benched_until"] > time.time()


def test_clean_empty_answer_does_not_spend_paid_credits():
    a = fake("free", [Answer(), Answer()], units=["e1", "e2"], per_query=1)
    b = fake("paid", [hit("paid")])
    s = ws.Search(settings(["free", "paid"]), {"free": a, "paid": b})
    ans, prov, _ = s.page(None, "q", 1)
    assert prov == "free" and ans.results == [] and b.calls == []
    assert len(a.calls) == 2  # tried the other engine once, like before


def test_provider_without_key_is_skipped(monkeypatch):
    paid = fake("paid", [hit("paid")], keys=["FAKE_KEY"])
    s = ws.Search(settings(["paid"]), {"paid": paid})
    with pytest.raises(RuntimeError, match="no API key"):
        s.page(None, "q", 1)
    monkeypatch.setenv("FAKE_KEY", "k-123")
    assert ws.Search(settings(["paid"]), {"paid": paid}).page(None, "q", 1)[1] == "paid"


def test_daily_limit_stops_a_provider_until_midnight():
    paid = fake("paid", [hit("paid"), hit("paid")])
    s = ws.Search(settings(["paid"], paid={"daily_limit": 1}), {"paid": paid})
    s.page(None, "q", 1)
    with pytest.raises(ws.SearchExhausted) as exc:
        s.page(None, "q", 2)
    assert exc.value.until > time.time() and len(paid.calls) == 1


def test_provider_error_moves_on_without_benching():
    a = fake("free", [ConnectionError("refused")])
    b = fake("paid", [hit("paid")])
    s = ws.Search(settings(["free", "paid"]), {"free": a, "paid": b})
    assert s.page(None, "q", 1)[1] == "paid"
    assert s.rot["free"].state["free"]["benched_until"] == 0 and "free" in s.down


def test_all_benched_raises_with_earliest_return():
    s = ws.Search(settings(["free"]), {"free": fake("free", [], units=["a", "b"])})
    s.rot["free"].bench("a", "too many requests")
    s.rot["free"].bench("b", "captcha")
    with pytest.raises(ws.SearchExhausted) as exc:
        s.page(None, "q", 1)
    assert exc.value.until == pytest.approx(time.time() + 3600, abs=5)


def test_unknown_provider_is_reported_not_fatal():
    s = ws.Search(settings(["nope", "free"]), {"free": fake("free", [])})
    assert s.unknown == ["nope"] and [p.name for p in s.providers] == ["free"]


def test_builtin_providers_register_and_duplicates_are_refused():
    reg = search_providers.load_all()
    assert {"searxng", "serper", "brave"} <= set(reg)
    clash = type("Clash", (SearchProvider,), {"name": "serper"})
    with pytest.raises(ValueError):
        search_providers.register(clash)


# --- API adapters --------------------------------------------------------------------------------
class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, resp):
        self.resp, self.sent = resp, {}

    def post(self, url, **kw):
        self.sent = {"url": url, **kw}
        return self.resp

    get = post


def test_serper_maps_results_and_sends_key_as_header(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "sk-secret")
    reg = search_providers.load_all()
    sess = Session(Resp(200, {"organic": [{"link": "https://nseindia.com/x", "title": "T", "snippet": "S"}]}))
    ans = reg["serper"]({}).search(sess, "q", 2, ["serper"])
    assert ans.results[0]["url"] == "https://nseindia.com/x" and ans.results[0]["engines"] == ["serper"]
    assert sess.sent["headers"]["X-API-KEY"] == "sk-secret" and sess.sent["json"]["page"] == 2
    assert "sk-secret" not in json.dumps({k: v for k, v in sess.sent.items() if k != "headers"})


def test_brave_maps_results_and_errors(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "bk")
    brave = search_providers.load_all()["brave"]({})
    ok = brave.search(Session(Resp(200, {"web": {"results": [{"url": "https://a.in", "title": "t",
                                                                "description": "d"}]}})), "q", 3, ["brave"])
    assert ok.results[0]["content"] == "d"
    limited = brave.search(Session(Resp(429, {"error": "rate"})), "q", 1, ["brave"])
    denied = brave.search(Session(Resp(401, {})), "q", 1, ["brave"])
    assert "too many requests" in limited.failures[0][1] and "access denied" in denied.failures[0][1]


def test_quota_errors_bench_for_a_day():
    serper = search_providers.load_all()["serper"]({})
    reason = serper.http_failure(Resp(400, {"message": "Not enough credits"})).failures[0][1]
    r = ws.Rotation("serper", ["serper"], settings([]), {})
    r.bench("serper", reason)
    assert "quota" in reason and r.state["serper"]["benched_until"] == pytest.approx(time.time() + 86400, abs=5)


def test_redact_hides_keys(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "sk-secret")
    serper = search_providers.load_all()["serper"]({})
    assert serper.redact("GET https://x?key=sk-secret failed") == "GET https://x?key=*** failed"


# --- credentials ---------------------------------------------------------------------------------
def test_credentials_env_then_files(tmp_path, monkeypatch):
    f = tmp_path / "keys.env"
    f.write_text('# keys\nexport SERPER_API_KEY="from-file"\nBRAVE_API_KEY=b1 # comment\n')
    monkeypatch.setattr(credentials, "key_files", lambda: [f])
    credentials._file_values.cache_clear()
    assert credentials.get("SERPER_API_KEY") == "from-file" and credentials.get("BRAVE_API_KEY") == "b1"
    monkeypatch.setenv("SERPER_API_KEY", "from-env")
    assert credentials.get("SERPER_API_KEY") == "from-env" and credentials.where("SERPER_API_KEY") == "environment"


def test_tracked_env_file_is_refused(tmp_path, monkeypatch):
    f = credentials.ROOT / ".env.test-tracked"
    monkeypatch.setattr(credentials, "key_files", lambda: [f])
    monkeypatch.setattr(credentials, "tracked_by_git", lambda p: True)
    monkeypatch.setattr(credentials.Path, "is_file", lambda self: True)
    monkeypatch.setattr(credentials.Path, "read_text", lambda self, **k: "X=1")
    credentials._file_values.cache_clear()
    with pytest.raises(RuntimeError, match="tracked by git"):
        credentials.get("X")
    credentials._file_values.cache_clear()


def test_all_engines_failing_returns_the_failures_not_an_error():
    a = fake("free", [Answer(failures=[("e1", "captcha")]), Answer(failures=[("e2", "access denied")])],
             units=["e1", "e2", "e3"], per_query=1)
    paid = fake("paid", [], keys=["FAKE_KEY"])  # no key: can't help
    s = ws.Search(settings(["free", "paid"]), {"free": a, "paid": paid})
    ans, prov, _ = s.page(None, "q", 1)
    assert prov == "free" and ans.results == [] and ans.failures  # -> collect.py marks engines_blocked
