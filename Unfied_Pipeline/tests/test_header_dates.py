"""Effective dates read from a circular's header when its table has no date column. Provenance kept
(date_source), nothing invented, ambiguous headers flagged; delisting-reversal notices never become records."""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import normalize_and_merge as nm  # noqa: E402
import parse_nse_records as p  # noqa: E402

NSE_CIRCULAR = """National Stock Exchange of India
Circular
Download Ref No: NSE/CML/70787 Date: October 13, 2025
Subject: Voluntary Delisting of Equity Shares of Bhalchandram Clothing Limited w.e.f.
November 03, 2025.
pursuant to the same, it is hereby notified that the company shall be suspended from trading w.e.f. October 27, 2025.
Further the admission to dealings in the following security shall be withdrawn (delisted) w.e.f. November 03, 2025.
"""

BSE_NOTICE = """NOTICES
Notice No. 20180228-15 Notice Date 28 Feb 2018
Subject Compulsory Delisting of Companies
Trading Members of the Exchange are hereby informed that the undermentioned 8 companies that have remained
suspended for more than 3 years would be compulsorily delisted from the
platform of the Exchange, with effect from March 05, 2018 pursuant to order of the Delisting Committee
"""

LIGATURE = ("Subject Compulsory Delisting of Company This is to inform that the undermentioned company that has remained "
            "suspended for more than 6 months would be delisted from the platform of the Exchange, with e(cid:431)ect "
            "from August 6, 2026 pursuant to order of the Delisting Committee")


def rows(n=2, **kw):
    return [{"company_name": f"Company {i} Ltd", "effective_delisting_date": None, "order_date": None,
             "extraction": "table", **kw} for i in range(n)]


def test_suspension_date_is_never_the_delisting_date():
    assert [d for d, _ in p.header_dates(NSE_CIRCULAR)] == ["2025-11-03"]


def test_delisting_sentence_inside_a_suspension_sentence_is_found():
    assert [d for d, _ in p.header_dates(BSE_NOTICE)] == ["2018-03-05"]


def test_broken_ff_ligature():
    assert [d for d, _ in p.header_dates(LIGATURE)] == ["2026-08-06"]


def test_reference_dates_are_not_effective_dates():
    text = "Circular dated October 24, 2013. Date: October 13, 2025. The list of companies is attached."
    assert p.header_dates(text) == []


def test_single_header_date_fills_undated_table_rows_with_provenance():
    rs = rows()
    p.apply_header_dates(rs, NSE_CIRCULAR)
    assert {r["effective_delisting_date"] for r in rs} == {"2025-11-03"}
    assert {r["date_source"] for r in rs} == {"document_header"}
    assert all("w.e.f." in r["date_evidence"] for r in rs)


def test_table_with_a_date_column_is_left_alone():
    rs = rows()
    rs[0]["effective_delisting_date"] = "2025-11-10"
    p.apply_header_dates(rs, NSE_CIRCULAR)
    assert rs[1]["effective_delisting_date"] is None and "date_source" not in rs[1]


def test_two_header_dates_set_nothing_and_flag():
    text = ("Company A shall be delisted w.e.f. March 05, 2018. "
            "Company B shall be delisted w.e.f. April 10, 2018.")
    rs = rows()
    p.apply_header_dates(rs, text)
    assert all(r["effective_delisting_date"] is None for r in rs)
    assert all(r["parse_flags"] == ["header_date_ambiguous:2018-03-05|2018-04-10"] for r in rs)


def test_no_header_date_invents_nothing():
    rs = rows()
    p.apply_header_dates(rs, "Compulsory delisting of companies. Sr. No. Company Name")
    assert all(r["effective_delisting_date"] is None and "date_source" not in r for r in rs)


def test_order_rows_are_not_touched():
    rs = rows(1, extraction="order")
    p.apply_header_dates(rs, NSE_CIRCULAR)
    assert rs[0]["effective_delisting_date"] is None


def test_date_source_per_extraction():
    assert p.date_source({"extraction": "table", "effective_delisting_date": "2020-01-01"}) == "table_column"
    assert p.date_source({"extraction": "order", "order_date": "2020-01-01"}) == "document_text"
    assert p.date_source({"extraction": "table"}) is None
    assert p.date_source({"extraction": "table", "effective_delisting_date": "2020-01-01",
                          "date_source": "document_header"}) == "document_header"


REVERSAL = """NOTICE Subject Revocation
Sr. Scrip Company name No. Code 1 531343 India Infraspace Ltd In view thereof, the status of the company would be
changed from “Delisted” to “Suspended” w.e.f. March 27, 2026."""


def test_reversal_notice_creates_no_record():
    assert p.REVERSAL_RE.search(REVERSAL)
    assert not p.REVERSAL_RE.search(BSE_NOTICE) and not p.REVERSAL_RE.search(NSE_CIRCULAR)


def base(**kw):
    r = {"company_name": "Acme Textiles Ltd", "nse_symbol": "ACME", "isin": None, "delisting_type": "COMPULSORY",
         "order_date": None, "effective_delisting_date": "2018-03-05", "exit_or_fair_value_inr": None,
         "source_url": "https://www.bseindia.com/a.pdf", "local_artifact_path": "data/raw/a.pdf",
         "confidence_status": "VERIFIED_PRIMARY", "extraction": "table"}
    r.update(kw)
    return nm.normalize(r)


def test_merge_prefers_a_stated_date_source_over_a_header_one():
    a = base(date_source="document_header", source_url="https://www.bseindia.com/a.pdf")
    b = base(date_source="table_column", source_url="https://www.nseindia.com/b.pdf")
    rec = nm.merge_group([a, b])
    assert rec["date_source"] == "table_column"
    assert nm.merge_group([a])["date_source"] == "document_header"


def test_ambiguous_header_flag_reaches_the_record():
    r = base(effective_delisting_date=None, parse_flags=["header_date_ambiguous:2018-03-05|2018-04-10"])
    rec = nm.merge_group([r])
    assert "header_date_ambiguous:2018-03-05|2018-04-10" in rec["review_flags"]
    assert rec["date_source"] is None


def test_date_source_is_exported():
    assert "date_source" in nm.PROVENANCE_FIELDS


def test_first_leasing_bse_header_date_never_anchors_the_nse_event(tmp_path, monkeypatch):
    """Staging finding: a BSE notice's header date (its BSE delisting, 2018-05-11) out-ranked the NSE rows and
    split NSE's own event (letter: COMPULSORY 2018-05-30) off. NSE rows anchor; the BSE event stays BSE-only."""
    from test_data_integrity import merge, row
    letter = "https://nsearchives.nseindia.com/web/sites/default/files/inline-files/First%20Leasing.pdf"
    out = merge(tmp_path, monkeypatch, [
        row("First Leasing Company of India Ltd", effective_delisting_date="2018-05-11", delisting_type="IBC_NCLT",
            source_url="https://beta.bseindia.com/downloads1/TradingNotice_No_20180509_3.pdf",
            local_artifact_path="data/raw/bse.pdf", date_source="document_header"),
        row("First Leasing Company of India Ltd.", effective_delisting_date="2018-05-30",
            source_url="https://archives.nseindia.com/invest/content/db/DelistedCos_Buy_Sell.xls",
            local_artifact_path="data/raw/bs.xls"),
        row("First Leasing Company of India Ltd", order_date="2018-05-18", effective_delisting_date="2018-05-30",
            delisting_type="COMPULSORY", source_url=letter, local_artifact_path="data/raw/l.pdf"),
        row("First Leasing Company India Limited", nse_symbol="FIRSTLEASE", effective_delisting_date="2018-05-30",
            delisting_type="IBC_NCLT")])
    assert len(out) == 1, [(r["company_name"], r["effective_delisting_date"]) for r in out]
    r = out[0]
    assert r["effective_delisting_date"] == "2018-05-30" and r["nse_symbol"] == "FIRSTLEASE"
    assert "bseindia" not in r["all_source_urls"]            # the BSE event is not merged into NSE's
    bse = list(csv.DictReader((tmp_path / "bse_only_delistings.csv").open()))
    assert [(b["effective_delisting_date"]) for b in bse] == ["2018-05-11"]
