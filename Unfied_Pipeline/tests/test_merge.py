from datetime import date

import normalize_and_merge as m


def test_financial_year():
    assert m.financial_year(date(2017, 4, 1)) == "FY2017-18"
    assert m.financial_year(date(2018, 3, 31)) == "FY2017-18"
    assert m.financial_year(None) is None


def test_regime_boundaries():
    assert m.regime_for(date(2009, 6, 9)) == "SEBI_2003_Guidelines"
    assert m.regime_for(date(2009, 6, 10)) == "SEBI_2009_Regs"
    assert m.regime_for(date(2021, 6, 10)) == "SEBI_2021_Regs"
    assert m.regime_for(date(2024, 9, 25)) == "SEBI_2024_Amendment"


def test_normalize_derives_fields():
    r = m.normalize({"company_name": "  ABC   Ltd ", "nse_symbol": "abc", "isin": "ine002a01018",
                     "delisting_type": "BOGUS", "order_date": "2017-02-01",
                     "effective_delisting_date": None, "confidence_status": "VERIFIED_PRIMARY"})
    assert r["company_name"] == "ABC Ltd"
    assert r["nse_symbol"] == "ABC"
    assert r["isin"] == "INE002A01018"
    assert r["delisting_type"] == "OTHER"
    assert r["calendar_year"] == 2017 and r["is_priority_year"] is True
    assert r["financial_year"] == "FY2016-17"


def _row(**kw):
    base = {"company_name": "ABC Ltd", "nse_symbol": None, "isin": "INE002A01018", "delisting_type": "COMPULSORY",
            "order_date": None, "effective_delisting_date": "2017-06-05", "source_url": "https://nseindia.com/a",
            "confidence_status": "VERIFIED_PRIMARY"}
    return m.normalize({**base, **kw})


def test_merge_prefers_confidence_and_fills_gaps():
    a = _row(nse_symbol=None)
    b = _row(nse_symbol="ABC", confidence_status="NEEDS_REVIEW", source_url="https://blog.example/x",
             effective_delisting_date="2017-06-20")
    assert m.same_event(a, b)
    merged = m.merge_group([b, a])
    assert merged["confidence_status"] == "VERIFIED_PRIMARY"
    assert merged["nse_symbol"] == "ABC"
    assert merged["source_count"] == 2


def test_events_far_apart_are_separate():
    assert not m.same_event(_row(), _row(effective_delisting_date="2019-01-01"))


def test_record_id_is_stable():
    assert m.merge_group([_row()])["record_id"] == m.merge_group([_row()])["record_id"]


def test_two_unverified_domains_upgrade_to_corroborated():
    a = _row(confidence_status="NEEDS_REVIEW", source_url="https://one.example/a")
    b = _row(confidence_status="NEEDS_REVIEW", source_url="https://two.example/b")
    assert m.merge_group([a, b])["confidence_status"] == "CORROBORATED_SECONDARY"


def test_undated_row_joins_the_companys_only_event(tmp_path, monkeypatch):
    import json
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    base = {"company_name": "Mandu Industries Limited", "nse_symbol": "MANDUIND", "isin": None,
            "order_date": None, "source_url": "https://nsearchives.nseindia.com/content/equities/delisted.xlsx",
            "local_artifact_path": "x.xlsx", "confidence_status": "VERIFIED_PRIMARY", "record_status": "DELISTED"}
    rows = [dict(base, effective_delisting_date=None, delisting_type="IBC_NCLT"),
            dict(base, effective_delisting_date="2016-11-22", delisting_type="COMPULSORY")]
    (parsed / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(m, "PARSED", parsed)
    monkeypatch.setattr(m, "PROCESSED", tmp_path)
    monkeypatch.setattr(m, "log", lambda *a, **k: None)
    monkeypatch.setattr(m, "emit", lambda *a, **k: None)
    m.main()
    out = (tmp_path / "nse_delisted_master.jsonl").read_text().splitlines()
    assert len(out) == 1 and json.loads(out[0])["effective_delisting_date"] == "2016-11-22"


def test_bse_only_delistings_go_to_their_own_file(tmp_path, monkeypatch):
    import csv
    import json
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    row = {"isin": None, "order_date": None, "effective_delisting_date": None, "delisting_type": "COMPULSORY",
           "local_artifact_path": "x.pdf", "confidence_status": "NEEDS_REVIEW", "record_status": "DELISTED"}
    rows = [dict(row, company_name="Adhunik Yarns Ltd", nse_symbol=None,
                 source_url="https://www.bseindia.com/downloads1/comp_delisted_cos_db.pdf"),
            dict(row, company_name="Mandu Industries Ltd", nse_symbol=None,
                 source_url="https://beta.bseindia.com/downloads1/TradingNotice.pdf"),
            dict(row, company_name="Mandu Industries Limited", nse_symbol="MANDUIND", effective_delisting_date="2016-11-22",
                 confidence_status="VERIFIED_PRIMARY",
                 source_url="https://nsearchives.nseindia.com/content/equities/delisted.xlsx")]
    (parsed / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(m, "PARSED", parsed)
    monkeypatch.setattr(m, "PROCESSED", tmp_path)
    monkeypatch.setattr(m, "log", lambda *a, **k: None)
    monkeypatch.setattr(m, "emit", lambda *a, **k: None)
    m.main()
    master = list(csv.DictReader((tmp_path / "nse_delisted_master.csv").open()))
    bse = list(csv.DictReader((tmp_path / "bse_only_delistings.csv").open()))
    assert [r["company_name"] for r in bse] == ["Adhunik Yarns Ltd"]
    assert [r["nse_symbol"] for r in master] == ["MANDUIND"]  # BSE notice joined the NSE company


def test_merged_record_keeps_every_source_file():
    a = _row(local_artifact_path="data/raw/master_lists/a.xlsx")
    b = _row(local_artifact_path="data/raw/priority_2017/b.pdf", source_url="https://nseindia.com/b",
             order_date="2017-05-01")
    best = m.merge_group([a, b])
    assert best["all_artifact_paths"] == "data/raw/priority_2017/b.pdf | data/raw/master_lists/a.xlsx"  # best first
    assert best["source_count"] == 2
