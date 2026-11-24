import json
from pathlib import Path

import audit_dataset

ROOT = Path(__file__).resolve().parent.parent


def _row(**kw):
    base = {"record_id": "a1", "company_name": "ABC Ltd", "nse_symbol": "ABC", "isin": "INE002A01018",
            "delisting_type": "COMPULSORY", "order_date": "2017-01-01", "effective_delisting_date": "",
            "calendar_year": "2017", "source_url": "https://www.nseindia.com/x", "local_artifact_path": "README.md",
            "confidence_status": "VERIFIED_PRIMARY"}
    return {**base, **kw}


def test_clean_row_passes():
    r = audit_dataset.audit([_row()])
    assert r["error_count"] == 0 and r["warning_count"] == 0


def test_bad_rows_are_flagged(tmp_path, monkeypatch):
    (tmp_path / "data" / "raw").mkdir(parents=True)  # a checkout with downloads, so a missing one is an error
    (tmp_path / "data" / "raw" / "present.pdf").write_bytes(b"x")
    (tmp_path / "README.md").write_text("x")
    monkeypatch.setattr(audit_dataset, "ROOT", tmp_path)
    rows = [
        _row(record_id="dup"), _row(record_id="dup", company_name="Other Ltd", isin=""),
        _row(record_id="b", isin="INE123", company_name="Bad Isin Ltd"),
        _row(record_id="c", source_url="https://www.nseindia.com/static/complaints/defaulter-expelled-members",
             company_name="Broker Ltd", isin=""),
        _row(record_id="d", local_artifact_path="data/raw/missing.pdf", company_name="Gone Ltd", isin=""),
        _row(record_id="e", company_name="NaT", isin="", order_date=""),
    ]
    r = audit_dataset.audit(rows)
    for check in ("duplicate_record_id", "malformed_isin", "non_listing_source", "missing_artifact"):
        assert check in r["errors"], check
    for check in ("no_date", "suspicious_company_name"):
        assert check in r["warnings"], check


def test_query_plan_is_valid():
    plan = json.loads((ROOT / "config" / "query_plan.json").read_text())
    assert {"fetch_domains", "phases", "disk"} <= set(plan)
    seen = set()
    for ph in plan["phases"]:
        assert ph["name"] and ph["queries"]
        for q in ph["queries"]:
            text, tag = (q["q"], q["year_tag"]) if isinstance(q, dict) else (q, ph["year_tag"])
            assert tag == "master" or (tag.isdigit() and 2006 <= int(tag) <= 2026), (ph["name"], tag)
            assert text not in seen, f"duplicate query: {text}"
            seen.add(text)


def test_report_issue_dedupes_open_issues(tmp_path, monkeypatch):
    import report_issue as ri
    monkeypatch.setattr(ri, "ISSUES", tmp_path / "issues.jsonl")
    monkeypatch.setattr(ri, "LOGS", tmp_path)   # the lock file too: tests never write the real logs/
    monkeypatch.setattr(ri, "log", lambda *a, **k: None)
    assert ri.report("zero_rows", "3 orders gave 0 rows", ["a.pdf"]) == {"reported": True, "id": 1}
    again = ri.report("zero_rows", "3 orders gave 0 rows", ["b.pdf"])
    assert again["duplicate_of"] == 1 and again["times_seen"] == 2
    assert ri.report("fetch", "domain blocked", [])["id"] == 2
    [first, _] = ri.load()
    assert first["evidence"] == ["a.pdf", "b.pdf"]


def test_master_count_counts_records_not_lines(tmp_path, monkeypatch):
    import collect
    (tmp_path / "nse_delisted_master.csv").write_text('company_name,nse_symbol\n"A Ltd","X\nY"\nB Ltd,B\n')
    monkeypatch.setattr(collect, "PROCESSED", tmp_path)
    assert collect.master_count() == 2


def test_verify_name_matching_keeps_bracketed_words():
    import verify_records as v
    assert v.norm_name("Ponni Sugars (Orissa) Limited") != v.norm_name("Ponni Sugars (Erode) Limited")
    assert v.norm_name("Vimal Oil & Foods Ltd") == v.norm_name("Vimal Oil and Foods Limited")


def test_verify_detects_order_only_split_event():
    import verify_records as v
    rows = [{"record_id": "a", "company_name": "Zylog Systems Limited", "order_date": "2020-11-27",
             "effective_delisting_date": ""},
            {"record_id": "b", "company_name": "Zylog Systems Ltd", "order_date": "",
             "effective_delisting_date": "2021-02-24"}]
    assert v.split_events(rows) == {"a"}


def test_fresh_clone_without_raw_files_only_warns(tmp_path, monkeypatch):
    import audit_dataset as ad
    monkeypatch.setattr(ad, "ROOT", tmp_path)
    row = {"record_id": "a", "company_name": "ABC Ltd", "isin": None, "delisting_type": "COMPULSORY",
           "confidence_status": "VERIFIED_PRIMARY", "source_url": "https://nseindia.com/x",
           "local_artifact_path": "data/raw/master_lists/x.xlsx", "order_date": "2017-01-01", "calendar_year": "2017"}
    res = ad.audit([row])
    assert "missing_artifact" not in res["errors"] and "raw_files_not_downloaded" in res["warnings"]
    (tmp_path / "data" / "raw" / "master_lists").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "master_lists" / "other.pdf").write_bytes(b"x")
    assert "missing_artifact" in ad.audit([row])["errors"]
