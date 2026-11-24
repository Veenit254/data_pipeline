---
name: nse-document-parser
description: Rules for extracting ISIN, symbol, order date, effective date, delisting type and regime from NSE/SEBI/BSE circulars, PDFs and XLSX sheets.
---

# NSE Document Parser

## Command
```bash
.venv/bin/python scripts/parse_nse_records.py            # parse new artifacts in data/raw/
.venv/bin/python scripts/parse_nse_records.py --force    # reparse everything
.venv/bin/python scripts/parse_nse_records.py --file data/raw/priority_2017/<file>
```
Output: `data/processed/parsed/<artifact>.jsonl` (one candidate row per line) + a JSON summary.
Source URLs come from `logs/fetch_manifest.jsonl` automatically.

## How extraction works
- **XLSX / CSV / HTML tables**: header matching. A column is mapped if its header contains:
  - name: `company`, `name of`, `security name`, `scrip name`
  - symbol: `symbol`, `scrip code`, `ticker`
  - isin: `isin`
  - dates: `delist` + `date` → effective; `order` / `notice` / `approval` + `date` → order date; plain `date` → effective
  - price: `exit price`, `fair value`, `offer price`, `price`
- **PDF / plain HTML text**: line scan. A line with an ISIN is a candidate row; the company name is the text before the ISIN, the symbol is an ALL-CAPS token next to it.

## Field rules
| Field | Rule |
|---|---|
| isin | Regex `\bIN[A-Z0-9]{9}[0-9]\b` (equities start `INE`). Uppercase. |
| nse_symbol | Uppercase, trimmed, `UNKNOWN` if absent. |
| order_date / effective_delisting_date | Normalize to `YYYY-MM-DD`; Indian order is day-first (`05-06-2017` = 5 June). |
| delisting_type | Keyword map (document text or row text): `compulsor` → COMPULSORY; `exit offer`, `reverse book`, `voluntar` + price → VOLUNTARY_EXIT_OFFER; `voluntar` without exit offer → VOLUNTARY_NO_EXIT; `amalgamat`, `merger`, `scheme of arrangement` → AMALGAMATION_MERGER; `nclt`, `insolvency`, `resolution plan`, `ibc` → IBC_NCLT; else OTHER. |
| regulatory_regime | By date (normalize script derives it): < 2009-06-10 → SEBI_2003_Guidelines; < 2021-06-10 → SEBI_2009_Regs; < 2024-09-25 → SEBI_2021_Regs; else SEBI_2024_Amendment. |
| confidence_status | Set by domain: nseindia/sebi/bseindia → VERIFIED_PRIMARY; known news/legal → CORROBORATED_SECONDARY; else NEEDS_REVIEW. Rows without an ISIN **and** without a date are always NEEDS_REVIEW. |

## When parsing misses rows
1. Check the summary: `rows: 0` with `tables_seen > 0` means headers didn't match — add the header keyword to `HEADER_MAP` in the script.
2. Scanned (image) PDFs produce no text — the file is listed under `no_text`. Flag for the user; do not OCR yourself.
3. Never hand-type rows into processed files.
