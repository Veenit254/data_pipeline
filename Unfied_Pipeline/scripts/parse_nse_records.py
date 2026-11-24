"""Extract candidate delisting records from cached artifacts in data/raw/.

Handles XLSX/XLS/CSV sheets, HTML (tables + text), and PDFs (tables + text).
Writes data/processed/parsed/<artifact>.jsonl and prints a JSON summary.

Examples:
  python scripts/parse_nse_records.py
  python scripts/parse_nse_records.py --force
  python scripts/parse_nse_records.py --file data/raw/priority_2017/ab12cd34.pdf
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import warnings
from datetime import date
from io import StringIO
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

import ocr

from common import (
    MANIFEST, NON_LISTING_RE, PARSED, PROCEDURAL_RE, RAW, ROOT, atomic_write_text, confidence_for_url, emit, event, isin_equity,
    isin_valid, log, read_jsonl,
)

warnings.filterwarnings("ignore")

ISIN_RE = re.compile(r"\bIN[E9][A-Z0-9]{8}[0-9]\b")  # candidates; confirmed with common.isin_valid
DATE_RE = re.compile(
    r"\b(\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:\d{1,2}\s+)?[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*|\s+)\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4})\b"
)
SYMBOL_RE = re.compile(r"\b[A-Z][A-Z0-9&\-]{1,19}\b")
ORDINAL_RE = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.I)
NOT_SYMBOLS = {"LTD", "LIMITED", "INE", "THE", "AND", "OF", "ISIN", "CIN", "NSE", "BSE", "SYMBOL", "SCRIP", "CODE",
               "FACE", "VALUE", "RS", "INR", "NO", "WEF", "DATE", "NAME", "COMPANY"}
# Institutions named with their abbreviation in orders ("Central Depository Services (India) Limited (CDSL)",
# "BSE Limited (BSE)"): never the symbol of the company the order is about.
INSTITUTION_SYMBOLS = {"BSE", "NSE", "NSEIL", "MSEI", "CDSL", "NSDL", "SEBI", "RBI", "NCLT", "NCLAT", "IBBI", "MCA",
                       "ROC", "SAT", "CSE", "DSE", "ASE", "MSE", "NSCCL", "ICCL", "SCORES", "IBC"}
MAX_ROWS_PER_FILE = 5000   # the largest real delisting list has a few hundred rows; more means a wrong table


class ParseLimit(ValueError):
    """The file produced an implausible number of rows (reported as a parse error, never truncated)."""


class OcrFailed(RuntimeError):
    """A scanned PDF whose OCR failed on every page (reported as a parse error, retried when the file changes)."""


def parse_price(v) -> float | None:
    """Exit / fair value in rupees: 125, "Rs. 125.50", "Rs.125/-", "₹ 1,234.00", "125.50 per share", "INR 3,400"."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"(?i)\b(?:rs|inr)\b\.?|₹|/-", " ", str(v)).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def clean_isin(v) -> str | None:
    """ISIN from a cell or text: spaces removed, upper-cased, check digit verified."""
    m = ISIN_RE.search(re.sub(r"\s+", "", str(v or "")).upper())
    return m.group(0) if m and isin_valid(m.group(0)) else None

# header keyword -> schema field (checked in order; first match wins per column)
HEADER_MAP = [
    ("isin", "isin"),
    ("symbol", "nse_symbol"), ("ticker", "nse_symbol"), ("scrip code", "nse_symbol"),
    ("delist", "effective_delisting_date"),  # refined below: needs 'date'
    ("order", "order_date"), ("notice", "order_date"), ("approval", "order_date"),
    ("exit price", "exit_or_fair_value_inr"), ("fair value", "exit_or_fair_value_inr"),
    ("offer price", "exit_or_fair_value_inr"), ("discovered price", "exit_or_fair_value_inr"),
    ("floor price", "exit_or_fair_value_inr"),  # never a bare "price": "Last Traded Price" is not an exit price
    ("company", "company_name"), ("name of", "company_name"),
    ("security name", "company_name"), ("scrip name", "company_name"), ("name", "company_name"),
    ("date", "_plain_date"),  # only trusted as the delisting date in tables that also carry a symbol/ISIN
    ("reason", "_reason"), ("remark", "_reason"), ("category", "_reason"), ("type", "_reason"), ("details", "_reason"),
]

# Whole words only ("Vibcon" is not IBC, "Liquidity" is not liquidation), in priority order for ties.
TYPE_PATTERNS = [
    ("IBC_NCLT", re.compile(r"\bnclt\b|\binsolvency\b|\bresolution plan\b|\bibc\b|\bliquidat(?:ion|ed|or)\b", re.I)),
    ("AMALGAMATION_MERGER", re.compile(r"\bamalgamat(?:ion|ed)\b|\bmerger\b|\bscheme of arrangement\b|\bmerged\b", re.I)),
    ("COMPULSORY", re.compile(r"\bcompulsor(?:y|ily)\b", re.I)),
    ("VOLUNTARY_EXIT_OFFER", re.compile(r"\bexit offer\b|\breverse book\b|\bexit price\b|\bdiscovered price\b", re.I)),
    ("VOLUNTARY_NO_EXIT", re.compile(r"\bvoluntar(?:y|ily)\b", re.I)),
]


# Listing tables are never delisting lists. Tables about people/shareholdings are rejected unless
# they are keyed by a company symbol/ISIN (e.g. NSE's proposed-delisting list has a promoter column).
# Listing and market-activity tables (Factbook "most active days", turnover, market cap) are never delisting lists.
LISTING_HEADER_RE = re.compile(r"(?<!de)listing date|date of (?<!de)listing|listed on|traded value|turnover|"
                               r"traded quantity|market capitali|\brank\b|no\.? of trades", re.I)
PEOPLE_HEADER_RE = re.compile(r"director|designation|\bdin\b|shareholder|promoter|particulars|no\.? of shares|"
                              r"holding|\bpan\b|address", re.I)
COMPANY_RE = re.compile(r"\b(ltd|limited|corporation|corpn?|industries|company|co\.|inc|plc|bank|enterprises|"
                        r"holdings|mills|private|pvt|infra\w*|finance|financial|textiles|chemicals|steels?|"
                        r"pharma\w*|power|projects|exports|technologies|systems|services)\b", re.I)


LEGAL_TEXT_RE = re.compile(r"\bu/s\b|\bAct,?\s+\d{4}|\bSection\s+\d|\bRegulation\s+\d", re.I)


FRAGMENT_END_RE = re.compile(r"\s(is|are|was|were|has|have|had|the|of|and|to|for|by|in|on|with|from|that)$", re.I)


# Field labels that sit next to identifiers in documents: never part of a company name.
FIELD_LABEL_RE = re.compile(r"\b(?:ISIN(?:\s*(?:No\.?|Number|Code))?|(?:NSE\s*|Trading\s*)?Symbol|Scrip\s*(?:Code|ID|Name)|"
                      r"Security\s*(?:Code|ID)|BSE\s*Code|Face\s*Value|CIN)\b", re.I)
CIN_RE = re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")
GENERIC_NAME_RE = re.compile(r"^(?:of\s+)?(?:the\s+)?(?:company|issuer|security|scrip|equity shares?|shares?)$", re.I)


def name_ok(name: str, has_id: bool) -> bool:
    """Plausible company name. Without a symbol/ISIN the name must look like a company (filters
    sentences, people and OCR noise). Field labels (ISIN, Symbol, CIN, ...) and identifiers are never names."""
    if FIELD_LABEL_RE.search(name) or "|" in name or ISIN_RE.search(name.upper()) or CIN_RE.search(name.upper()):
        return False
    if GENERIC_NAME_RE.match(name.strip()):
        return False
    if not 3 <= len(name) <= 120 or "\n" in name or "•" in name or name[0] in "(-*•" or len(name.split()) > 12:
        return False
    if not re.search(r"[A-Za-z]{3}", name):
        return False
    if LEGAL_TEXT_RE.search(name):  # "Financial Institutions u/s. 4A of Companies Act, 1956"
        return False
    if FRAGMENT_END_RE.search(name):  # "Company is", "shares of the": a cut-off sentence
        return False
    return has_id or bool(COMPANY_RE.search(name))


def classify(text: str) -> str:
    """The delisting type the text talks about most (ties: the order of TYPE_PATTERNS). A voluntary exit-offer
    document that mentions an NCLT once stays a voluntary exit offer."""
    t = text or ""
    counts = [(len(rx.findall(t)), -i, label) for i, (label, rx) in enumerate(TYPE_PATTERNS)]
    n, _, label = max(counts)
    return label if n else "OTHER"


def to_iso(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    raw = m.group(0)
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if iso:  # ISO 8601 is year-month-day: never day-first
        try:
            d = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
        return d.isoformat() if 1990 <= d.year <= 2030 else None
    txt = ORDINAL_RE.sub("", re.sub(r",(?=\d)", ", ", raw))   # "21st" -> "21" (but "August" stays August)
    txt = re.sub(r"\bSept\b", "Sep", txt, flags=re.I)
    ts = pd.to_datetime(txt, dayfirst=True, errors="coerce")
    if pd.isna(ts) or not (1990 <= ts.year <= 2030):
        return None
    return ts.strftime("%Y-%m-%d")


def map_headers(cols) -> dict[int, str]:
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for i, c in enumerate(cols):
        h = re.sub(r"\s+", " ", str(c).lower()).strip()
        for kw, field in HEADER_MAP:
            if kw not in h:
                continue
            if kw == "delist" and "date" not in h:
                continue
            if field in used and field != "_reason":
                continue
            mapping[i] = field
            used.add(field)
            break
    return mapping


def find_header_row(df: pd.DataFrame, scan: int = 15, allow_people: bool = False) -> int | None:
    """Index of the header row of a delisting table (-1 = the DataFrame's own columns), else None.
    Needs a company-name column plus a symbol, ISIN or explicit delisting/order date column, and no
    listing/people/shareholding columns. People columns (promoter etc.) are allowed only in
    proposed-delisting lists (allow_people), which name the promoters of each listed company; in
    NSE's delisted.xlsx the promoter sheet is one row per person, not per delisting."""
    candidates = [(-1, list(df.columns))] + [(i, list(df.iloc[i].values)) for i in range(min(scan, len(df)))]
    for idx, cells in candidates:
        if any(LISTING_HEADER_RE.search(str(c)) for c in cells):
            continue
        m = set(map_headers(cells).values())
        if not (allow_people and m & {"nse_symbol", "isin"}) and any(PEOPLE_HEADER_RE.search(str(c)) for c in cells):
            continue
        if "company_name" in m and m & {"nse_symbol", "isin", "effective_delisting_date", "order_date"}:
            return idx
    return None


def sheet_type(df: pd.DataFrame) -> str:
    """Fallback type of one sheet from its title lines and data cells, never its column-header row ("Exit Price" as a
    column name is not evidence of an exit offer; a title "... pertaining to the Delisting Exit Offer" is)."""
    df = df.dropna(how="all").dropna(axis=1, how="all")
    hdr = find_header_row(df, allow_people=True)
    if hdr is not None and hdr >= 0:
        df = df.drop(df.index[hdr])
    return classify(df.to_string(header=False)[:20000])


# A reason cell saying a debt security was redeemed (BSE's delisted list mixes bonds, NCDs, InvITs and mutual
# fund units in): not an equity delisting.
DEBT_REASON_RE = re.compile(r"\b(?:final\s+)?redemption\b|\bredeemed\b|\bmaturity\b|\bmatured\b|\bcall option\b",
                            re.I)


def rows_from_table(df: pd.DataFrame, doc_type: str, allow_people: bool = False,
                    review: list | None = None) -> list[dict]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return []
    hdr = find_header_row(df, allow_people=allow_people)
    if hdr is None:
        return []
    if hdr >= 0:
        df.columns = list(df.iloc[hdr].values)
        df = df.iloc[hdr + 1:]
    mapping = map_headers(df.columns)
    fields = set(mapping.values())
    if "_plain_date" in fields and "effective_delisting_date" not in fields and fields & {"nse_symbol", "isin"}:
        mapping = {i: ("effective_delisting_date" if f == "_plain_date" else f) for i, f in mapping.items()}
    out = []
    for _, r in df.iterrows():
        vals = list(r.values)
        rec: dict = {}
        for i, field in mapping.items():
            v = vals[i] if i < len(vals) else None
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
                continue
            rec[field] = v
        name = re.sub(r"\s+", " ", str(rec.get("company_name", ""))).strip()  # cells often wrap names
        isin = clean_isin(rec.get("isin"))
        if not name:
            continue
        if name.lower() in ("company name", "name of the company", "total", "nan", "nat", "none"):
            continue
        symbol = str(rec.get("nse_symbol", "")).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9&\-]{0,19}", symbol):  # cells of bullet text are not symbols
            symbol = ""
        if name and not name_ok(name, bool(isin or symbol)):
            continue
        # the type comes from the reason column, else the row's other cells: never from the company name
        ident_cols = {i for i, f in mapping.items() if f in ("company_name", "nse_symbol", "isin")}
        row_text = " ".join(str(v) for i, v in enumerate(vals) if i not in ident_cols)
        if DEBT_REASON_RE.search(str(rec.get("_reason", ""))):
            if review is not None:
                review.append({"extraction": "table", "reason": "debt_redemption_not_equity", "candidate_name": name,
                               "trailing_name": None, "isin": isin, "line": str(rec.get("_reason", ""))[:300]})
            continue
        reason_type = classify(str(rec.get("_reason", "")))
        price = parse_price(rec.get("exit_or_fair_value_inr"))
        out.append({
            "company_name": name or None,
            "nse_symbol": symbol or None,
            "isin": isin,
            "order_date": to_iso(rec.get("order_date")),
            "effective_delisting_date": to_iso(rec.get("effective_delisting_date")),
            "exit_or_fair_value_inr": price,
            # the row's own reason first; a reason we can't classify ("Exit from ITP platform") is OTHER, never the
            # document-wide guess; that guess only applies to rows that say nothing at all
            "delisting_type": reason_type if reason_type != "OTHER" else "OTHER" if str(rec.get("_reason", "")).strip()
            else row_type if (row_type := classify(row_text)) != "OTHER" else doc_type,
            "extraction": "table",
        })
    return out


# Form labels and letterhead text that sit next to registration numbers in offer documents.
LABEL_RE = re.compile(r"SEBI Reg|E-?mail|@|\bCIN\b|Target Company|Registration No|Website|Tel\b|Fax\b", re.I)


# NSE's monthly "Processing Status of Voluntary Delisting Applications": one row per application; only
# rows whose status says the company is (or shall stand) delisted w.e.f. a date are delistings.
STATUS_DELISTED_RE = re.compile(r"delisted\s+(?:w\.?e\.?f\.?|with effect from)\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4}|"
                                r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", re.I)


def rows_from_status_table(df: pd.DataFrame) -> list[dict]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    for i in range(min(10, len(df))):
        cells = [str(c) for c in df.iloc[i].values]
        name_col = next((j for j, c in enumerate(cells) if re.search(r"name of (?:the )?company", c, re.I)), None)
        status_col = next((j for j, c in enumerate(cells) if re.search(r"\bstatus\b", c, re.I)), None)
        if name_col is None or status_col is None:
            continue
        nature_col = next((j for j, c in enumerate(cells) if re.search(r"nature", c, re.I)), None)
        out = []
        for vals in df.iloc[i + 1:].itertuples(index=False):
            status, name = str(vals[status_col]), re.sub(r"\s+", " ", str(vals[name_col])).strip()
            m = STATUS_DELISTED_RE.search(status)
            if not m or not name_ok(name, False):
                continue
            nature = str(vals[nature_col]) if nature_col is not None else ""
            out.append({"company_name": name, "nse_symbol": None, "isin": None, "order_date": None,
                        "effective_delisting_date": to_iso(m.group(1)), "exit_or_fair_value_inr": None,
                        "delisting_type": classify(nature + " " + status), "extraction": "status_table"})
        return out
    return []


def clean_text_name(before: str) -> str:
    """The company name in the text before an ISIN, without serial numbers, CINs, "Symbol: X", "Scrip Code 500325",
    bare labels ("ISIN", "ISIN of") and separators."""
    t = CIN_RE.sub(" ", before)
    t = re.sub(r"(?i)\b(?:(?:NSE|Trading)\s*)?Symbol\s*[:\-]?\s*[A-Z][A-Z0-9&\-]{0,19}\b", " ", t)
    t = re.sub(r"(?i)\b(?:Scrip|Security|BSE)\s*(?:Code|ID)\s*[:\-]?\s*\d{3,7}\b", " ", t)
    t = FIELD_LABEL_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" |,-:.\t")
    t = re.sub(r"^\s*\d+[.)]?\s*", "", t)                     # serial number
    t = re.sub(r"(?i)^(?:of|for)\s+(?:the\s+)?(?=\S)", "", t)  # "ISIN of <company>"
    return t.strip(" |,-:\t")


PRIVATE_COMPANY_RE = re.compile(r"\b(?:Private|Pvt\.?)\s+(?:Limited|Ltd\b)", re.I)
SYMBOL_LABEL_RE = re.compile(r"(?:NSE\s*|Trading\s*)?Symbol\s*[:\-]?\s*([A-Z][A-Z0-9&\-]{1,19})\b")
# The words right after an ISIN: a symbol ("INE002A01018 ABCIND 08 June 2017") or the start of another company's
# name ("IN9509C01016 ... ADHARSHILA CAPITAL SERVICES LIMITED")
AFTER_WORDS_RE = re.compile(r"\s*[|,;:\-]?\s*((?:[A-Z][A-Za-z0-9&.\-]*\s*){1,8})")
TRAILING_COMPANY_RE = re.compile(r"\b([A-Z][A-Z0-9&.\-]+(?:\s+[A-Z][A-Z0-9&.\-]+){0,8}\s+(?:LIMITED|LTD\.?))(?![A-Za-z])")


def text_name_ok(name: str) -> bool:
    """A company name read from a free-text line, where an ISIN alone is weak evidence (newspaper clippings,
    multi-column OCR): it must pass name_ok, look like a company and not be a private company."""
    return (name_ok(name, True) and not LABEL_RE.search(name) and bool(COMPANY_RE.search(name))
            and not PRIVATE_COMPANY_RE.search(name))


def rows_from_text(text: str, doc_type: str, review: list | None = None) -> list[dict]:
    """Rows from lines holding a valid ISIN. The name is the text before the ISIN; the symbol is a labelled
    "Symbol: X" or the single token right after the ISIN, never a word of the name. A line whose name is not a
    plausible company is not a record; if it carried a candidate name it goes to `review` (never silently kept)."""
    out = []
    for line in text.splitlines():
        m = ISIN_RE.search(line.upper())
        if not m or not isin_valid(m.group(0)):
            continue
        raw_before, after = line[: m.start()], line[m.end():]
        before = clean_text_name(raw_before)
        label = SYMBOL_LABEL_RE.search(line)
        sym = label.group(1) if label else None
        words = AFTER_WORDS_RE.match(after)
        trailing = TRAILING_COMPANY_RE.search(after)
        if not sym and words and not COMPANY_RE.search(words.group(1)):
            tok = words.group(1).split()[0]
            if SYMBOL_RE.fullmatch(tok) and tok not in NOT_SYMBOLS and tok not in INSTITUTION_SYMBOLS:
                sym = tok
        if sym and (sym in NOT_SYMBOLS or sym in INSTITUTION_SYMBOLS
                    or sym.lower() in {w.lower() for w in before.split()}):
            sym = None
        dates = [d for d in (to_iso(x) for x in DATE_RE.findall(line)) if d]
        if not before or not text_name_ok(before):
            # an ISIN without a usable company name on the line is not a record
            if review is not None and (before or trailing):
                reason = ("company_name_after_isin_ambiguous" if trailing and not COMPANY_RE.search(before)
                          else "private_company" if PRIVATE_COMPANY_RE.search(before)
                          else "name_not_company_like")
                review.append({"extraction": "text", "reason": reason, "candidate_name": before or None,
                               "trailing_name": trailing.group(1) if trailing else None,
                               "isin": m.group(0), "line": line.strip()[:300]})
            continue
        out.append({
            "company_name": before or None,
            "nse_symbol": sym,
            "isin": m.group(0),
            "order_date": None,
            "effective_delisting_date": dates[0] if dates else None,
            "exit_or_fair_value_inr": None,
            "delisting_type": doc_type,
            "extraction": "text",
        })
    return out


DELIST_RE = re.compile(r"delist", re.I)
CONTENT_SIGNAL_RE = re.compile(r"delist|withdrawn from (?:the )?(?:admission|dealings)|exit offer", re.I)
# Lists of companies *proposed* to be delisted ("approved-delisting-proposals" are not proposals any more).
PROPOSED_RE = re.compile(r"proposed|show.?cause|to.?be.?delisted", re.I)
# "In the matter of the Company: SecUR Credentials Limited (SECURCRED)", or without the symbol and with
# the name on the next line: "In the matter of the company\nM/s Petron Engineering Construction Limited".
MATTER_RE = re.compile(
    r"In the matter of(?: the [Cc]ompany)?\s*:?\s*(?:M/s\.?\s*)?([^\n(]{3,120}?)\s*"
    r"(?:\(\s*([A-Z][A-Z0-9&\-]{1,19})\s*\)|(?<=Limited)|(?<=Ltd)|(?<=Ltd\.)|\n)")
# A company name followed by its symbol anywhere in the order: "Cox & Kings Limited (COX&KINGS)"
NAME_SYMBOL_RE = re.compile(r"(?:Limited|Ltd\.?)\s*\(\s*([A-Z][A-Z0-9&\-]{2,19})\s*\)")
# Trading-member disciplinary orders (MCSGFC) share the order template but are not about listed companies.
MEMBER_ORDER_RE = re.compile(r"Trading Member|Settlement Guarantee Fund|MCSGFC")
ORDER_DATE_RE = re.compile(r"\bDate[d]?\s*:?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
DECISION_RE = re.compile(r"(?:be|is hereby|are hereby|shall be)\s+(compulsorily\s+)?(?:delisted|restrained and withdrawn)"
                         r"\s+from\s+the\s+Exchange[^.]{0,200}", re.I)
# Compulsory-delisting provisions: Reg 22(1) of the 2009 Regulations, Reg 32(1) of the 2021 Regulations.
COMPULSORY_REG_RE = re.compile(r"Regulation\s+(?:22|32)\s*\(\s*1\s*\)|compulsor", re.I)
EFFECTIVE_RE = re.compile(r"delisted[^.]{0,120}?(?:w\.e\.f\.?|with effect from)\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", re.I)


# Court cause-title roles: "In the matter of X ......Operational Creditor Vs. Y ......Corporate Debtor" names
# litigants, not a company an exchange delisted.
PARTY_ROLE_RE = re.compile(r"\.{2,}\s*(?:Operational|Financial)\s+Creditor|\.{2,}\s*Corporate\s+Debtor|"
                           r"\.{2,}\s*(?:Applicant|Respondent|Petitioner)|[\u2026]+\s*(?:Operational|Financial|"
                           r"Corporate|Applicant|Respondent|Petitioner)", re.I)


def order_symbol(text: str, name: str) -> str | None:
    """The symbol in "<name> Limited (SYMBOL)": only next to the company's own name, never an institution's
    abbreviation ("Central Depository Services (India) Limited (CDSL)")."""
    first = next((w for w in re.findall(r"[A-Za-z]{3,}", name) if w.lower() not in {"the", "shri", "sri"}), None)
    for s in NAME_SYMBOL_RE.finditer(text):
        sym = s.group(1)
        if sym in INSTITUTION_SYMBOLS or sym in NOT_SYMBOLS:
            continue
        if first and re.search(rf"\b{re.escape(first)}\b", text[max(0, s.start() - 120): s.start()], re.I):
            return sym
    return None


def rows_from_order(text: str, review: list | None = None) -> list[dict]:
    """Single-company Delisting Committee / exchange orders: one row from the order template."""
    head = text[:4000]
    if not re.search(r"Delisting Committee|In the matter of", head, re.I) or MEMBER_ORDER_RE.search(head):
        return []
    m = MATTER_RE.search(head)
    if not m:
        return []
    name = re.sub(r"\s+", " ", m.group(1)).strip(" :,-")
    symbol = m.group(2) if m.group(2) not in INSTITUTION_SYMBOLS else None
    if not symbol:
        symbol = order_symbol(text, name)
    if not name_ok(name, bool(symbol)):  # "In the matter of Non-Obtaining of SCORES Authentication"
        return []
    if PRIVATE_COMPANY_RE.search(name) or PARTY_ROLE_RE.search(head[m.start(): m.end() + 200]):
        if review is not None:  # a court case caption or a private company: not an exchange delisting
            review.append({"extraction": "order", "reason": "private_company" if PRIVATE_COMPANY_RE.search(name)
                           else "court_case_party", "candidate_name": name, "trailing_name": None,
                           "isin": None, "line": re.sub(r"\s+", " ", head[m.start(): m.end() + 120])[:300]})
        return []
    dates = ORDER_DATE_RE.findall(text[-3000:])
    found = list(DECISION_RE.finditer(text))
    # Orders warn "failing which ... shall be delisted" before the actual decision: prefer the
    # compulsory decision, else the last delisting sentence.
    decision = next((d for d in found if d.group(1)), found[-1] if found else None)
    eff = EFFECTIVE_RE.search(decision.group(0)) if decision else None
    isin = next((i for i in ISIN_RE.finditer(text.upper()) if isin_valid(i.group(0))), None)
    dtype = classify(decision.group(0)) if decision else classify(head)
    if decision and (decision.group(1) or COMPULSORY_REG_RE.search(decision.group(0))):
        dtype = "COMPULSORY"
    return [{
        "company_name": name,
        "nse_symbol": symbol,
        "isin": isin.group(0) if isin else None,
        "order_date": to_iso(dates[-1]) if dates else None,
        "effective_delisting_date": to_iso(eff.group(1)) if eff else None,
        "exit_or_fair_value_inr": None,
        "delisting_type": dtype,
        "extraction": "order" if decision else "order_no_decision",
    }]


# NSE listing-department letters to a company ("Ref: NSE/LIST/... <date>", "The Company Secretary\n<name>",
# "... delisted (withdrawn from admission to dealings) w.e.f <date>"), often scanned.
LETTER_REF_RE = re.compile(r"Ref\s*[:.]?\s*NSE/LIST/[^\n]*?([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4})")
LETTER_TO_RE = re.compile(r"The Company Secretary\W*\n+\W*([^\n]{3,100})")
LETTER_SUBJECT_RE = re.compile(r"Sub(?:ject)?\s*[:.\-]+\s*([^\n]{3,160})")
LETTER_EFFECTIVE_RE = re.compile(r"delist\w*[^.]{0,220}?(?:w\.?e\.?f\.?|with effect from)\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4})",
                                 re.I | re.S)


def rows_from_letter(text: str) -> list[dict]:
    text = text.replace("_", " ")  # OCR reads "March 24" as "March_24"
    ref, to = LETTER_REF_RE.search(text), LETTER_TO_RE.search(text)
    # The Exchange's own decision is the last "delist ... w.e.f." sentence; earlier ones quote BSE's
    # delisting or the original listing date.
    effs = [m for m in LETTER_EFFECTIVE_RE.finditer(text) if not re.search(r"\bBSE\b|listed on", m.group(0))]
    eff = effs[-1] if effs else None
    if not (ref and to and eff):
        return []
    name = re.sub(r"\s+", " ", re.sub(r"[^\w&.,()' -]", " ", to.group(1))).strip(" .,-")
    name = re.sub(r"^M/s\.?\s*", "", name)
    if not name_ok(name, False):
        return []
    subject = LETTER_SUBJECT_RE.search(text)
    dtype = classify(subject.group(1)) if subject else "OTHER"
    if dtype == "OTHER":
        dtype = "COMPULSORY" if re.search(r"withdrawn from admission to dealings|compulsor", text, re.I) else classify(text)
    return [{"company_name": name, "nse_symbol": None, "isin": None, "order_date": to_iso(ref.group(1)),
             "effective_delisting_date": to_iso(eff.group(1)), "exit_or_fair_value_inr": None,
             "delisting_type": dtype, "extraction": "letter"}]


# A circular/notice whose table has no date column states the date in its text: "the admission to dealings ...
# shall be withdrawn (delisted) w.e.f. November 03, 2025", "would be delisted ... with effect from March 05, 2018".
HEADER_SCAN_CHARS = 6000
_HDATE = (r"([A-Z][a-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+\.?,?\s+\d{4}"
          r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})")
HEADER_EFFECTIVE_RE = re.compile(r"(delist\w*|withdrawn|withdraw the admission|cease\w* to be listed)([^.;]{0,250}?)"
                                 r"(?:w\.?\s?e\.?\s?f\.?|with effect from)\s*:?\s*" + _HDATE, re.I | re.S)


def header_dates(text: str) -> list[tuple[str, str]]:
    """Distinct effective delisting dates stated in the document's opening text, each with its evidence snippet.
    Only a date bound to a delisting verb in the same sentence counts (never a suspension date or a reference
    date like "circular dated ...")."""
    out: dict[str, str] = {}
    head = (text or "")[:HEADER_SCAN_CHARS].replace("_", " ")
    head = re.sub(r"(?<=e)\(cid:\d+\)(?=ect)", "ff", head)   # "with e(cid:431)ect from": a broken ff ligature
    for start in re.finditer(r"delist|withdraw|cease", head, re.I):  # every verb (spans may overlap)
        m = HEADER_EFFECTIVE_RE.match(head, start.start())
        if not m or re.search(r"suspen", m.group(2), re.I):
            continue
        d = to_iso(m.group(3))
        if d:
            out.setdefault(d, re.sub(r"\s+", " ", m.group(0))[:200])
    return list(out.items())


# A notice reversing a delisting ("the status of the company would be changed from “Delisted” to “Suspended”"):
# the company named in it is not delisted by this document.
REVERSAL_RE = re.compile(r"from\s*[\"“”']?\s*Delisted\s*[\"“”']?\s*to\s*[\"“”']?\s*(?:Suspended|Active|Listed)|"
                         r"revocation of (?:the )?delisting|delisting (?:order )?(?:is |stands )?(?:revoked|set aside)", re.I)


def apply_header_dates(rows: list[dict], text: str) -> None:
    """Fill the effective date of table/text rows from the document header, only when none of those rows has a
    date of its own (the table has no date column). One distinct header date: used, with provenance. Several:
    no date is set and every row is flagged for review. No header date: nothing is invented."""
    targets = [r for r in rows if r.get("extraction") in ("table", "text", "status_table")]
    if not targets or any(r.get("effective_delisting_date") or r.get("order_date") for r in targets):
        return
    found = header_dates(text)
    if len(found) == 1:
        d, evidence = found[0]
        for r in targets:
            r.update(effective_delisting_date=d, date_source="document_header", date_evidence=evidence)
    elif len(found) > 1:
        for r in targets:
            r.setdefault("parse_flags", []).append("header_date_ambiguous:" + "|".join(d for d, _ in found))


def date_source(r: dict) -> str | None:
    if r.get("date_source"):
        return r["date_source"]
    if not (r.get("effective_delisting_date") or r.get("order_date")):
        return None
    return {"table": "table_column", "status_table": "table_column", "text": "line_text"}.get(
        r.get("extraction"), "document_text")


def is_relevant(path: Path, src: str | None, harvested_from: str | None, stats: dict) -> tuple[bool, str]:
    """Only documents about delisting may produce rows. Returns (relevant, reason)."""
    where = " ".join(x for x in (src, harvested_from, path.name) if x)
    if NON_LISTING_RE.search(src or ""):
        return False, "non_listing_page"
    if PROCEDURAL_RE.search(unquote((src or path.name).rsplit("/", 1)[-1])):
        return False, "procedural_document"
    if DELIST_RE.search(src or ""):
        return True, "url"
    if path.suffix.lower() in (".html", ".htm"):  # page bodies include site-wide nav menus: trust the title only
        return (True, "title") if DELIST_RE.search(stats.get("title", "")) else (False, "html_title_unrelated")
    if DELIST_RE.search(harvested_from or ""):  # linked from a delisting page: the document must say so too
        head = stats.get("text_head", "")
        if CONTENT_SIGNAL_RE.search(head):
            return True, "linked_and_content"
        if not head.strip():  # no text layer (scan without OCR): nothing contradicts the link
            return True, "linked_no_text"
        return False, "linked_from_delisting_page_without_delisting_text"
    if DELIST_RE.search(stats.get("text_head", "")):
        return True, "content"
    return False, f"no_delisting_signal ({where[-60:]})"


def parse_file(path: Path, proposed: bool = False) -> tuple[list[dict], dict]:
    ext = path.suffix.lower()
    stats = {"tables_seen": 0, "no_text": False, "title": "", "text_head": "", "review": []}
    rows: list[dict] = []
    text = ""
    if ext in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None, header=None)
        for df in sheets.values():
            stats["tables_seen"] += 1
            text += df.to_string()[:20000]
        for df in sheets.values():  # a workbook mixes sheets of every type: each sheet's fallback is its own data
            rows += rows_from_table(df, sheet_type(df), proposed, stats["review"]) or rows_from_status_table(df)
    elif ext == ".csv":
        df = pd.read_csv(path, header=None, dtype=str, on_bad_lines="skip", encoding_errors="ignore")
        stats["tables_seen"] = 1
        text = df.head(50).to_string()
        rows = rows_from_table(df, classify(df.to_string()[:20000]), proposed, stats["review"])
    elif ext in (".html", ".htm"):
        from bs4 import BeautifulSoup
        html = path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "lxml")
        stats["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = soup.get_text("\n", strip=True)
        doc_type = classify(text)
        try:
            tables = pd.read_html(StringIO(html), header=None)
        except (ValueError, ImportError):  # no tables, or a parser backend missing: fall back to text
            tables = []
        stats["tables_seen"] = len(tables)
        for df in tables:
            rows += rows_from_table(df, doc_type, proposed, stats["review"]) or rows_from_status_table(df)
        if not rows:
            rows = rows_from_text(text, doc_type, stats["review"])
        if not rows:
            rows = rows_from_order(text, stats["review"])
    elif ext == ".pdf":
        import pdfplumber
        tables = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
                for t in page.extract_tables() or []:
                    if t and len(t) > 1:
                        tables.append(pd.DataFrame(t))
        stats["tables_seen"] = len(tables)
        stats["no_text"] = not text.strip()
        if stats["no_text"]:  # scanned PDF: OCR it (cached), then use the text extractors
            text, ocr_errors = ocr.ocr_pdf_detail(path)
            if ocr_errors and not text.strip():
                raise OcrFailed("; ".join(ocr_errors)[:300])
            stats["ocr"] = bool(text.strip())
            stats["ocr_errors"] = ocr_errors
            stats["text_head"] = text[:2000]
        doc_type = classify(text)
        order_rows = rows_from_order(text, stats["review"]) or rows_from_letter(text)
        if order_rows:  # order template wins: its tables are compliance lists, not delisted companies
            rows = order_rows
        else:
            for df in tables:
                rows += rows_from_table(df, doc_type, proposed, stats["review"]) or rows_from_status_table(df)
            if not rows:
                rows = rows_from_text(text, doc_type, stats["review"])
    elif ext == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
        rows = rows_from_text(text, classify(text), stats["review"])
    else:
        stats["skipped_ext"] = ext
    stats["text_head"] = text[:6000]
    if rows and REVERSAL_RE.search(text[:HEADER_SCAN_CHARS]):
        m = REVERSAL_RE.search(text[:HEADER_SCAN_CHARS])
        stats["review"] += [{"extraction": r.get("extraction"), "reason": "delisting_reversed_notice",
                             "candidate_name": r.get("company_name"), "trailing_name": None, "isin": r.get("isin"),
                             "line": re.sub(r"\s+", " ", text[max(0, m.start() - 150): m.end() + 60])[:300]}
                            for r in rows]
        rows = []
    if ext in (".pdf", ".html", ".htm", ".txt"):
        apply_header_dates(rows, text)
    for r in rows:  # "Stephano(cid:415)s Finance Ltd": a glyph the PDF font did not map; a human must read it
        if "(cid:" in (r.get("company_name") or ""):
            r.setdefault("parse_flags", []).append("name_has_unmapped_pdf_glyph")
    if len(rows) > MAX_ROWS_PER_FILE:
        raise ParseLimit(f"{len(rows)} rows from one file (> {MAX_ROWS_PER_FILE}): not a delisting list")
    return rows, stats


def parser_version() -> str:
    """Changes whenever the parser or OCR code changes: files that failed are retried then."""
    h = hashlib.sha256()
    for f in ("parse_nse_records.py", "ocr.py", "common.py"):
        h.update((Path(__file__).parent / f).read_bytes())
    return h.hexdigest()[:12]


def manifest_index() -> dict[str, dict]:
    return {e["local_path"]: e for e in read_jsonl(MANIFEST) if e.get("local_path")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--file", type=Path)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    PARSED.mkdir(parents=True, exist_ok=True)
    files = [a.file if a.file.is_absolute() else ROOT / a.file] if a.file else \
        sorted(p for p in RAW.rglob("*") if p.is_file() and not p.name.startswith("."))
    idx = manifest_index()
    errors_path = PARSED / "_errors.json"
    known = json.loads(errors_path.read_text(encoding="utf-8")) if errors_path.exists() else {}
    version = parser_version()
    relevance_path = PARSED / "_relevance.json"
    relevance = json.loads(relevance_path.read_text(encoding="utf-8")) if relevance_path.exists() else {}
    # candidates an extractor refused (OCR garbage, a name after the ISIN, court captions): kept for a human
    review_path = PARSED / "_review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    summary = {"files_seen": len(files), "parsed": 0, "skipped_up_to_date": 0, "rows": 0,
               "irrelevant_files": 0, "zero_row_files": [], "no_text": [], "errors": [], "known_errors": [],
               "ocr_page_errors": []}

    for p in files:
        rel = str(p.relative_to(ROOT))
        out = PARSED / f"{p.parent.name}__{p.stem}.jsonl"
        if out.exists() and not a.force and out.stat().st_mtime >= p.stat().st_mtime:
            summary["skipped_up_to_date"] += 1
            continue
        k = known.get(rel)
        if k and not a.force and k.get("mtime") == p.stat().st_mtime and k.get("parser") == version:
            summary["known_errors"].append({"file": rel, "error": k["error"]})  # not retried until file/parser change
            continue
        try:
            rows, stats = parse_file(p, proposed=bool(PROPOSED_RE.search(idx.get(rel, {}).get("url") or rel)))
        except Exception as e:  # keep going; one bad artifact shouldn't stop the batch
            err = f"{type(e).__name__}: {e}"[:200]
            summary["errors"].append({"file": rel, "error": err})
            known[rel] = {"error": err, "mtime": p.stat().st_mtime, "parser": version}
            log(f"parse error {rel}: {e}", level="ERROR", kind="parse_error", file=rel)
            continue
        known.pop(rel, None)
        if stats.get("ocr_errors"):
            summary["ocr_page_errors"].append({"file": rel, "errors": stats["ocr_errors"][:5]})
        m = idx.get(rel, {})
        src = m.get("url")
        relevant, reason = is_relevant(p, src, m.get("harvested_from"), stats)
        relevance[rel] = reason if relevant else f"irrelevant:{reason}"
        if not relevant:
            rows = []
            summary["irrelevant_files"] += 1
        if relevant and stats.get("review"):
            review[rel] = [dict(c, source_url=src, local_artifact_path=rel) for c in stats["review"]]
        else:
            review.pop(rel, None)
        conf = confidence_for_url(src) if src else "NEEDS_REVIEW"
        if stats.get("ocr"):  # OCR can misread names and dates: a human should confirm
            conf = "NEEDS_REVIEW"
        # Lists of companies *proposed* to be delisted are leads, not delistings.
        record_status = "PROPOSED" if PROPOSED_RE.search(src or rel) else "DELISTED"
        # debt securities (NCDs, bonds, CP) are suspended/redeemed, not delisted companies
        rows = [r for r in rows if not r.get("isin") or isin_equity(r["isin"])]
        lines = []
        for r in rows:
            r["date_source"] = date_source(r)
            r.update({"source_url": src, "local_artifact_path": rel, "year_tag": m.get("year_tag"),
                      "record_status": record_status, "ocr": bool(stats.get("ocr")),
                      "confidence_status": conf if (r.get("isin") or r.get("effective_delisting_date")
                                                    or r.get("order_date")) else "NEEDS_REVIEW"})
            lines.append(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        atomic_write_text(out, "".join(lines))  # the merge may be reading parsed/ right now
        summary["parsed"] += 1
        summary["rows"] += len(rows)
        event("parse_file", file=rel, rows=len(rows), tables_seen=stats["tables_seen"],
              no_text=stats.get("no_text", False), source_url=src, relevant=relevant, relevance=reason)
        if not rows and relevant:
            summary["zero_row_files"].append({"file": rel, "tables_seen": stats["tables_seen"]})
        if stats.get("no_text"):
            summary["no_text"].append(rel)

    atomic_write_text(relevance_path, json.dumps(relevance, indent=1))
    atomic_write_text(review_path, json.dumps(review, indent=1, ensure_ascii=False))
    summary["review_candidates"] = sum(len(v) for v in review.values())
    atomic_write_text(errors_path, json.dumps(known, indent=1))
    summary["zero_row_files"] = summary["zero_row_files"][:20]
    log(f"parsed={summary['parsed']} rows={summary['rows']} errors={len(summary['errors'])}",
        kind="parse", files=summary["parsed"], rows=summary["rows"], errors=len(summary["errors"]))
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
