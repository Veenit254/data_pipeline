"""Sync NSE active listings, delisting archives, and leads into SQLite."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from company_pipeline.storage import CompanyStore

_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y")


def _parse_date(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _read_clean_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            cleaned: dict[str, str] = {}
            for key, val in raw_row.items():
                if key is None:
                    continue
                clean_key = key.strip()
                if not clean_key:
                    continue
                cleaned[clean_key] = (val or "").strip()
            rows.append(cleaned)
    return rows


def sync_archive_to_store(
    repo_root: str | Path,
    store: CompanyStore,
    include_bse_only: bool = False,
) -> dict[str, int]:
    """Import NSE active listings, delistings, pre-2006 exits, and leads into SQLite."""
    root = Path(repo_root)
    counts = {
        "active_equity_rows": 0,
        "active_sme_rows": 0,
        "delisted_2006_2026_rows": 0,
        "delisted_pre_2006_rows": 0,
        "proposed_lead_rows": 0,
        "bse_only_rows": 0,
    }

    # 1. Active mainboard NSE equity listings
    equity_rows = _read_clean_csv(root / "data" / "reference" / "nse_equity_listed.csv")
    for row in equity_rows:
        company_name = row.get("NAME OF COMPANY", "")
        if not company_name:
            continue
        store.upsert_nse_entity(
            {
                "isin": row.get("ISIN NUMBER"),
                "nse_symbol": row.get("SYMBOL"),
                "company_name": company_name,
                "series": row.get("SERIES"),
                "listing_date": _parse_date(row.get("DATE OF LISTING")),
                "listing_status": "ACTIVE",
                "paid_up_value": _parse_float(row.get("PAID UP VALUE")),
                "face_value": _parse_float(row.get("FACE VALUE")),
            }
        )
        counts["active_equity_rows"] += 1

    # 2. Active NSE SME listings
    sme_rows = _read_clean_csv(root / "data" / "reference" / "nse_sme_listed.csv")
    for row in sme_rows:
        company_name = row.get("NAME_OF_COMPANY", "")
        if not company_name:
            continue
        store.upsert_nse_entity(
            {
                "isin": row.get("ISIN_NUMBER"),
                "nse_symbol": row.get("SYMBOL"),
                "company_name": company_name,
                "series": row.get("SERIES"),
                "listing_date": _parse_date(row.get("DATE_OF_LISTING")),
                "listing_status": "ACTIVE",
                "paid_up_value": _parse_float(row.get("PAID_UP_VALUE")),
                "face_value": _parse_float(row.get("FACE_VALUE")),
            }
        )
        counts["active_sme_rows"] += 1

    # 3. Verified NSE delistings (2006-2026)
    delisted_rows = _read_clean_csv(root / "data" / "processed" / "nse_delisted_master.csv")
    for row in delisted_rows:
        _ingest_delisting_row(
            store, row, listing_status="DELISTED", scope="NSE_2006_2026"
        )
        counts["delisted_2006_2026_rows"] += 1

    # 4. Pre-2006 out-of-scope NSE delistings
    pre_2006_rows = _read_clean_csv(
        root / "data" / "processed" / "out_of_scope_delistings.csv"
    )
    for row in pre_2006_rows:
        _ingest_delisting_row(
            store, row, listing_status="DELISTED_PRE_2006", scope="PRE_2006"
        )
        counts["delisted_pre_2006_rows"] += 1

    # 5. Proposed delisting leads
    lead_rows = _read_clean_csv(
        root / "data" / "processed" / "proposed_delisting_leads.csv"
    )
    for row in lead_rows:
        company_name = row.get("company_name", "")
        if not company_name:
            continue
        nse_symbol = row.get("nse_symbol", "")
        isin = row.get("isin", "")
        source_url = row.get("source_url", "")
        local_artifact_path = row.get("local_artifact_path", "")

        entity_id, _ = store.upsert_nse_entity(
            {
                "isin": isin or None,
                "nse_symbol": nse_symbol or None,
                "company_name": company_name,
                "listing_status": "PROPOSED_DELISTING",
                "source_url": source_url or None,
            }
        )
        lead_digest = hashlib.sha256(
            f"{company_name}|{nse_symbol}|{source_url}".encode("utf-8")
        ).hexdigest()[:12]
        store.upsert_delisting_event(
            entity_id,
            {
                "record_id": f"lead-{lead_digest}",
                "scope": "PROPOSED_LEAD",
                "company_name": company_name,
                "nse_symbol": nse_symbol or None,
                "isin": isin or None,
                "source_url": source_url or None,
                "local_artifact_path": local_artifact_path or None,
                "confidence_status": "VERIFIED_PRIMARY",
            },
        )
        store.upsert_qualitative_filing(
            {
                "entity_id": entity_id,
                "isin": isin or None,
                "nse_symbol": nse_symbol or None,
                "surveillance_stage": "PROPOSED_DELISTING",
                "announcement_category": "PROPOSED_DELISTING",
                "announcement_headline": f"{company_name} (PROPOSED_DELISTING)",
                "source_url": source_url or None,
                "local_artifact_path": local_artifact_path or None,
                "confidence_status": "VERIFIED_PRIMARY",
            }
        )
        counts["proposed_lead_rows"] += 1

    # 6. Optional BSE-only delistings
    if include_bse_only:
        bse_rows = _read_clean_csv(
            root / "data" / "processed" / "bse_only_delistings.csv"
        )
        for row in bse_rows:
            _ingest_delisting_row(
                store, row, listing_status="BSE_ONLY", scope="BSE_ONLY"
            )
            counts["bse_only_rows"] += 1

    entities_total = len(store.list_nse_entities(include_bse_only=include_bse_only))
    delisting_events_total = len(store.list_delisting_events())
    qualitative_filings_total = len(store.list_qualitative_filings())

    return {
        **counts,
        "entities_total": entities_total,
        "delisting_events_total": delisting_events_total,
        "qualitative_filings_total": qualitative_filings_total,
    }


def _ingest_delisting_row(
    store: CompanyStore,
    row: Mapping[str, str],
    *,
    listing_status: str,
    scope: str,
) -> None:
    company_name = row.get("company_name", "")
    record_id = row.get("record_id", "")
    if not company_name or not record_id:
        return

    order_date = _parse_date(row.get("order_date"))
    effective_date = _parse_date(row.get("effective_delisting_date"))
    delisting_type = (row.get("delisting_type") or "").strip().upper() or "OTHER"
    isin = row.get("isin") or None
    nse_symbol = row.get("nse_symbol") or None
    source_url = row.get("source_url") or None
    local_artifact_path = row.get("local_artifact_path") or None
    confidence_status = row.get("confidence_status") or None

    entity_id, _ = store.upsert_nse_entity(
        {
            "isin": isin,
            "nse_symbol": nse_symbol,
            "company_name": company_name,
            "listing_status": listing_status,
            "latest_delisting_date": effective_date or order_date,
            "latest_delisting_type": delisting_type,
            "source_url": source_url,
        }
    )
    store.upsert_delisting_event(
        entity_id,
        {
            "record_id": record_id,
            "scope": scope,
            "company_name": company_name,
            "nse_symbol": nse_symbol,
            "isin": isin,
            "delisting_type": delisting_type,
            "order_date": order_date,
            "effective_delisting_date": effective_date,
            "calendar_year": _parse_int(row.get("calendar_year")),
            "financial_year": row.get("financial_year") or None,
            "is_priority_year": _parse_bool(row.get("is_priority_year")),
            "regulatory_regime": row.get("regulatory_regime") or None,
            "exit_or_fair_value_inr": _parse_float(row.get("exit_or_fair_value_inr")),
            "source_url": source_url,
            "local_artifact_path": local_artifact_path,
            "confidence_status": confidence_status,
            "source_count": _parse_int(row.get("source_count")),
            "all_source_urls": row.get("all_source_urls") or None,
            "all_artifact_paths": row.get("all_artifact_paths") or None,
            "source_conflicts": row.get("source_conflicts") or None,
            "review_flags": row.get("review_flags") or None,
            "date_source": row.get("date_source") or None,
        },
    )
    store.upsert_qualitative_filing(
        {
            "entity_id": entity_id,
            "isin": isin,
            "nse_symbol": nse_symbol,
            "broadcast_timestamp": order_date or effective_date,
            "announcement_category": f"DELISTING_{delisting_type}",
            "announcement_headline": f"{company_name} ({delisting_type})",
            "source_url": source_url,
            "local_artifact_path": local_artifact_path,
            "confidence_status": confidence_status,
        }
    )
