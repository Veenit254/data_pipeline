# NSE Quantitative & Qualitative Research Pipeline

An end-to-end local-first data engineering pipeline for Indian equities on the National Stock Exchange (NSE):

1. **Historical Delisting & Filing Collector (`scripts/`)** — Multi-provider discovery (`SearXNG`, `Serper`, `Brave`), rate-limited fetching, PDF/XLSX/HTML parsing, Tesseract OCR, and Union-Find event reconciliation covering NSE delistings from 2006–2026 (plus pre-2006 exits and proposed delisting leads).
2. **Quantitative, Qualitative & Point-in-Time Standardization Engine (`src/company_pipeline/`)** — Relational SQLite persistence (`data/nse_unified.sqlite3`), 50-column canonical market schema, PDF-to-Markdown + image-region OCR, active/delisted universe bridge (`nse_bridge.py`), and lookahead-bias-free Z-factor standardization (`z_factor.py`).

---

## Architecture

```text
  [1] Discovery & Delisting Collection (scripts/collect.py + parse_nse_records.py + normalize_and_merge.py)
        ├── Active reference masters: data/reference/nse_equity_listed.csv, nse_sme_listed.csv
        └── Audited delisting outputs: data/processed/nse_delisted_master.csv (409 rows),
            out_of_scope_delistings.csv (34 rows), proposed_delisting_leads.csv (14 rows)
                    │
                    ▼  src/company_pipeline/nse_bridge.py (sync-nse-archive)
  [2] Point-in-Time SQLite Database (data/nse_unified.sqlite3)
        ├── nse_entities           (ACTIVE / PROPOSED_DELISTING / DELISTED / DELISTED_PRE_2006, re-listing aware)
        ├── delisting_events       (order_date, effective_delisting_date, SEBI regime, exit value, provenance)
        ├── daily_market_data      (OHLCV, delivery, F&O, valuation, Z-factor brackets)
        ├── quarterly_fundamentals (keyed by quarter_ending + broadcast_timestamp for zero lookahead)
        └── qualitative_filings    (announcements, concalls, SAST, surveillance, delisting orders)
                    │
                    ▼  src/company_pipeline/z_factor.py (compute-z-factors)
  [3] Cross-Sectional Z-Factor Standardization (Zero Lookahead Bias)
        └── Point-in-time Z-score, percentile, size bracket (MEGA..MICRO), and causal deflated market cap
```

---

## Quickstart

### Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[ocr,dev]"
```

Or use the guided installer for the full collection stack (SearXNG Docker, Cline, Tesseract):

```bash
./install.sh
```

### Sync the NSE Active & Delisted Universe into SQLite

```bash
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 sync-nse-archive --repo-root .
```

### Query the Survivorship-Bias-Free Universe on Any Historical Date

```bash
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 universe-as-of 2007-06-01 --limit 10
```

### Ingest Market Records & Standardize the `₹3,500 Cr – ₹12,500 Cr` Target Bracket

```bash
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 ingest-market-json market-data.json --output filtered.csv --where sector=Technology

# Default zero-lookahead calibration using modern NSE mainboard distribution prior (₹3,500 Cr – ₹12,500 Cr -> Z in [+0.535, +1.055])
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 compute-z-factors --target-min-cr 3500 --target-max-cr 12500

# Or calibrate from an empirical recent 2–3 year NSE window and project backward without lookahead
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 compute-z-factors --calibration-mode reference_window --calibrate-from 2023-11-01 --calibrate-to 2026-11-01

# Inspect historical equivalent rupee brackets or query survivorship-bias-free bracket constituents
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 target-bracket --schedule
PYTHONPATH=src python3 -m company_pipeline.cli --database data/nse_unified.sqlite3 target-bracket --date 2010-06-01
```

### Extract Text / OCR from PDF Filings

```bash
PYTHONPATH=src python3 -m company_pipeline.cli process-pdf filing.pdf --output filing.md
```

### Run Delisting Collection & Health Checks

```bash
.venv/bin/python scripts/collect.py --dry-run
.venv/bin/python scripts/status_report.py
scripts/check.sh --offline
```

### Back Up and Restore the SQLite Database

Run these commands from the project root after installing the package:

```bash
# Create a validated backup
python -m company_pipeline.cli --database data/nse_unified.sqlite3 db-backup

# Specify the backup file
python -m company_pipeline.cli --database data/nse_unified.sqlite3 db-backup --output data/backups/nse_unified.sqlite3

# Restore, with an automatic safety backup of the current database
python -m company_pipeline.cli --database data/nse_unified.sqlite3 db-restore data/backups/nse_unified.sqlite3
```

To skip creating the automatic pre-restore backup, add `--no-pre-restore-backup` to the restore command. Only do this if you have another verified copy of the current database.

```bash
python -m company_pipeline.cli --database data/nse_unified.sqlite3 db-restore data/backups/nse_unified.sqlite3 --no-pre-restore-backup
```

---

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p "test_*pipeline*.py"
.venv/bin/pytest -q tests
```

---

## External Data Adapters

The core pipeline, relational database, universe reconstruction, and Z-factor math are ready to use. Two adapter interfaces in [`src/company_pipeline/api.py`](src/company_pipeline/api.py) connect external feeds:

1. **Quantitative Broker / Bhavcopy Adapter (`MarketDataApiClient`):** Implement `fetch_market_records()` for your data broker API or NSE historical bhavcopy downloader and feed rows into `CompanyStore.ingest_unified_market_record()`.
2. **Qualitative Document Extractor (`MarketDocumentApiClient`):** Implement `extract_market_records(markdown_text, fields)` to map PDF-extracted Markdown (`process-pdf` / `extract-market-pdf`) into the 50-column schema in [`src/company_pipeline/market_data.py`](src/company_pipeline/market_data.py).
