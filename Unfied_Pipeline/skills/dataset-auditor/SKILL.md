---
name: dataset-auditor
description: Cross-checking, deduplication and confidence-scoring rules for the NSE delisted master dataset.
---

# Dataset Auditor

## Commands
```bash
.venv/bin/python scripts/normalize_and_merge.py   # rebuild master + priority audit from parsed/*.jsonl
.venv/bin/python scripts/status_report.py         # counts by year × type × confidence
.venv/bin/python scripts/status_report.py --year 2017 --needs-review   # list rows to check
.venv/bin/python scripts/audit_dataset.py          # data-quality errors/warnings → data/metadata/dataset_audit.json
```
Audit errors (`duplicate_record_id`, `malformed_isin`, `non_listing_source`, `missing_artifact`, …) mean a parser
rule is wrong. Don't fix it — report it for the user:
`.venv/bin/python scripts/report_issue.py --kind audit_error --summary "<check>: <count> rows" --evidence <record_id>`.

## Outputs
- `data/processed/nse_delisted_master.csv` / `.jsonl` — all years, schema = root `nse_delisted_master.csv`.
- `data/processed/priority_years_audit.csv` — rows where `is_priority_year = true`, plus `source_count` and `all_source_urls`.

## Deduplication
- Key: `isin` if present, else normalized company name (uppercase, drop `LTD/LIMITED/PVT/PRIVATE/THE/.,&`).
- Same key + same `delisting_type` + effective dates within 30 days (or one missing) → same event, merged.
- Merge keeps the row with the best confidence (`VERIFIED_PRIMARY` > `CORROBORATED_SECONDARY` > `NEEDS_REVIEW`), then fills its empty fields from the others.
- `record_id` = first 16 hex of sha256(`key|effective_date|delisting_type`) — deterministic, stable across re-runs.

## Confidence upgrades
- A `NEEDS_REVIEW` row confirmed by a second independent domain → `CORROBORATED_SECONDARY` (script does this automatically when `source_count ≥ 2` across distinct domains).
- Only an official NSE/SEBI/BSE document makes a row `VERIFIED_PRIMARY`. Never upgrade by hand.

## Audit checklist (do this per priority year)
1. `status_report.py --year Y` — are all delisting types represented? Gaps → new searches.
2. `--needs-review` rows: find an official source for each (search `site:nseindia.com "<company name>" delisting`).
3. Rows with `isin` empty: search the company name + `ISIN`.
4. Conflicting dates between sources: keep the official one; note the conflict in `logs/agent_run.log`.
5. Sanity: `calendar_year` within 2006–2026; ISIN 12 chars; symbol uppercase.
