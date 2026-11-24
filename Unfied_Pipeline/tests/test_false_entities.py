"""Regression tests for false company names, trailing names, NCLT titles, and symbol leakage."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import parse_nse_records as p  # noqa: E402

# Exact extracted text of data/raw/priority_2017/6ddf19798b18ad47.pdf page 3 (DFM Foods post-offer public
# announcement; the newspaper page also carries an unrelated partly-paid call-money corrigendum).
DFM_LINES = (
    "Company allotted pursuant to Rights Issue on August 26, 2022 and having 1. Fag Re B rel] 2 % 8 . H 7 Y , e 1 l "
    "8 ,02 2 7 0 / 2 - 2 S qe T AT a A r t d A e h m l 7 afe a r f 9 e 1 r 3 r / e 7 t 7 AT hh T M 4 A 1 T .3-—se 29 "
    "r g f e ew e a e x tr 20 a 22 m e| 4 (Ua XY” ak 1 e Z2 ,4 o 5, r 0 e 00 7 t a = r fera fhe botom company mame "
    "should be read as\n"
    "faites4 fra ISIN: IN9509C01016 (partly paid-up equity shares). ADHARSHILA CAPITAL SERVICES LIMITED\n"
    "instof ePaarikdsh a Fir-iysel-Leerse Limiled. Other\n"
)


def test_dfm_adharshila_line_is_not_a_record():
    review = []
    assert p.rows_from_text(DFM_LINES, "VOLUNTARY_EXIT_OFFER", review) == []
    [c] = review
    assert c["reason"] == "company_name_after_isin_ambiguous"
    assert c["candidate_name"] == "faites4 fra"
    assert c["trailing_name"] == "ADHARSHILA CAPITAL SERVICES LIMITED"
    assert c["isin"] == "IN9509C01016"


def test_corrigendum_line_alone():
    line = "faites4 fra ISIN: IN9509C01016 (partly paid-up equity shares). ADHARSHILA CAPITAL SERVICES LIMITED"
    assert p.rows_from_text(line, "OTHER") == []


def test_symbol_is_never_a_word_of_the_name():
    # production master had symbol "BHAGYANAGAR" (the name's first word); the real symbol is BHAGYAPROP
    [r] = p.rows_from_text("ISIN of BHAGYANAGAR PROPERTIES LIMITED INE363W01018", "VOLUNTARY_EXIT_OFFER")
    assert r["company_name"] == "BHAGYANAGAR PROPERTIES LIMITED"
    assert r["nse_symbol"] is None


def test_labelled_symbol_is_kept():
    [r] = p.rows_from_text("Acme Textiles Limited Symbol: ACMETEX ISIN INE002A01018", "OTHER")
    assert r["nse_symbol"] == "ACMETEX" and r["company_name"] == "Acme Textiles Limited"


def test_symbol_after_isin_is_still_read():
    [r] = p.rows_from_text("1. ABC Industries Limited INE002A01018 ABCIND 08 June 2017", "COMPULSORY")
    assert r["nse_symbol"] == "ABCIND"


def test_trailing_company_name_is_not_a_symbol():
    review = []
    rows = p.rows_from_text("Acme Textiles Limited INE002A01018 ZENITH STEEL PIPES LIMITED", "OTHER", review)
    assert [r["nse_symbol"] for r in rows] == [None]


def test_free_text_name_must_look_like_a_company():
    review = []
    assert p.rows_from_text("garbled ocr xq INE002A01018", "OTHER", review) == []
    assert review and review[0]["reason"] == "name_not_company_like"


def test_private_company_is_not_a_listed_delisting():
    review = []
    assert p.rows_from_text("Supreme Hydro Engineering Private Limited INE002A01018", "OTHER", review) == []
    assert review[0]["reason"] == "private_company"


NCLT_HEAD = """IN THE NATIONAL COMPANY LAW TRIBUNAL,
MUMBAI BENCH, COURT - III
Dolphin Offshore Enterprises (India) Limited,
......Applicant
Vs.
Bombay Stock Exchange Limited (BSE),
......Respondent
In the matter of
Supreme Hydro Engineering Private Limited
……Operational Creditor
Vs.
Dolphin Offshore Enterprise (India) Limited
.…..Corporate Debtor
Order Pronounced on: 20.08.2024
the Respondent issued a letter intimating the applicant about the compulsory delisting of securities
"""


def test_nclt_cause_title_is_not_a_delisting_order():
    review = []
    assert p.rows_from_order(NCLT_HEAD, review) == []
    assert review[0]["candidate_name"] == "Supreme Hydro Engineering Private Limited"


VISESH = """ORDER PASSED BY THE DELISTING COMMITTEE OF BSE LTD. IN THE MATTER OF VISESH INFOTECNICS
LTD. FOR COMPULSORY DELISTING
In the matter of Visesh Infotecnics Ltd.
(i) The Depositories i.e. Central Depository Services India Ltd. (CDSL) and National
Securities Depositories Limited ("NSDL") have blocked the Benpos
Date: February 26, 2026
"""


def test_institution_abbreviation_is_not_the_company_symbol():
    [r] = p.rows_from_order(VISESH)
    assert r["company_name"].startswith("Visesh Infotecnics")
    assert r["nse_symbol"] is None


def test_order_symbol_next_to_the_company_name_is_kept():
    text = ("Delisting Committee\nIn the matter of the Company: Cox & Kings Limited\n"
            "the equity shares of Cox & Kings Limited (COX&KINGS) are hereby delisted from the Exchange.\n")
    [r] = p.rows_from_order(text)
    assert r["nse_symbol"] == "COX&KINGS"


def test_review_candidates_are_written_and_exported(tmp_path, monkeypatch):
    """A refused candidate reaches data/processed/extraction_review.csv; it never enters the master."""
    import common
    import normalize_and_merge as nm
    parsed, processed = tmp_path / "parsed", tmp_path / "processed"
    parsed.mkdir(parents=True)
    raw = tmp_path / "raw" / "priority_2017"
    raw.mkdir(parents=True)
    f = raw / "dfm.txt"
    f.write_text("Post offer public announcement: voluntary delisting\n" + DFM_LINES, encoding="utf-8")
    for mod in (p, nm, common):
        monkeypatch.setattr(mod, "PARSED", parsed, raising=False)
        monkeypatch.setattr(mod, "PROCESSED", processed, raising=False)
    monkeypatch.setattr(p, "ROOT", tmp_path)
    monkeypatch.setattr(p, "RAW", tmp_path / "raw")
    monkeypatch.setattr(p, "manifest_index", lambda: {})
    monkeypatch.setattr(p, "log", lambda *a, **k: None)
    monkeypatch.setattr(p, "event", lambda *a, **k: None)
    monkeypatch.setattr(p, "emit", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["parse_nse_records.py"])
    assert p.main() == 0
    review = json.loads((parsed / "_review.json").read_text())
    assert list(review) == ["raw/priority_2017/dfm.txt"]
    monkeypatch.setattr(nm, "log", lambda *a, **k: None)
    monkeypatch.setattr(nm, "emit", lambda *a, **k: None)
    processed.mkdir()
    assert nm.main() == 0
    csv_text = (processed / "extraction_review.csv").read_text()
    assert "ADHARSHILA CAPITAL SERVICES LIMITED" in csv_text and "faites4 fra" in csv_text
    assert "faites4" not in (processed / "nse_delisted_master.csv").read_text().lower()


def test_bse_debt_redemption_rows_are_not_equity_delistings():
    import pandas as pd
    df = pd.DataFrame([["Security Code", "Full Name", "Date of Delisting", "Reason"],
                       ["532702", "Gujarat State Petronet Ltd.", "12 Oct 2026", "AMALGAMATION/ MERGER"],
                       ["523628", "Poddar Housing and Development Limited", "07 Oct 2026", "Compulsory Delisting"],
                       ["974001", "Vertis Infrastructure Trust", "25 Sep 2026", "REDEMPTION"],
                       ["974002", "SK Finance Limited", "25 Sep 2026", "FINAL REDEMPTION"]])
    review = []
    rows = p.rows_from_table(df, "OTHER", review=review)
    assert [(r["company_name"], r["delisting_type"]) for r in rows] == [
        ("Gujarat State Petronet Ltd.", "AMALGAMATION_MERGER"), ("Poddar Housing and Development Limited", "COMPULSORY")]
    assert [c["reason"] for c in review] == ["debt_redemption_not_equity"] * 2


def test_unmapped_pdf_glyph_in_a_name_is_flagged(tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("Delisting notice\nStephano(cid:415)s Finance Ltd INE002A01018\n")
    rows, _ = p.parse_file(f)
    assert rows and rows[0]["parse_flags"] == ["name_has_unmapped_pdf_glyph"]
