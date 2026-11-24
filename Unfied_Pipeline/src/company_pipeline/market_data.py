"""Canonical tabular schema and CSV export for market data records."""

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


MARKET_DATA_COLUMNS = (
    "isin",
    "nse_symbol",
    "series",
    "sector",
    "industry",
    "market_cap_cr",
    "free_float_pct",
    "is_fno",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "total_traded_qty",
    "turnover_cr",
    "deliverable_qty",
    "delivery_pct",
    "no_of_trades",
    "bulk_block_deal_flag",
    "futures_oi",
    "oi_change_pct",
    "quarter_ending",
    "sales_growth_yoy",
    "sales_growth_qoq",
    "ebitda_margin_pct",
    "pat_growth_yoy",
    "eps_ttm",
    "roce_pct",
    "roe_pct",
    "debt_to_equity",
    "interest_coverage",
    "cash_flow_from_ops",
    "pe_ratio",
    "sector_median_pe",
    "ev_ebitda",
    "peg_ratio",
    "promoter_holding_pct",
    "promoter_pledge_pct",
    "fii_holding_chg_qoq",
    "dii_holding_chg_qoq",
    "retail_holding_chg_qoq",
    "surveillance_stage",
    "broadcast_timestamp",
    "announcement_category",
    "announcement_headline",
    "announcement_body_md",
    "concall_commentary_md",
    "concall_guidance_md",
    "sast_insider_activity",
    "bulk_block_deal_participants",
)


def normalize_market_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return one record in canonical column order, filling absent fields with null."""
    unknown_columns = set(record) - set(MARKET_DATA_COLUMNS)
    if unknown_columns:
        names = ", ".join(sorted(unknown_columns))
        raise ValueError(f"Unknown market data column(s): {names}")
    return {column: record.get(column) for column in MARKET_DATA_COLUMNS}


def filter_market_records(
    records: Sequence[Mapping[str, Any]], filters: Sequence[str]
) -> list[dict[str, Any]]:
    """Apply case-insensitive exact FIELD=VALUE filters; multiple filters are ANDed."""
    criteria: dict[str, str] = {}
    for expression in filters:
        field, separator, value = expression.partition("=")
        field = field.strip()
        if not separator or field not in MARKET_DATA_COLUMNS:
            raise ValueError(
                f"Invalid filter {expression!r}; use FIELD=VALUE with a known column."
            )
        criteria[field] = value.strip().casefold()

    normalized_records = [normalize_market_record(record) for record in records]
    return [
        record
        for record in normalized_records
        if all(
            _filter_value(record[field]).casefold() == expected
            for field, expected in criteria.items()
        )
    ]


def write_market_csv(
    output_path: str | Path, records: Sequence[Mapping[str, Any]]
) -> None:
    """Write market records with the canonical headers and column order."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=MARKET_DATA_COLUMNS)
        writer.writeheader()
        for record in records:
            normalized = normalize_market_record(record)
            writer.writerow(
                {column: _csv_value(value) for column, value in normalized.items()}
            )


def _csv_value(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _filter_value(value: Any) -> str:
    return _csv_value(value).__str__()
