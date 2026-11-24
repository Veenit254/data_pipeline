"""Regression tests: dates, false entities, classification, entity matching, duplicates vs distinct events,
conflict/provenance flags, record_id stability, atomic outputs, audit.

The merge cases mirror real duplicate groups observed in official records:
Buy_Sell.xls vs delisted.xlsx (same event), order vs effective date (same event), Husys (two real events),
B S Appliances (sources disagree: flag, don't merge)."""
import csv
import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest

import audit_dataset as ad
import common
import normalize_and_merge as m
import ocr
import parse_nse_records as p

NSE_LIST = "https://nsearchives.nseindia.com/content/equities/delisted.xlsx"
BUY_SELL = "https://archives.nseindia.com/invest/content/db/DelistedCos_Buy_Sell.xls"
ORDER = "https://www.nseindia.com/web/sites/default/files/inline-files/Zylog%20Systems%20Limited.pdf"


# ---------- D1/D2 dates -------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("15 August 2017", "2017-08-15"), ("August 1, 2017", "2017-08-01"), ("1st August 2017", "2017-08-01"),
    ("21st March 2020", "2020-03-21"), ("March 21st, 2020", "2020-03-21"), ("2017-03-04", "2017-03-04"),
    ("2009-06-09", "2009-06-09"), ("04-03-2017", "2017-03-04"), ("4.3.2017", "2017-03-04"), ("24-Mar-17", "2017-03-24"),
    ("Mar 4, 2017", "2017-03-04"), ("23rd August, 2007", "2007-08-23"), ("w.e.f. August 22, 2008", "2008-08-22"),
    ("Sept 5, 2017", "2017-09-05"), ("2nd September 2019", "2019-09-02"),
])
def test_dates(text, want):
    assert p.to_iso(text) == want


@pytest.mark.parametrize("text", ["31-09-2017", "30-02-2020", "2017-13-01", "2017-02-30", "Section 21 2009"])
def test_impossible_dates_are_rejected(text):
    assert p.to_iso(text) is None


# ---------- D3 false entities from label lines -----------------------------------------------------------------
@pytest.mark.parametrize("line", [
    "ISIN: INE002A01018", "ISIN of the Company INE002A01018", "ISIN Code INE002A01018 Face Value Rs 10",
    "Scrip Code 500325 ISIN INE002A01018", "Symbol: IFBIND | ISIN INE559A01017", "CIN L17110MH1973PLC019786 INE002A01018",
    "ISIN No. INE002A01018", "NSE Symbol: RELIANCE ISIN INE002A01018",
])
def test_label_lines_are_not_companies(line):
    assert p.rows_from_text(line, "OTHER") == []


def test_label_prefix_is_stripped_from_a_real_name():
    [r] = p.rows_from_text("ISIN of BHAGYANAGAR PROPERTIES LIMITED INE002A01018", "OTHER")
    assert r["company_name"] == "BHAGYANAGAR PROPERTIES LIMITED" and r["nse_symbol"] != "ISIN"
    [r] = p.rows_from_text("1. Acme Textiles Limited ISIN INE002A01018 ACMETEX 15-08-2017", "OTHER")
    assert r["company_name"] == "Acme Textiles Limited" and r["nse_symbol"] == "ACMETEX"
    assert r["effective_delisting_date"] == "2017-08-15"


@pytest.mark.parametrize("name", ["ISIN", "Symbol: IFBIND | ISIN", "ISIN of the Company", "INE002A01018 Ltd"])
def test_name_check_rejects_labels(name):
    assert not p.name_ok(name, True)


# ---------- D8 classification ----------------------------------------------------------------------------------
def test_company_name_never_decides_the_row_type():
    df = pd.DataFrame([["Company Name", "Symbol", "Date of Delisting", "Reason"],
                       ["Amalgamated Electricity Co Ltd", "AMALELEC", "05-06-2017", "Compulsory"],
                       ["Liquidity Finance Ltd", "LIQFIN", "05-06-2017", ""],
                       ["Merger Mills Ltd", "MERGMILL", "05-06-2017", ""]])
    assert [r["delisting_type"] for r in p.rows_from_table(df, "COMPULSORY")] == ["COMPULSORY"] * 3


def test_an_unrecognised_reason_is_other_not_the_documents_guess():
    df = pd.DataFrame([["Symbol", "Company Name", "Delisted Date", "Type of Delisting"],
                       ["HUSYS", "Husys Consulting Limited", "09-06-2016", "Exit from ITP platform"],
                       ["ABC", "ABC Industries Ltd", "09-06-2016", "Voluntary Delisting"],
                       ["XYZ", "XYZ Industries Ltd", "09-06-2016", ""]])
    assert [r["delisting_type"] for r in p.rows_from_table(df, "COMPULSORY")] == ["OTHER", "VOLUNTARY_NO_EXIT", "COMPULSORY"]


def test_workbook_sheets_fall_back_to_their_own_type(tmp_path):
    f = tmp_path / "delisted.xlsx"
    with pd.ExcelWriter(f) as w:
        pd.DataFrame([["Symbol", "Company Name", "Delisted Date", "Type of Delisting"]] +
                     [[f"V{i}", f"Vol {i} Ltd", "01-01-2017", "Voluntary Delisting"] for i in range(5)]
                     ).to_excel(w, sheet_name="delisted", header=False, index=False)
        pd.DataFrame([["Symbol", "Company", "Delisted Date", "Exit Price"], ["BHORIND", "Bhor Industries Ltd", "24-03-2017", 0.92]]
                     ).to_excel(w, sheet_name="Exit Provided", header=False, index=False)
    rows, _ = p.parse_file(f)
    assert {r["company_name"]: r["delisting_type"] for r in rows}["Bhor Industries Ltd"] == "OTHER"


@pytest.mark.parametrize("text,want", [
    ("Voluntary delisting of equity shares. Exit offer at the discovered price of Rs 120. Note: the acquirer is "
     "not under any NCLT proceedings.", "VOLUNTARY_EXIT_OFFER"),
    ("delisting of Vibcon Industries Ltd", "OTHER"),
    ("pursuant to the resolution plan approved by the NCLT under the IBC, the shares are delisted", "IBC_NCLT"),
    ("delisted pursuant to the scheme of amalgamation sanctioned by the High Court", "AMALGAMATION_MERGER"),
    ("compulsorily delisted under Regulation 22(1)", "COMPULSORY"),
    ("Liquidity of the scrip was low", "OTHER"),
])
def test_classification_uses_words_not_substrings(text, want):
    assert p.classify(text) == want


# ---------- D13/D14 ISINs, prices, columns ----------------------------------------------------------------------
def test_table_isins_with_spaces_or_lowercase_are_recovered():
    df = pd.DataFrame([["Company Name", "ISIN", "Date of Delisting"],
                       ["ABC Industries Ltd", "ine002a01018", "05-06-2017"], ["XYZ Industries Ltd", "INE 009A01021", "05-06-2017"]])
    assert [r["isin"] for r in p.rows_from_table(df, "OTHER")] == ["INE002A01018", "INE009A01021"]


def test_normalize_drops_invalid_isins_and_normalizes_valid_ones():
    assert m.normalize({"company_name": "ABC Ltd", "isin": "INE002A01019"})["isin"] is None
    assert m.normalize({"company_name": "ABC Ltd", "isin": " ine 002a01018 "})["isin"] == "INE002A01018"


@pytest.mark.parametrize("cell,want", [("Rs. 125.50", 125.5), ("₹ 1,234.00", 1234.0), ("125.50 per share", 125.5),
                                       ("Rs.125/-", 125.0), (125, 125.0), ("INR 3,400", 3400.0), ("N.A.", None), ("", None)])
def test_exit_price_parsing(cell, want):
    df = pd.DataFrame([["Company Name", "Symbol", "Date of Delisting", "Exit Price"],
                       ["ABC Industries Ltd", "ABCIND", "05-06-2017", cell]])
    [row] = p.rows_from_table(df, "VOLUNTARY_EXIT_OFFER")
    assert row["exit_or_fair_value_inr"] == want


def test_last_traded_price_is_not_the_exit_price():
    df = pd.DataFrame([["Company Name", "Symbol", "Date of Delisting", "Last Traded Price"],
                       ["ABC Industries Ltd", "ABCIND", "05-06-2017", "3.45"]])
    [row] = p.rows_from_table(df, "COMPULSORY")
    assert row["exit_or_fair_value_inr"] is None


# ---------- D15 relevance and proposed lists ---------------------------------------------------------------------
def test_proposed_regex_does_not_hit_approved_proposals():
    assert not p.PROPOSED_RE.search("https://www.bseindia.com/delisting/approved-delisting-proposals-2017.pdf")
    assert p.PROPOSED_RE.search("https://nsearchives.nseindia.com/content/equities/proposed_delisting.xlsx")
    assert p.PROPOSED_RE.search("Companies to be delisted")


def test_a_link_from_a_delisting_page_alone_is_not_relevance():
    ok, _ = p.is_relevant(Path("x.pdf"), "https://www.nseindia.com/corporate/AnnualReport_2017.pdf",
                          "https://www.nseindia.com/regulations/delisting", {"title": "", "text_head": "Annual Report 2017"})
    assert not ok
    ok, why = p.is_relevant(Path("x.pdf"), "https://www.nseindia.com/files/Zylog%20Systems%20Limited.pdf",
                            "https://www.nseindia.com/regulations/delisting",
                            {"title": "", "text_head": "the equity shares shall be delisted from the Exchange"})
    assert ok, why


def test_implausibly_large_sheets_are_a_parse_error(tmp_path):
    rows = [["Company Name", "Symbol", "Date of Delisting"]] + [[f"Company {i} Ltd", f"C{i}", "01-01-2017"]
                                                              for i in range(p.MAX_ROWS_PER_FILE + 5)]
    f = tmp_path / "big.csv"
    pd.DataFrame(rows).to_csv(f, header=False, index=False)
    with pytest.raises(p.ParseLimit):
        p.parse_file(f)


# ---------- merge helpers ------------------------------------------------------------------------------------------
def merge(tmp_path, monkeypatch, rows, previous=None):
    parsed = tmp_path / "parsed"
    parsed.mkdir(exist_ok=True)
    for f in parsed.glob("*.jsonl"):
        f.unlink()
    (parsed / "a.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(m, "PARSED", parsed)
    monkeypatch.setattr(m, "PROCESSED", tmp_path)
    monkeypatch.setattr(m, "log", lambda *a, **k: None)
    monkeypatch.setattr(m, "emit", lambda *a, **k: None)
    m.main()
    return list(csv.DictReader((tmp_path / "nse_delisted_master.csv").open(encoding="utf-8")))


def row(name, **kw):
    base = {"company_name": name, "nse_symbol": None, "isin": None, "order_date": None, "effective_delisting_date": None,
            "delisting_type": "OTHER", "source_url": NSE_LIST, "local_artifact_path": "data/raw/x.xlsx",
            "confidence_status": "VERIFIED_PRIMARY", "record_status": "DELISTED"}
    return {**base, **kw}


# ---------- D4/D5/D6: duplicates that are one event -----------------------------------------------------------
def test_same_company_with_and_without_isin_is_one_record(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Jindal Cotex Limited", isin="INE904J01016", nse_symbol="JINDCOT", effective_delisting_date="2017-03-24",
            delisting_type="COMPULSORY"),
        row("Jindal Cotex Limited", nse_symbol="JINDCOT", order_date="2017-03-20", effective_delisting_date="2017-03-24",
            delisting_type="COMPULSORY", source_url=ORDER, local_artifact_path="data/raw/o.pdf")])
    assert len(out) == 1 and out[0]["isin"] == "INE904J01016" and out[0]["order_date"] == "2017-03-20"


def test_buy_sell_list_and_delisted_xlsx_are_one_event(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Acropetal Technologies Ltd", effective_delisting_date="2018-08-09", source_url=BUY_SELL,
            local_artifact_path="data/raw/b.xls"),
        row("Acropetal Technologies Limited", nse_symbol="ACROPETAL", effective_delisting_date="2018-08-08",
            delisting_type="COMPULSORY")])
    assert len(out) == 1
    r = out[0]
    assert (r["nse_symbol"], r["delisting_type"], r["effective_delisting_date"]) == ("ACROPETAL", "COMPULSORY", "2018-08-08")
    assert "2018-08-09" in r["source_conflicts"] and r["source_count"] == "2"


def test_order_and_its_effective_date_are_one_event(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Zylog Systems Limited", order_date="2020-11-27", delisting_type="COMPULSORY", source_url=ORDER,
            local_artifact_path="data/raw/o.pdf"),
        row("Zylog Systems Ltd", nse_symbol="ZYLOG", effective_delisting_date="2021-02-24", delisting_type="IBC_NCLT")])
    assert len(out) == 1
    r = out[0]
    assert (r["order_date"], r["effective_delisting_date"], r["calendar_year"]) == ("2020-11-27", "2021-02-24", "2021")
    assert "delisting_type" in r["source_conflicts"]          # COMPULSORY order vs IBC list: kept, and recorded


def test_conflicting_types_a_few_days_apart_are_one_event(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("ABC Ltd", isin="INE002A01018", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY"),
        row("ABC Ltd", isin="INE002A01018", effective_delisting_date="2017-03-28", source_url="https://news.example.com/x",
            confidence_status="NEEDS_REVIEW")])
    assert len(out) == 1 and out[0]["delisting_type"] == "COMPULSORY"


def test_conflicting_dates_keep_the_primary_and_record_the_conflict(tmp_path, monkeypatch):
    [r] = merge(tmp_path, monkeypatch, [
        row("ABC Ltd", isin="INE002A01018", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY"),
        row("ABC Ltd", isin="INE002A01018", effective_delisting_date="2017-04-10", delisting_type="COMPULSORY",
            source_url="https://news.example.com/x", confidence_status="NEEDS_REVIEW")])
    assert r["effective_delisting_date"] == "2017-03-24" and "2017-04-10" in r["source_conflicts"]


# ---------- distinct events stay distinct ---------------------------------------------------------------------
def test_two_real_delistings_of_one_company_stay_separate(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Husys Consulting Limited", nse_symbol="HUSYS", effective_delisting_date="2016-06-09", delisting_type="COMPULSORY"),
        row("Husys Consulting Limited", nse_symbol="HUSYSLTD", effective_delisting_date="2021-07-08",
            delisting_type="VOLUNTARY_NO_EXIT")])
    assert sorted(r["nse_symbol"] for r in out) == ["HUSYS", "HUSYSLTD"]


def test_disagreeing_sources_months_apart_are_not_merged(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("B S Appliances Ltd", nse_symbol="BPLSANUTIL", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY",
            source_url="https://archives.nseindia.com/research/dynaContent/NSE_Factbook-2017_Final.pdf"),
        row("B S Appliances Ltd", nse_symbol="BPLSANUTIL", effective_delisting_date="2018-05-22", delisting_type="COMPULSORY")])
    assert len(out) == 2
    res = ad.audit(out)
    assert res["warnings"]["possible_duplicate_event"]["count"] == 2


def test_order_dated_a_few_days_after_the_effective_date_still_pairs(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Poddar Housing and Development Limited", effective_delisting_date="2026-10-07", delisting_type="COMPULSORY"),
        row("Poddar Housing and Development Limited", order_date="2026-10-09", delisting_type="COMPULSORY", source_url=ORDER)])
    assert len(out) == 1 and (out[0]["order_date"], out[0]["effective_delisting_date"]) == ("2026-10-09", "2026-10-07")


def test_a_bse_event_of_an_nse_company_is_bse_only(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("BIL Industries Limited", nse_symbol="BIL", effective_delisting_date="2016-09-12", delisting_type="IBC_NCLT"),
        row("Bil Industries Ltd", effective_delisting_date="2016-08-17", delisting_type="COMPULSORY",
            source_url="https://www.bseindia.com/downloads1/comp_delisted_cos_db.pdf")])
    bse = list(csv.DictReader((tmp_path / "bse_only_delistings.csv").open()))
    assert [r["nse_symbol"] for r in out] == ["BIL"] and [r["effective_delisting_date"] for r in bse] == ["2016-08-17"]


def test_a_sheet_title_is_type_evidence_its_column_names_are_not():
    df = pd.DataFrame([["Report under Regulation 27 (c) pertaining to the Delisting Exit Offer", None, None],
                       ["Symbol", "Name of the Company", "Date of Delisting"], ["TCIDEVELOP", "TCI Developers Limited", "18-11-2022"]])
    assert p.sheet_type(df) == "VOLUNTARY_EXIT_OFFER"
    assert p.sheet_type(pd.DataFrame([["Symbol", "Company", "Delisted Date", "Exit Price"],
                                      ["BHORIND", "Bhor Industries Ltd", "24-03-2017", 0.92]])) == "OTHER"


def test_order_after_the_effective_date_is_not_merged(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Autoriders Finance Ltd", effective_delisting_date="2021-02-24", source_url=BUY_SELL),
        row("Autoriders Finance Ltd", nse_symbol="AUTORIDFIN", order_date="2023-03-31", delisting_type="COMPULSORY",
            source_url=ORDER)])
    assert len(out) == 2


def test_different_isins_with_one_name_are_different_companies(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Indian Mills Ltd", isin="INE002A01018", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY"),
        row("Indian Mills Ltd", isin="INE009A01021", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY"),
        row("Indian Mills Ltd", effective_delisting_date="2017-03-25", source_url=BUY_SELL)])
    assert len(out) == 3                         # the name cannot say which ISIN the third row belongs to
    assert any("ambiguous_company" in r["review_flags"] for r in out)


def test_undated_row_still_joins_the_companys_only_event(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Mandu Industries Limited", nse_symbol="MANDUIND", delisting_type="IBC_NCLT"),
        row("Mandu Industries Limited", nse_symbol="MANDUIND", effective_delisting_date="2016-11-22",
            delisting_type="COMPULSORY")])
    assert len(out) == 1 and out[0]["effective_delisting_date"] == "2016-11-22"


# ---------- D9 NEEDS_REVIEW (OCR) fields in verified records -------------------------------------------------
def test_fields_from_needs_review_sources_are_flagged_and_implausible_ones_rejected(tmp_path, monkeypatch):
    [r] = merge(tmp_path, monkeypatch, [
        row("Test Co Ltd", isin="INE002A01018", effective_delisting_date="2017-06-01", delisting_type="COMPULSORY"),
        row("Test Co Ltd", isin="INE002A01018", order_date="2017-03-01", effective_delisting_date="2017-06-01",
            source_url="https://blog.example/x", confidence_status="NEEDS_REVIEW", exit_or_fair_value_inr=12.5)])
    assert r["order_date"] == "2017-03-01" and r["confidence_status"] == "VERIFIED_PRIMARY"
    assert "order_date:needs_review_source" in r["review_flags"]
    [r] = merge(tmp_path, monkeypatch, [
        row("Test Co Ltd", isin="INE002A01018", effective_delisting_date="2017-06-01", delisting_type="COMPULSORY"),
        row("Test Co Ltd", isin="INE002A01018", order_date="2011-06-01", effective_delisting_date="2017-06-01",
            source_url="https://blog.example/x", confidence_status="NEEDS_REVIEW")])
    assert r["order_date"] == "" and "order_date_rejected" in r["review_flags"]


def test_dates_derived_fields_follow_the_merged_dates(tmp_path, monkeypatch):
    [r] = merge(tmp_path, monkeypatch, [
        row("Zeta Ltd", nse_symbol="ZETA", delisting_type="COMPULSORY", order_date="2009-05-01", source_url=ORDER),
        row("Zeta Ltd", nse_symbol="ZETA", effective_delisting_date="2009-07-01", confidence_status="NEEDS_REVIEW",
            source_url="https://blog.example/z")])
    assert (r["calendar_year"], r["regulatory_regime"], r["financial_year"]) == ("2009", "SEBI_2009_Regs", "FY2009-10")


# ---------- D11 record_id stability, D16 scope ---------------------------------------------------------------
def test_record_id_survives_a_better_source_arriving(tmp_path, monkeypatch):
    first = [row("ABC Ltd", isin="INE002A01018", effective_delisting_date="2017-03-28", delisting_type="COMPULSORY",
                 source_url="https://news.example.com/x", confidence_status="NEEDS_REVIEW")]
    id1 = merge(tmp_path, monkeypatch, first)[0]["record_id"]
    second = first + [row("ABC Ltd", isin="INE002A01018", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY")]
    [r] = merge(tmp_path, monkeypatch, second)
    assert r["record_id"] == id1 and r["effective_delisting_date"] == "2017-03-24"


def test_out_of_scope_years_go_to_their_own_file(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [row("Old Mills Ltd", effective_delisting_date="1998-01-05", delisting_type="COMPULSORY"),
                                        row("New Mills Ltd", effective_delisting_date="2018-01-05", delisting_type="COMPULSORY")])
    assert [r["company_name"] for r in out] == ["New Mills Ltd"]
    kept = list(csv.DictReader((tmp_path / "out_of_scope_delistings.csv").open()))
    assert [r["company_name"] for r in kept] == ["Old Mills Ltd"]


# ---------- D12 atomic outputs ----------------------------------------------------------------------------------
def test_atomic_write_keeps_the_old_file_on_failure(tmp_path, monkeypatch):
    f = tmp_path / "master.csv"
    f.write_text("OLD")
    monkeypatch.setattr(common.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(OSError):
        common.atomic_write_text(f, "NEW")
    assert f.read_text() == "OLD" and [x.name for x in tmp_path.iterdir()] == ["master.csv"]


def test_every_dataset_output_is_written_atomically(tmp_path, monkeypatch):
    written = []
    real = common.atomic_write_text
    monkeypatch.setattr(m, "atomic_write_text", lambda path, text: written.append(Path(path).name) or real(path, text))
    merge(tmp_path, monkeypatch, [row("ABC Ltd", effective_delisting_date="2017-01-01", delisting_type="COMPULSORY")])
    assert {"nse_delisted_master.csv", "nse_delisted_master.jsonl", "priority_years_audit.csv", "bse_only_delistings.csv",
            "proposed_delisting_leads.csv", "out_of_scope_delistings.csv"} <= set(written)


# ---------- D7 audit -------------------------------------------------------------------------------------------
def audit_rows(rows, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "ROOT", tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True, exist_ok=True)
    return ad.audit(rows)


def arow(rid, name, **kw):
    return {"record_id": rid, "company_name": name, "isin": "", "nse_symbol": "UNKNOWN", "delisting_type": "COMPULSORY",
            "confidence_status": "VERIFIED_PRIMARY", "source_url": NSE_LIST, "local_artifact_path": "x",
            "order_date": "", "effective_delisting_date": "2017-01-01", "calendar_year": "2017", **kw}


def test_audit_flags_duplicates_across_isin_and_type(tmp_path, monkeypatch):
    res = audit_rows([arow("1", "Jindal Cotex Limited", isin="INE904J01016"), arow("2", "Jindal Cotex Ltd", delisting_type="OTHER"),
                      arow("3", "Husys Consulting Ltd", effective_delisting_date="2016-06-09", calendar_year="2016"),
                      arow("4", "Husys Consulting Ltd", effective_delisting_date="2021-07-08", calendar_year="2021")],
                     tmp_path, monkeypatch)
    flagged = {e["record_id"] for e in res["warnings"]["possible_duplicate_event"]["examples"]}
    assert flagged == {"1", "2"}                     # 5 years apart: two events, not a duplicate


def test_audit_errors_on_label_names_and_flags_date_order(tmp_path, monkeypatch):
    res = audit_rows([arow("1", "ISIN"), arow("2", "Symbol: IFBIND | ISIN"), arow("3", "ISIN of BHAGYANAGAR PROPERTIES LIMITED"),
                      arow("4", "Good Mills Ltd", order_date="2018-01-01")], tmp_path, monkeypatch)
    assert res["errors"]["label_as_company_name"]["count"] == 3
    assert res["warnings"]["order_after_effective"]["count"] == 1


def test_audit_can_write_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "load_rows", lambda: [])
    target = tmp_path / "audit.json"
    ad.run(out=target)
    assert json.loads(target.read_text())["records"] == 0


# ---------- O10 OCR failures, O11 parse retry loops -------------------------------------------------------------
def scan_pdf(tmp_path, name="scan.pdf", pages=1):
    from PIL import Image
    pdf = tmp_path / "raw" / name
    pdf.parent.mkdir(exist_ok=True)
    imgs = [Image.new("RGB", (200, 200), "white") for _ in range(pages)]
    imgs[0].save(pdf, save_all=True, append_images=imgs[1:])
    return pdf


def test_failed_ocr_is_not_cached(tmp_path, monkeypatch):
    pdf = scan_pdf(tmp_path)
    monkeypatch.setattr(ocr, "OCR_DIR", tmp_path / "ocr")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "Error opening data"))
    text, errors = ocr.ocr_pdf_detail(pdf)
    assert text == "" and errors and not ocr.cache_path(pdf).exists()


def test_one_slow_page_does_not_lose_the_others(tmp_path, monkeypatch):
    pdf = scan_pdf(tmp_path, "two.pdf", pages=2)
    monkeypatch.setattr(ocr, "OCR_DIR", tmp_path / "ocr")
    monkeypatch.setattr(ocr, "available", lambda: True)
    calls = []

    def run(cmd, **k):
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, 120)
        return subprocess.CompletedProcess(cmd, 0, "page two text", "")
    monkeypatch.setattr(ocr.subprocess, "run", run)
    text, errors = ocr.ocr_pdf_detail(pdf)
    assert "page two text" in text and len(errors) == 1 and not ocr.cache_path(pdf).exists()


def test_ocr_that_fails_on_every_page_is_a_parse_error(tmp_path, monkeypatch):
    pdf = scan_pdf(tmp_path, "dead.pdf")
    monkeypatch.setattr(ocr, "ocr_pdf_detail", lambda path, max_pages=12: ("", ["page 1: tesseract exit 1"]))
    with pytest.raises(p.OcrFailed):
        p.parse_file(pdf)


def test_broken_file_is_not_reparsed_until_it_or_the_parser_changes(tmp_path, monkeypatch):
    raw, parsed = tmp_path / "data" / "raw" / "priority_2017", tmp_path / "data" / "processed" / "parsed"
    raw.mkdir(parents=True)
    (raw / "broken.pdf").write_bytes(b"%PDF-1.4 broken")
    monkeypatch.setattr(p, "ROOT", tmp_path)
    monkeypatch.setattr(p, "RAW", tmp_path / "data" / "raw")
    monkeypatch.setattr(p, "PARSED", parsed)
    monkeypatch.setattr(p, "MANIFEST", tmp_path / "manifest.jsonl")
    out = []
    monkeypatch.setattr(p, "emit", out.append)
    monkeypatch.setattr(p, "log", lambda *a, **k: None)
    monkeypatch.setattr(p, "event", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["parse_nse_records.py"])
    p.main()
    p.main()
    assert len(out[0]["errors"]) == 1 and out[1]["errors"] == [] and len(out[1]["known_errors"]) == 1
    os.utime(raw / "broken.pdf", (1, 2_000_000_000))    # the file changed: try again
    p.main()
    assert len(out[2]["errors"]) == 1
