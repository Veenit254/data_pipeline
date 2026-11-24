"""Command-line entry points for the unified NSE pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import importlib
import json
from pathlib import Path
from typing import Any, cast

from company_pipeline.api import (
    MarketDocumentApiClient,
    extract_market_records_from_pdf,
)
from company_pipeline.filtering import CompanyFilter, filter_companies
from company_pipeline.markdown import pages_to_markdown
from company_pipeline.market_data import filter_market_records, write_market_csv
from company_pipeline.models import CompanyRecord
from company_pipeline.nse_bridge import sync_archive_to_store
from company_pipeline.pdf import PdfProcessor
from company_pipeline.storage import (
    CompanyStore,
    backup_database,
    restore_database,
)
from company_pipeline.z_factor import (
    DEFAULT_TARGET_BRACKET_CR,
    TargetBracketCalibration,
    recompute_store_z_factors,
    summarize_historical_brackets,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="company-pipeline")
    parser.add_argument(
        "--database",
        default="data/nse_unified.sqlite3",
        help="SQLite database path.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    pdf_command = commands.add_parser("process-pdf", help="Convert a PDF to Markdown.")
    pdf_command.add_argument("input", type=Path)
    pdf_command.add_argument("--output", type=Path, required=True)

    extract_command = commands.add_parser(
        "extract-market-pdf",
        help="Extract market-data fields from a PDF using a configured API client.",
    )
    extract_command.add_argument("input", type=Path)
    extract_command.add_argument("--output", type=Path, required=True)
    extract_command.add_argument(
        "--client",
        required=True,
        metavar="MODULE:CLASS",
        help="Import path for your configured MarketDocumentApiClient implementation.",
    )

    ingest_command = commands.add_parser(
        "ingest-json", help="Load company records from a JSON array."
    )
    ingest_command.add_argument("input", type=Path)

    market_command = commands.add_parser(
        "ingest-market-json",
        help="Ingest market records and export the canonical filtered CSV.",
    )
    market_command.add_argument("input", type=Path)
    market_command.add_argument("--output", type=Path, required=True)
    market_command.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="Keep matching rows; repeat to require multiple exact matches.",
    )

    list_command = commands.add_parser("list", help="List stored companies.")
    list_command.add_argument("--country")
    list_command.add_argument("--industry")
    list_command.add_argument("--name-contains")

    sync_command = commands.add_parser(
        "sync-nse-archive",
        help="Import NSE active listings, delistings, and leads into the unified SQLite DB.",
    )
    sync_command.add_argument("--repo-root", type=Path, default=Path("."))
    sync_command.add_argument("--include-bse-only", action="store_true")

    universe_command = commands.add_parser(
        "universe-as-of",
        help="Query the survivorship-bias-free active NSE universe on a historical date.",
    )
    universe_command.add_argument("date", metavar="YYYY-MM-DD")
    universe_command.add_argument("--status")
    universe_command.add_argument("--limit", type=int, default=10)

    z_command = commands.add_parser(
        "compute-z-factors",
        help="Compute point-in-time cross-sectional Z-factors and target-bracket flags for daily_market_data.",
    )
    z_command.add_argument("--robust", action="store_true")
    z_command.add_argument("--anchor-date", metavar="YYYY-MM-DD")
    z_command.add_argument(
        "--target-min-cr",
        type=float,
        default=DEFAULT_TARGET_BRACKET_CR[0],
        help="Lower bound of target bracket in modern Cr (default 3500.0).",
    )
    z_command.add_argument(
        "--target-max-cr",
        type=float,
        default=DEFAULT_TARGET_BRACKET_CR[1],
        help="Upper bound of target bracket in modern Cr (default 12500.0).",
    )
    z_command.add_argument(
        "--calibration-mode",
        choices=("nse_prior", "reference_window", "fixed_z"),
        default=None,
        help="Calibration mode for mapping modern bracket bounds to Z-scores.",
    )
    z_command.add_argument("--calibrate-from", metavar="YYYY-MM-DD")
    z_command.add_argument("--calibrate-to", metavar="YYYY-MM-DD")
    z_command.add_argument("--reference-years", type=int, default=3)
    z_command.add_argument("--z-min", type=float)
    z_command.add_argument("--z-max", type=float)
    z_command.add_argument("--trailing-days", type=int, default=1)
    z_command.add_argument("--z-buffer", type=float, default=0.0)
    z_command.add_argument("--include-sme-in-stats", action="store_true")

    bracket_command = commands.add_parser(
        "target-bracket",
        help="Query Z-standardized target-bracket cohorts or historical rupee-equivalent schedule.",
    )
    bracket_command.add_argument("--date", metavar="YYYY-MM-DD")
    bracket_command.add_argument("--start-date", metavar="YYYY-MM-DD")
    bracket_command.add_argument("--end-date", metavar="YYYY-MM-DD")
    bracket_command.add_argument("--schedule", action="store_true")
    bracket_command.add_argument("--output", type=Path)
    bracket_command.add_argument("--limit", type=int, default=20)

    backup_command = commands.add_parser(
        "db-backup",
        help="Create a validated online backup of the unified SQLite database.",
    )
    backup_command.add_argument(
        "--output",
        type=Path,
        help="Backup file path. Defaults to data/backups/nse_unified-<timestamp>.sqlite3.",
    )

    restore_command = commands.add_parser(
        "db-restore",
        help="Restore the unified SQLite database from a validated backup.",
    )
    restore_command.add_argument("backup", type=Path)
    restore_command.add_argument(
        "--no-pre-restore-backup",
        action="store_true",
        help="Skip creating a safety backup of the current database.",
    )

    args = parser.parse_args()
    if args.command == "process-pdf":
        processor = PdfProcessor()
        pages = processor.extract_pages(args.input)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            pages_to_markdown(args.input.stem, pages), encoding="utf-8"
        )
        print(f"Wrote {args.output}")
        return

    if args.command == "extract-market-pdf":
        client = cast(MarketDocumentApiClient, _load_market_document_client(args.client))
        records = extract_market_records_from_pdf(args.input, client)
        write_market_csv(args.output, records)
        print(f"Extracted {len(records)} record(s) to {args.output}.")
        return

    if args.command == "db-backup":
        destination = backup_database(args.database, args.output)
        print(json.dumps({"backup": str(destination)}, indent=2))
        return

    if args.command == "db-restore":
        result = restore_database(
            args.database,
            args.backup,
            create_pre_restore_backup=not args.no_pre_restore_backup,
        )
        print(json.dumps(result, indent=2))
        return

    with CompanyStore(args.database) as store:
        if args.command == "ingest-market-json":
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Market data input JSON must be an array of objects.")
            inserted = duplicates = 0
            for item in payload:
                if not isinstance(item, dict):
                    raise ValueError("Each market data item must be an object.")
                res = store.ingest_unified_market_record(item)
                if res["market_record_inserted"]:
                    inserted += 1
                else:
                    duplicates += 1
            filtered_records = filter_market_records(
                store.list_market_records(), args.where
            )
            write_market_csv(args.output, filtered_records)
            print(
                f"Inserted {inserted}; skipped {duplicates} duplicate(s); "
                f"exported {len(filtered_records)} row(s) to {args.output}."
            )
        elif args.command == "ingest-json":
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Input JSON must be an array of company objects.")
            inserted = updated = 0
            for item in payload:
                if not isinstance(item, dict):
                    raise ValueError("Each item in the JSON array must be an object.")
                if store.upsert(CompanyRecord.from_mapping(item)):
                    inserted += 1
                else:
                    updated += 1
            print(f"Inserted {inserted}; updated {updated}.")
        elif args.command == "sync-nse-archive":
            summary = sync_archive_to_store(
                args.repo_root,
                store,
                include_bse_only=args.include_bse_only,
            )
            print(json.dumps(summary, indent=2))
        elif args.command == "universe-as-of":
            entities = store.list_nse_entities(
                status=args.status,
                as_of_date=args.date,
            )
            by_status = dict(Counter(item["listing_status"] for item in entities))
            sample = [
                {
                    "nse_symbol": item["nse_symbol"],
                    "isin": item["isin"],
                    "company_name": item["company_name"],
                    "listing_status": item["listing_status"],
                    "listing_date": item["listing_date"],
                    "subsequent_delisting_date": item["subsequent_delisting_date"],
                }
                for item in entities[: max(0, args.limit)]
            ]
            print(
                json.dumps(
                    {
                        "as_of_date": args.date,
                        "count": len(entities),
                        "by_status": by_status,
                        "sample": sample,
                    },
                    indent=2,
                )
            )
        elif args.command == "compute-z-factors":
            mode = args.calibration_mode
            if mode is None:
                if args.z_min is not None and args.z_max is not None:
                    mode = "fixed_z"
                elif args.calibrate_from is not None or args.calibrate_to is not None:
                    mode = "reference_window"
                else:
                    mode = "nse_prior"
            summary = recompute_store_z_factors(
                store,
                robust=args.robust,
                anchor_date=args.anchor_date,
                target_bracket_cr=(args.target_min_cr, args.target_max_cr),
                calibration_mode=mode,
                reference_start=args.calibrate_from,
                reference_end=args.calibrate_to,
                reference_years=args.reference_years,
                z_min=args.z_min,
                z_max=args.z_max,
                mainboard_only_stats=not args.include_sme_in_stats,
                trailing_days=args.trailing_days,
                z_buffer=args.z_buffer,
            )
            print(json.dumps(summary, indent=2))
        elif args.command == "target-bracket":
            saved_cal = store.latest_z_calibration()
            calibration = (
                _calibration_from_row(saved_cal)
                if saved_cal is not None
                else TargetBracketCalibration.default_nse()
            )
            if args.schedule:
                raw_rows = store.list_daily_market_rows_for_z(pit_universe_only=True)
                schedule = summarize_historical_brackets(
                    raw_rows,
                    robust=calibration.robust,
                    target_bracket_cr=(
                        calibration.target_min_cr,
                        calibration.target_max_cr,
                    ),
                    calibration=calibration,
                    mainboard_only_stats=calibration.mainboard_only_stats,
                )
                if args.date is not None:
                    schedule = [r for r in schedule if r["trade_date"] == args.date]
                if args.start_date is not None:
                    schedule = [r for r in schedule if r["trade_date"] >= args.start_date]
                if args.end_date is not None:
                    schedule = [r for r in schedule if r["trade_date"] <= args.end_date]
                if args.output is not None:
                    _write_dicts_csv(args.output, schedule)
                print(
                    json.dumps(
                        {
                            "calibration": calibration.to_dict(),
                            "dates": len(schedule),
                            "schedule": schedule[: max(0, args.limit)],
                        },
                        indent=2,
                    )
                )
            else:
                matches = store.list_target_bracket_stocks(
                    trade_date=args.date,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    survivorship_free=True,
                )
                export_rows = [
                    {k: v for k, v in row.items() if k != "raw_json"} for row in matches
                ]
                if args.output is not None:
                    _write_dicts_csv(args.output, export_rows)
                sample = [
                    {
                        "trade_date": row["trade_date"],
                        "nse_symbol": row["nse_symbol"],
                        "isin": row["isin"],
                        "company_name": row["company_name"],
                        "listing_status": row["listing_status"],
                        "latest_delisting_date": row["latest_delisting_date"],
                        "market_cap_cr": row["market_cap_cr"],
                        "z_factor_mcap": row["z_factor_mcap"],
                        "mcap_percentile": row["mcap_percentile"],
                        "mcap_rank": row["mcap_rank"],
                        "equivalent_mcap_cr": row["equivalent_mcap_cr"],
                        "implied_bracket_min_cr": row["implied_bracket_min_cr"],
                        "implied_bracket_max_cr": row["implied_bracket_max_cr"],
                    }
                    for row in matches[: max(0, args.limit)]
                ]
                print(
                    json.dumps(
                        {
                            "count": len(matches),
                            "calibration": saved_cal or calibration.to_dict(),
                            "sample": sample,
                        },
                        indent=2,
                    )
                )
        else:
            companies = filter_companies(
                store.list_companies(),
                CompanyFilter(
                    country=args.country,
                    industry=args.industry,
                    name_contains=args.name_contains,
                ),
            )
            print(json.dumps([company.__dict__ for company in companies], indent=2))


def _calibration_from_row(row: dict[str, Any]) -> TargetBracketCalibration:
    return TargetBracketCalibration(
        target_min_cr=float(row["target_min_cr"]),
        target_max_cr=float(row["target_max_cr"]),
        ref_log_center=float(row["ref_log_center"]),
        ref_log_scale=float(row["ref_log_scale"]),
        z_min=float(row["z_min"]),
        z_max=float(row["z_max"]),
        source=str(row["source"]),
        reference_start=row.get("reference_start"),
        reference_end=row.get("reference_end"),
        reference_dates_count=int(row.get("reference_dates_count") or 0),
        robust=bool(row.get("robust")),
        mainboard_only_stats=bool(row.get("mainboard_only_stats", True)),
    )


def _write_dicts_csv(output_path: Path, rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        if fieldnames:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def _load_market_document_client(import_path: str) -> object:
    module_name, separator, class_name = import_path.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError("Client must use MODULE:CLASS format.")
    client_class = getattr(importlib.import_module(module_name), class_name)
    client = client_class()
    if not callable(getattr(client, "extract_market_records", None)):
        raise TypeError(
            "Configured client must implement extract_market_records(markdown_text, fields)."
        )
    return client


if __name__ == "__main__":
    main()
