import pytest

import add_queries as aq
import tool_guard as g


@pytest.fixture(autouse=True)
def no_historical(monkeypatch):
    monkeypatch.setattr(aq, "existing_queries", lambda plan: {
        (q["q"] if isinstance(q, dict) else q).lower() for ph in plan["phases"] for q in ph["queries"]})


def plan():
    return {"phases": [{"name": "master", "year_tag": "master", "queries": ["NSE delisted list"]}],
            "fetch_domains": ["nseindia.com"], "deny_domains": ["youtube.com"], "search": {"providers": ["searxng"]}}


def test_appends_to_agent_added_and_skips_duplicates_and_junk():
    p = plan()
    out = aq.add(p, ["SEBI order delisting 2008", "nse delisted list", "x", "a `rm -rf` b"], "2008", [])
    assert out["added_queries"] == ["SEBI order delisting 2008"]
    assert [s["why"] for s in out["skipped_queries"]] == ["already in the plan", "too short/long", "disallowed characters"]
    assert p["phases"][-1] == {"name": "agent_added", "year_tag": "per_query", "pages": 1,
                               "queries": [{"q": "SEBI order delisting 2008", "year_tag": "2008"}]}
    assert p["search"] == {"providers": ["searxng"]}  # nothing else touched


def test_domains_validated():
    p = plan()
    out = aq.add(p, [], None, ["www.CapitalMarket.com", "m.youtube.com", "not a host", "nseindia.com"])
    assert out["added_domains"] == ["capitalmarket.com"] and len(out["skipped_domains"]) == 3


def test_year_tag_required_and_checked():
    for tag in (None, "2008; rm", "1850"):
        with pytest.raises(ValueError):
            aq.add(plan(), ["SEBI order delisting 2008"], tag, [])


def test_agent_may_run_it():
    assert g.check("run_commands", {"commands": [
        '.venv/bin/python scripts/add_queries.py --year-tag 2008 --query "SEBI order delisting 2008"']}) is None
