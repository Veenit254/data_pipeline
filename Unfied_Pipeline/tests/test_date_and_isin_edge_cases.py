"""Regression tests for date parsing, OCR date validation, and ISIN merging."""
import pytest

import parse_nse_records as p
from test_data_integrity import merge, row


@pytest.mark.parametrize("text,want", [
    ("2017-03-04", "2017-03-04"), ("2009-06-09", "2009-06-09"), ("2024-12-31", "2024-12-31"),     # ISO
    ("15 August 2017", "2017-08-15"), ("August 15, 2017", "2017-08-15"), ("15th Aug 2017", "2017-08-15"),
    ("September 27, 2021", "2021-09-27"), ("Sept 5, 2017", "2017-09-05"), ("05-Dec-2007", "2007-12-05"),
])
def test_iso_and_named_month_dates(text, want):
    assert p.to_iso(text) == want


@pytest.mark.parametrize("text", [
    "12-05-1887", "01-01-2199", "3O-O4-2O18", "31/02/2017", "2017-02-30", "45-13-2010", "Rs. 2017", "1/1/1",
])
def test_implausible_ocr_dates_are_rejected(text):
    assert p.to_iso(text) is None


def test_plausible_low_confidence_date_is_flagged_for_review(tmp_path, monkeypatch):
    # an OCR (NEEDS_REVIEW) source alone: the record stays NEEDS_REVIEW
    [r] = merge(tmp_path, monkeypatch, [row("Ocr Only Ltd", nse_symbol="OCRONLY", effective_delisting_date="2017-06-01",
                                            confidence_status="NEEDS_REVIEW", source_url="https://x.example/scan.pdf")])
    assert r["confidence_status"] == "NEEDS_REVIEW"
    # an OCR date filling a primary record's gap: kept, flagged
    [r] = merge(tmp_path, monkeypatch, [
        row("Gap Co Ltd", nse_symbol="GAPCO", delisting_type="COMPULSORY", order_date="2017-05-01"),
        row("Gap Co Ltd", nse_symbol="GAPCO", effective_delisting_date="2017-06-01", confidence_status="NEEDS_REVIEW",
            source_url="https://x.example/scan.pdf")])
    assert r["effective_delisting_date"] == "2017-06-01"
    assert "effective_delisting_date:needs_review_source" in r["review_flags"]


def test_isin_and_no_isin_rows_merge_keeping_the_valid_isin(tmp_path, monkeypatch):
    out = merge(tmp_path, monkeypatch, [
        row("Jindal Cotex Limited", nse_symbol="JINDCOT", effective_delisting_date="2017-03-24", delisting_type="COMPULSORY"),
        row("Jindal Cotex Limited", isin="INE904J01016", nse_symbol="JINDCOT", effective_delisting_date="2017-03-24",
            delisting_type="COMPULSORY", source_url="https://www.nseindia.com/o.pdf"),
        row("Jindal Cotex Limited", isin="INE904J01011", nse_symbol="JINDCOT", effective_delisting_date="2017-03-24",
            delisting_type="COMPULSORY", source_url="https://www.bseindia.com/o.pdf")])   # bad check digit
    assert len(out) == 1 and out[0]["isin"] == "INE904J01016"
