"""SQLite persistence for company, NSE lifecycle, market, and qualitative records."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from company_pipeline.market_data import normalize_market_record
from company_pipeline.models import CompanyRecord

_NAME_STOP_RE = re.compile(
    r"\b(LTD|LIMITED|PVT|PRIVATE|THE|CO|COMPANY|CORPN?|CORPORATION|OF)\b"
)


def _validate_database(database_path: Path) -> None:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {database_path}")
    connection = sqlite3.connect(str(database_path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            detail = integrity[0] if integrity is not None else "no result"
            raise sqlite3.DatabaseError(
                f"SQLite integrity check failed for {database_path}: {detail}"
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"Foreign-key check failed for {database_path}: {len(violations)} violation(s)"
            )
    finally:
        connection.close()


def backup_database(database_path: str | Path, backup_path: str | Path | None = None) -> Path:
    source_path = Path(database_path)
    _validate_database(source_path)

    if backup_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = (
            source_path.parent / "backups"
            / f"{source_path.stem}-{timestamp}{source_path.suffix}"
        )
    destination = Path(backup_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(str(source_path))
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    target = sqlite3.connect(str(temporary))
    try:
        source.backup(target)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target.close()
        source.close()

    _validate_database(temporary)
    temporary.replace(destination)
    return destination


def restore_database(
    database_path: str | Path,
    backup_path: str | Path,
    *,
    create_pre_restore_backup: bool = True,
) -> dict[str, Any]:
    target = Path(database_path)
    source = Path(backup_path)
    _validate_database(source)

    pre_restore_backup: Path | None = None
    if create_pre_restore_backup and target.is_file():
        pre_restore_backup = backup_database(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".restore.tmp")
    if temporary.exists():
        temporary.unlink()
    source_connection = sqlite3.connect(str(source))
    destination_connection = sqlite3.connect(str(temporary))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()

    _validate_database(temporary)
    temporary.replace(target)
    return {
        "database": str(target),
        "restored_from": str(source),
        "pre_restore_backup": str(pre_restore_backup) if pre_restore_backup else None,
    }


class CompanyStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.database_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        self._migrate_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "CompanyStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Company directory & raw market record storage
    # ------------------------------------------------------------------

    def upsert(self, company: CompanyRecord) -> bool:
        """Insert or update a company; return True only for a new row."""
        normalized_name = _normalize_name(company.name)
        normalized_website = _normalize_website(company.website)
        normalized_country = _normalize_name(company.country) if company.country else None
        existing = self._find_existing(
            company, normalized_name, normalized_website, normalized_country
        )

        values = (
            company.name,
            normalized_name,
            company.website,
            normalized_website,
            company.country,
            normalized_country,
            company.industry,
            company.description,
            company.source,
            company.source_id,
            json.dumps(dict(company.attributes), sort_keys=True),
        )

        with self._connection:
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO companies (
                        name, normalized_name, website, normalized_website, country,
                        normalized_country, industry, description, source, source_id,
                        attributes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                return True

            self._connection.execute(
                """
                UPDATE companies SET
                    name = ?, normalized_name = ?, website = ?, normalized_website = ?,
                    country = ?, normalized_country = ?, industry = ?, description = ?,
                    source = ?, source_id = ?, attributes_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (*values, existing["id"]),
            )
        return False

    def list_companies(self) -> list[CompanyRecord]:
        rows = self._connection.execute(
            "SELECT * FROM companies ORDER BY normalized_name, id"
        ).fetchall()
        return [_row_to_company(row) for row in rows]

    def add_market_record(self, record: dict[str, object]) -> bool:
        """Store a market record once by its complete normalized contents."""
        normalized = normalize_market_record(record)
        record_json = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        record_hash = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO market_records (record_hash, record_json)
                VALUES (?, ?)
                """,
                (record_hash, record_json),
            )
        return cursor.rowcount == 1

    def list_market_records(self) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT record_json FROM market_records ORDER BY id"
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    # ------------------------------------------------------------------
    # Unified NSE entities & survivorship-bias-free lifecycle tables
    # ------------------------------------------------------------------

    def upsert_nse_entity(self, entity: Mapping[str, Any]) -> tuple[int, bool]:
        """Insert or update an NSE entity; return (entity_id, inserted)."""
        company_name = _clean_str(entity.get("company_name"))
        if not company_name:
            raise ValueError("NSE entity requires a non-empty 'company_name'.")

        isin = _clean_upper(entity.get("isin"))
        nse_symbol = _clean_symbol(entity.get("nse_symbol"))
        normalized_name = _normalize_nse_entity_name(company_name)
        series = _clean_upper(entity.get("series"))
        sector = _clean_str(entity.get("sector"))
        industry = _clean_str(entity.get("industry"))
        listing_date = _clean_str(entity.get("listing_date"))
        incoming_status = _clean_upper(entity.get("listing_status")) or "ACTIVE"
        paid_up_value = _to_float(entity.get("paid_up_value"))
        face_value = _to_float(entity.get("face_value"))
        latest_delisting_date = _clean_str(entity.get("latest_delisting_date"))
        latest_delisting_type = _clean_upper(entity.get("latest_delisting_type"))
        source_url = _clean_str(entity.get("source_url"))

        existing = self._find_existing_nse_entity(isin, nse_symbol, normalized_name)

        with self._connection:
            if existing is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO nse_entities (
                        isin, nse_symbol, company_name, normalized_name, series,
                        sector, industry, listing_date, listing_status,
                        paid_up_value, face_value, latest_delisting_date,
                        latest_delisting_type, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        isin,
                        nse_symbol,
                        company_name,
                        normalized_name,
                        series,
                        sector,
                        industry,
                        listing_date,
                        incoming_status,
                        paid_up_value,
                        face_value,
                        latest_delisting_date,
                        latest_delisting_type,
                        source_url,
                    ),
                )
                return int(cursor.lastrowid), True

            merged_status = _resolve_merged_status(
                existing_status=existing["listing_status"],
                existing_listing_date=existing["listing_date"],
                existing_delisting_date=existing["latest_delisting_date"],
                incoming_status=incoming_status,
                incoming_listing_date=listing_date,
                incoming_delisting_date=latest_delisting_date,
            )

            merged_delisting_date = _max_non_empty(
                existing["latest_delisting_date"], latest_delisting_date
            )
            merged_delisting_type = (
                latest_delisting_type
                if (
                    latest_delisting_type
                    and (
                        not existing["latest_delisting_date"]
                        or (
                            latest_delisting_date
                            and latest_delisting_date >= existing["latest_delisting_date"]
                        )
                    )
                )
                else existing["latest_delisting_type"] or latest_delisting_type
            )

            # Prefer active listing symbol/name when entity is ACTIVE, else preserve existing non-empty.
            if incoming_status == "ACTIVE" and nse_symbol:
                merged_symbol = nse_symbol
            else:
                merged_symbol = existing["nse_symbol"] or nse_symbol

            merged_name = (
                company_name
                if (incoming_status == "ACTIVE" and not existing["listing_date"])
                else existing["company_name"] or company_name
            )

            self._connection.execute(
                """
                UPDATE nse_entities SET
                    isin = ?,
                    nse_symbol = ?,
                    company_name = ?,
                    normalized_name = ?,
                    series = ?,
                    sector = ?,
                    industry = ?,
                    listing_date = ?,
                    listing_status = ?,
                    paid_up_value = ?,
                    face_value = ?,
                    latest_delisting_date = ?,
                    latest_delisting_type = ?,
                    source_url = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    existing["isin"] or isin,
                    merged_symbol,
                    merged_name,
                    normalized_name or existing["normalized_name"],
                    series or existing["series"],
                    sector or existing["sector"],
                    industry or existing["industry"],
                    listing_date or existing["listing_date"],
                    merged_status,
                    paid_up_value if paid_up_value is not None else existing["paid_up_value"],
                    face_value if face_value is not None else existing["face_value"],
                    merged_delisting_date,
                    merged_delisting_type,
                    existing["source_url"] or source_url,
                    existing["id"],
                ),
            )
            return int(existing["id"]), False

    def upsert_delisting_event(self, entity_id: int, event: Mapping[str, Any]) -> bool:
        """Insert or update a delisting event/lead; return True if newly inserted."""
        record_id = _clean_str(event.get("record_id"))
        if not record_id:
            raise ValueError("Delisting event requires a non-empty 'record_id'.")
        scope = _clean_upper(event.get("scope")) or "NSE_2006_2026"
        company_name = _clean_str(event.get("company_name")) or ""

        existing = self._connection.execute(
            "SELECT id FROM delisting_events WHERE record_id = ?", (record_id,)
        ).fetchone()

        values = (
            entity_id,
            record_id,
            scope,
            company_name,
            _clean_str(event.get("nse_symbol")),
            _clean_upper(event.get("isin")),
            _clean_upper(event.get("delisting_type")),
            _clean_str(event.get("order_date")),
            _clean_str(event.get("effective_delisting_date")),
            _to_int(event.get("calendar_year")),
            _clean_str(event.get("financial_year")),
            _to_sqlite_bool(event.get("is_priority_year")),
            _clean_str(event.get("regulatory_regime")),
            _to_float(event.get("exit_or_fair_value_inr")),
            _clean_str(event.get("source_url")),
            _clean_str(event.get("local_artifact_path")),
            _clean_upper(event.get("confidence_status")),
            _to_int(event.get("source_count")),
            _clean_str(event.get("all_source_urls")),
            _clean_str(event.get("all_artifact_paths")),
            _clean_str(event.get("source_conflicts")),
            _clean_str(event.get("review_flags")),
            _clean_str(event.get("date_source")),
        )

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO delisting_events (
                    entity_id, record_id, scope, company_name, nse_symbol, isin,
                    delisting_type, order_date, effective_delisting_date,
                    calendar_year, financial_year, is_priority_year,
                    regulatory_regime, exit_or_fair_value_inr, source_url,
                    local_artifact_path, confidence_status, source_count,
                    all_source_urls, all_artifact_paths, source_conflicts,
                    review_flags, date_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    entity_id = excluded.entity_id,
                    scope = excluded.scope,
                    company_name = excluded.company_name,
                    nse_symbol = excluded.nse_symbol,
                    isin = excluded.isin,
                    delisting_type = excluded.delisting_type,
                    order_date = excluded.order_date,
                    effective_delisting_date = excluded.effective_delisting_date,
                    calendar_year = excluded.calendar_year,
                    financial_year = excluded.financial_year,
                    is_priority_year = excluded.is_priority_year,
                    regulatory_regime = excluded.regulatory_regime,
                    exit_or_fair_value_inr = excluded.exit_or_fair_value_inr,
                    source_url = excluded.source_url,
                    local_artifact_path = excluded.local_artifact_path,
                    confidence_status = excluded.confidence_status,
                    source_count = excluded.source_count,
                    all_source_urls = excluded.all_source_urls,
                    all_artifact_paths = excluded.all_artifact_paths,
                    source_conflicts = excluded.source_conflicts,
                    review_flags = excluded.review_flags,
                    date_source = excluded.date_source
                """,
                values,
            )
        return existing is None

    def list_delisting_events(
        self, scope: str | None = None, entity_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM delisting_events {where} ORDER BY COALESCE(effective_delisting_date, order_date, ''), id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_nse_entities(
        self,
        status: str | None = None,
        as_of_date: str | None = None,
        include_bse_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List NSE entities, optionally reconstructed point-in-time at as_of_date."""
        rows = self._connection.execute(
            "SELECT * FROM nse_entities ORDER BY normalized_name, id"
        ).fetchall()

        if as_of_date is None:
            out: list[dict[str, Any]] = []
            for row in rows:
                if not include_bse_only and row["listing_status"] == "BSE_ONLY":
                    continue
                if status is not None and row["listing_status"] != status:
                    continue
                out.append(dict(row))
            return out

        events_by_entity: dict[int, list[str]] = {}
        ev_rows = self._connection.execute(
            """
            SELECT entity_id, COALESCE(effective_delisting_date, order_date) AS ev_date
            FROM delisting_events
            WHERE scope IN ('NSE_2006_2026', 'PRE_2006')
            """
        ).fetchall()
        for ev in ev_rows:
            if ev["ev_date"]:
                events_by_entity.setdefault(ev["entity_id"], []).append(ev["ev_date"])
        for dates in events_by_entity.values():
            dates.sort()

        results: list[dict[str, Any]] = []
        for row in rows:
            if not include_bse_only and row["listing_status"] == "BSE_ONLY":
                continue
            if status is not None and row["listing_status"] != status:
                continue

            listing_date = row["listing_date"]
            ev_dates = events_by_entity.get(row["id"], [])
            included = False
            subsequent_delisting_date: str | None = None

            if not ev_dates:
                if row["listing_status"] not in ("DELISTED", "DELISTED_PRE_2006"):
                    if listing_date is None or listing_date <= as_of_date:
                        included = True
            elif row["listing_status"] != "ACTIVE":
                future_dates = [d for d in ev_dates if d > as_of_date]
                if future_dates and (listing_date is None or listing_date <= as_of_date):
                    included = True
                    subsequent_delisting_date = future_dates[0]
            else:
                prior_dates = [d for d in ev_dates if listing_date is None or d < listing_date]
                if prior_dates:
                    d_prev = prior_dates[-1]
                    if listing_date is not None:
                        if as_of_date >= listing_date:
                            included = True
                            future_after = [d for d in ev_dates if d > as_of_date]
                            subsequent_delisting_date = future_after[0] if future_after else None
                        elif as_of_date < prior_dates[0]:
                            included = True
                            subsequent_delisting_date = prior_dates[0]
                    elif as_of_date < d_prev:
                        included = True
                        subsequent_delisting_date = prior_dates[0]
                else:
                    future_dates = [d for d in ev_dates if d > as_of_date]
                    if future_dates and (listing_date is None or listing_date <= as_of_date):
                        included = True
                        subsequent_delisting_date = future_dates[0]

            if included:
                item = dict(row)
                item["as_of_status"] = "ACTIVE_AT_DATE"
                item["subsequent_delisting_date"] = subsequent_delisting_date
                results.append(item)
        return results

    # ------------------------------------------------------------------
    # Typed quantitative, fundamental & qualitative tables
    # ------------------------------------------------------------------

    def _resolve_entity_id(self, isin: str | None, nse_symbol: str | None) -> int | None:
        if isin:
            row = self._connection.execute(
                "SELECT id FROM nse_entities WHERE isin = ? LIMIT 1", (isin,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
        if nse_symbol:
            row = self._connection.execute(
                "SELECT id FROM nse_entities WHERE nse_symbol = ? LIMIT 1", (nse_symbol,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
        return None

    def upsert_daily_market_row(self, record: Mapping[str, Any]) -> bool:
        """Insert or enrich a daily market row keyed by (nse_symbol, trade_date)."""
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        trade_date = _clean_str(record.get("trade_date"))
        if not nse_symbol or not trade_date:
            raise ValueError("Daily market row requires 'nse_symbol' and 'trade_date'.")

        isin = _clean_upper(record.get("isin"))
        entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
            isin, nse_symbol
        )

        existing = self._connection.execute(
            "SELECT id FROM daily_market_data WHERE nse_symbol = ? AND trade_date = ?",
            (nse_symbol, trade_date),
        ).fetchone()

        raw_json = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
        values = (
            entity_id,
            isin,
            nse_symbol,
            _clean_upper(record.get("series")),
            _clean_str(record.get("sector")),
            _clean_str(record.get("industry")),
            trade_date,
            _to_float(record.get("open")),
            _to_float(record.get("high")),
            _to_float(record.get("low")),
            _to_float(record.get("close")),
            _to_int(record.get("total_traded_qty")),
            _to_float(record.get("turnover_cr")),
            _to_int(record.get("deliverable_qty")),
            _to_float(record.get("delivery_pct")),
            _to_int(record.get("no_of_trades")),
            _to_float(record.get("market_cap_cr")),
            _to_float(record.get("free_float_pct")),
            _to_sqlite_bool(record.get("is_fno")),
            _to_sqlite_bool(record.get("bulk_block_deal_flag")),
            _to_float(record.get("futures_oi")),
            _to_float(record.get("oi_change_pct")),
            _to_float(record.get("pe_ratio")),
            _to_float(record.get("sector_median_pe")),
            _to_float(record.get("ev_ebitda")),
            _to_float(record.get("peg_ratio")),
            _to_float(record.get("z_factor_mcap")),
            _to_float(record.get("mcap_percentile")),
            _to_int(record.get("mcap_rank")),
            _clean_upper(record.get("size_bracket")),
            _to_float(record.get("deflated_mcap_cr")),
            _to_float(record.get("equivalent_mcap_cr")),
            _to_sqlite_bool(record.get("in_target_bracket")),
            _to_float(record.get("target_bracket_distance_z")),
            _to_float(record.get("implied_bracket_min_cr")),
            _to_float(record.get("implied_bracket_max_cr")),
            raw_json,
        )

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO daily_market_data (
                    entity_id, isin, nse_symbol, series, sector, industry, trade_date,
                    open, high, low, close, total_traded_qty, turnover_cr,
                    deliverable_qty, delivery_pct, no_of_trades, market_cap_cr,
                    free_float_pct, is_fno, bulk_block_deal_flag, futures_oi,
                    oi_change_pct, pe_ratio, sector_median_pe, ev_ebitda, peg_ratio,
                    z_factor_mcap, mcap_percentile, mcap_rank, size_bracket,
                    deflated_mcap_cr, equivalent_mcap_cr, in_target_bracket,
                    target_bracket_distance_z, implied_bracket_min_cr,
                    implied_bracket_max_cr, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nse_symbol, trade_date) DO UPDATE SET
                    entity_id = COALESCE(excluded.entity_id, daily_market_data.entity_id),
                    isin = COALESCE(excluded.isin, daily_market_data.isin),
                    series = COALESCE(excluded.series, daily_market_data.series),
                    sector = COALESCE(excluded.sector, daily_market_data.sector),
                    industry = COALESCE(excluded.industry, daily_market_data.industry),
                    open = COALESCE(excluded.open, daily_market_data.open),
                    high = COALESCE(excluded.high, daily_market_data.high),
                    low = COALESCE(excluded.low, daily_market_data.low),
                    close = COALESCE(excluded.close, daily_market_data.close),
                    total_traded_qty = COALESCE(excluded.total_traded_qty, daily_market_data.total_traded_qty),
                    turnover_cr = COALESCE(excluded.turnover_cr, daily_market_data.turnover_cr),
                    deliverable_qty = COALESCE(excluded.deliverable_qty, daily_market_data.deliverable_qty),
                    delivery_pct = COALESCE(excluded.delivery_pct, daily_market_data.delivery_pct),
                    no_of_trades = COALESCE(excluded.no_of_trades, daily_market_data.no_of_trades),
                    market_cap_cr = COALESCE(excluded.market_cap_cr, daily_market_data.market_cap_cr),
                    free_float_pct = COALESCE(excluded.free_float_pct, daily_market_data.free_float_pct),
                    is_fno = COALESCE(excluded.is_fno, daily_market_data.is_fno),
                    bulk_block_deal_flag = COALESCE(excluded.bulk_block_deal_flag, daily_market_data.bulk_block_deal_flag),
                    futures_oi = COALESCE(excluded.futures_oi, daily_market_data.futures_oi),
                    oi_change_pct = COALESCE(excluded.oi_change_pct, daily_market_data.oi_change_pct),
                    pe_ratio = COALESCE(excluded.pe_ratio, daily_market_data.pe_ratio),
                    sector_median_pe = COALESCE(excluded.sector_median_pe, daily_market_data.sector_median_pe),
                    ev_ebitda = COALESCE(excluded.ev_ebitda, daily_market_data.ev_ebitda),
                    peg_ratio = COALESCE(excluded.peg_ratio, daily_market_data.peg_ratio),
                    z_factor_mcap = COALESCE(excluded.z_factor_mcap, daily_market_data.z_factor_mcap),
                    mcap_percentile = COALESCE(excluded.mcap_percentile, daily_market_data.mcap_percentile),
                    mcap_rank = COALESCE(excluded.mcap_rank, daily_market_data.mcap_rank),
                    size_bracket = COALESCE(excluded.size_bracket, daily_market_data.size_bracket),
                    deflated_mcap_cr = COALESCE(excluded.deflated_mcap_cr, daily_market_data.deflated_mcap_cr),
                    equivalent_mcap_cr = COALESCE(excluded.equivalent_mcap_cr, daily_market_data.equivalent_mcap_cr),
                    in_target_bracket = COALESCE(excluded.in_target_bracket, daily_market_data.in_target_bracket),
                    target_bracket_distance_z = COALESCE(excluded.target_bracket_distance_z, daily_market_data.target_bracket_distance_z),
                    implied_bracket_min_cr = COALESCE(excluded.implied_bracket_min_cr, daily_market_data.implied_bracket_min_cr),
                    implied_bracket_max_cr = COALESCE(excluded.implied_bracket_max_cr, daily_market_data.implied_bracket_max_cr),
                    raw_json = COALESCE(excluded.raw_json, daily_market_data.raw_json)
                """,
                values,
            )
        return existing is None

    def list_daily_market_rows(
        self, trade_date: str | None = None, nse_symbol: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_date is not None:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if nse_symbol is not None:
            clauses.append("nse_symbol = ?")
            params.append(nse_symbol.strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM daily_market_data {where} ORDER BY trade_date, nse_symbol, id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_daily_market_rows_for_z(
        self, pit_universe_only: bool = True
    ) -> list[dict[str, Any]]:
        """Fetch daily market rows enriched with entity series, optionally excluding post-delisting ghost rows."""
        rows = self._connection.execute(
            """
            SELECT
                d.*,
                COALESCE(d.series, e.series) AS effective_series,
                e.listing_status AS entity_listing_status,
                e.listing_date AS entity_listing_date,
                e.latest_delisting_date AS entity_delisting_date
            FROM daily_market_data d
            LEFT JOIN nse_entities e ON d.entity_id = e.id
            ORDER BY d.trade_date, d.nse_symbol, d.id
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if pit_universe_only and not _is_row_active_at_trade_date(item):
                continue
            item["series"] = item.get("effective_series") or item.get("series")
            out.append(item)
        return out

    def save_z_calibration(self, cal: Mapping[str, Any]) -> int:
        """Persist a target-bracket Z-factor calibration record."""
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO z_factor_calibrations (
                    target_min_cr, target_max_cr, ref_log_center, ref_log_scale,
                    z_min, z_max, source, reference_start, reference_end,
                    reference_dates_count, robust, mainboard_only_stats
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(cal["target_min_cr"]),
                    float(cal["target_max_cr"]),
                    float(cal["ref_log_center"]),
                    float(cal["ref_log_scale"]),
                    float(cal["z_min"]),
                    float(cal["z_max"]),
                    str(cal["source"]),
                    _clean_str(cal.get("reference_start")),
                    _clean_str(cal.get("reference_end")),
                    int(cal.get("reference_dates_count") or 0),
                    1 if cal.get("robust") else 0,
                    1 if cal.get("mainboard_only_stats", True) else 0,
                ),
            )
        return int(cursor.lastrowid)

    def latest_z_calibration(self) -> dict[str, Any] | None:
        """Return the most recently persisted target-bracket Z-factor calibration, if any."""
        row = self._connection.execute(
            "SELECT * FROM z_factor_calibrations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["robust"] = bool(out["robust"])
        out["mainboard_only_stats"] = bool(out["mainboard_only_stats"])
        return out

    def list_target_bracket_stocks(
        self,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        survivorship_free: bool = True,
    ) -> list[dict[str, Any]]:
        """Query standardized target-bracket stocks with entity lifecycle context."""
        clauses: list[str] = ["d.in_target_bracket = 1"]
        params: list[Any] = []
        if trade_date is not None:
            clauses.append("d.trade_date = ?")
            params.append(trade_date)
        if start_date is not None:
            clauses.append("d.trade_date >= ?")
            params.append(start_date)
        if end_date is not None:
            clauses.append("d.trade_date <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}"
        rows = self._connection.execute(
            f"""
            SELECT
                d.*,
                e.company_name AS company_name,
                e.listing_status AS entity_listing_status,
                e.listing_date AS entity_listing_date,
                e.latest_delisting_date AS entity_delisting_date,
                e.latest_delisting_type AS latest_delisting_type
            FROM daily_market_data d
            LEFT JOIN nse_entities e ON d.entity_id = e.id
            {where}
            ORDER BY d.trade_date, d.mcap_rank, d.nse_symbol
            """,
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if survivorship_free and not _is_row_active_at_trade_date(item):
                continue
            item["listing_status"] = item.pop("entity_listing_status", None)
            item["listing_date"] = item.pop("entity_listing_date", None)
            item["latest_delisting_date"] = item.pop("entity_delisting_date", None)
            out.append(item)
        return out

    def upsert_quarterly_fundamental(self, record: Mapping[str, Any]) -> bool:
        """Insert or update point-in-time quarterly fundamentals."""
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        quarter_ending = _clean_str(record.get("quarter_ending"))
        broadcast_timestamp = _clean_str(record.get("broadcast_timestamp"))
        if not nse_symbol or not quarter_ending or not broadcast_timestamp:
            raise ValueError(
                "Quarterly fundamental requires 'nse_symbol', 'quarter_ending', and 'broadcast_timestamp'."
            )

        isin = _clean_upper(record.get("isin"))
        entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
            isin, nse_symbol
        )

        existing = self._connection.execute(
            """
            SELECT id FROM quarterly_fundamentals
            WHERE nse_symbol = ? AND quarter_ending = ? AND broadcast_timestamp = ?
            """,
            (nse_symbol, quarter_ending, broadcast_timestamp),
        ).fetchone()

        values = (
            entity_id,
            isin,
            nse_symbol,
            quarter_ending,
            broadcast_timestamp,
            _to_float(record.get("sales_growth_yoy")),
            _to_float(record.get("sales_growth_qoq")),
            _to_float(record.get("ebitda_margin_pct")),
            _to_float(record.get("pat_growth_yoy")),
            _to_float(record.get("eps_ttm")),
            _to_float(record.get("roce_pct")),
            _to_float(record.get("roe_pct")),
            _to_float(record.get("debt_to_equity")),
            _to_float(record.get("interest_coverage")),
            _to_float(record.get("cash_flow_from_ops")),
            _to_float(record.get("promoter_holding_pct")),
            _to_float(record.get("promoter_pledge_pct")),
            _to_float(record.get("fii_holding_chg_qoq")),
            _to_float(record.get("dii_holding_chg_qoq")),
            _to_float(record.get("retail_holding_chg_qoq")),
        )

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO quarterly_fundamentals (
                    entity_id, isin, nse_symbol, quarter_ending, broadcast_timestamp,
                    sales_growth_yoy, sales_growth_qoq, ebitda_margin_pct,
                    pat_growth_yoy, eps_ttm, roce_pct, roe_pct, debt_to_equity,
                    interest_coverage, cash_flow_from_ops, promoter_holding_pct,
                    promoter_pledge_pct, fii_holding_chg_qoq, dii_holding_chg_qoq,
                    retail_holding_chg_qoq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nse_symbol, quarter_ending, broadcast_timestamp) DO UPDATE SET
                    entity_id = COALESCE(excluded.entity_id, quarterly_fundamentals.entity_id),
                    isin = COALESCE(excluded.isin, quarterly_fundamentals.isin),
                    sales_growth_yoy = COALESCE(excluded.sales_growth_yoy, quarterly_fundamentals.sales_growth_yoy),
                    sales_growth_qoq = COALESCE(excluded.sales_growth_qoq, quarterly_fundamentals.sales_growth_qoq),
                    ebitda_margin_pct = COALESCE(excluded.ebitda_margin_pct, quarterly_fundamentals.ebitda_margin_pct),
                    pat_growth_yoy = COALESCE(excluded.pat_growth_yoy, quarterly_fundamentals.pat_growth_yoy),
                    eps_ttm = COALESCE(excluded.eps_ttm, quarterly_fundamentals.eps_ttm),
                    roce_pct = COALESCE(excluded.roce_pct, quarterly_fundamentals.roce_pct),
                    roe_pct = COALESCE(excluded.roe_pct, quarterly_fundamentals.roe_pct),
                    debt_to_equity = COALESCE(excluded.debt_to_equity, quarterly_fundamentals.debt_to_equity),
                    interest_coverage = COALESCE(excluded.interest_coverage, quarterly_fundamentals.interest_coverage),
                    cash_flow_from_ops = COALESCE(excluded.cash_flow_from_ops, quarterly_fundamentals.cash_flow_from_ops),
                    promoter_holding_pct = COALESCE(excluded.promoter_holding_pct, quarterly_fundamentals.promoter_holding_pct),
                    promoter_pledge_pct = COALESCE(excluded.promoter_pledge_pct, quarterly_fundamentals.promoter_pledge_pct),
                    fii_holding_chg_qoq = COALESCE(excluded.fii_holding_chg_qoq, quarterly_fundamentals.fii_holding_chg_qoq),
                    dii_holding_chg_qoq = COALESCE(excluded.dii_holding_chg_qoq, quarterly_fundamentals.dii_holding_chg_qoq),
                    retail_holding_chg_qoq = COALESCE(excluded.retail_holding_chg_qoq, quarterly_fundamentals.retail_holding_chg_qoq)
                """,
                values,
            )
        return existing is None

    def list_fundamentals_as_of(
        self, nse_symbol: str, as_of_timestamp: str
    ) -> list[dict[str, Any]]:
        """Return fundamentals released on or before as_of_timestamp (zero lookahead)."""
        rows = self._connection.execute(
            """
            SELECT * FROM quarterly_fundamentals
            WHERE nse_symbol = ? AND broadcast_timestamp <= ?
            ORDER BY broadcast_timestamp, quarter_ending, id
            """,
            (nse_symbol.strip().upper(), as_of_timestamp),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_qualitative_filing(self, record: Mapping[str, Any]) -> bool:
        """Insert a qualitative filing deterministically by content hash."""
        isin = _clean_upper(record.get("isin"))
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
            isin, nse_symbol
        )

        payload = {
            "isin": isin or "",
            "nse_symbol": nse_symbol or "",
            "broadcast_timestamp": _clean_str(record.get("broadcast_timestamp")) or "",
            "surveillance_stage": _clean_str(record.get("surveillance_stage")) or "",
            "announcement_category": _clean_str(record.get("announcement_category")) or "",
            "announcement_headline": _clean_str(record.get("announcement_headline")) or "",
            "announcement_body_md": _clean_str(record.get("announcement_body_md")) or "",
            "concall_commentary_md": _clean_str(record.get("concall_commentary_md")) or "",
            "concall_guidance_md": _clean_str(record.get("concall_guidance_md")) or "",
            "sast_insider_activity": _serialize_optional_text(
                record.get("sast_insider_activity")
            )
            or "",
            "bulk_block_deal_participants": _serialize_optional_text(
                record.get("bulk_block_deal_participants")
            )
            or "",
            "source_url": _clean_str(record.get("source_url")) or "",
            "local_artifact_path": _clean_str(record.get("local_artifact_path")) or "",
        }
        filing_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

        existing = self._connection.execute(
            "SELECT id FROM qualitative_filings WHERE filing_hash = ?",
            (filing_hash,),
        ).fetchone()

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO qualitative_filings (
                    entity_id, filing_hash, isin, nse_symbol, broadcast_timestamp,
                    surveillance_stage, announcement_category, announcement_headline,
                    announcement_body_md, concall_commentary_md, concall_guidance_md,
                    sast_insider_activity, bulk_block_deal_participants, source_url,
                    local_artifact_path, confidence_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filing_hash) DO UPDATE SET
                    entity_id = COALESCE(qualitative_filings.entity_id, excluded.entity_id)
                """,
                (
                    entity_id,
                    filing_hash,
                    isin,
                    nse_symbol,
                    _clean_str(record.get("broadcast_timestamp")),
                    _clean_str(record.get("surveillance_stage")),
                    _clean_str(record.get("announcement_category")),
                    _clean_str(record.get("announcement_headline")),
                    _clean_str(record.get("announcement_body_md")),
                    _clean_str(record.get("concall_commentary_md")),
                    _clean_str(record.get("concall_guidance_md")),
                    _serialize_optional_text(record.get("sast_insider_activity")),
                    _serialize_optional_text(record.get("bulk_block_deal_participants")),
                    _clean_str(record.get("source_url")),
                    _clean_str(record.get("local_artifact_path")),
                    _clean_upper(record.get("confidence_status")),
                ),
            )
        return existing is None

    def list_qualitative_filings(
        self, nse_symbol: str | None = None, as_of_timestamp: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if nse_symbol is not None:
            clauses.append("nse_symbol = ?")
            params.append(nse_symbol.strip().upper())
        if as_of_timestamp is not None:
            clauses.append("(broadcast_timestamp IS NOT NULL AND broadcast_timestamp <= ?)")
            params.append(as_of_timestamp)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM qualitative_filings {where} ORDER BY COALESCE(broadcast_timestamp, ''), id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def ingest_unified_market_record(self, record: Mapping[str, Any]) -> dict[str, bool]:
        """Persist a canonical 50-column market record into raw and typed tables."""
        normalized = normalize_market_record(record)
        market_inserted = self.add_market_record(normalized)

        daily_upserted = False
        if _clean_symbol(normalized.get("nse_symbol")) and _clean_str(
            normalized.get("trade_date")
        ):
            daily_upserted = self.upsert_daily_market_row(normalized)

        fundamental_upserted = False
        if _clean_symbol(normalized.get("nse_symbol")) and _clean_str(
            normalized.get("quarter_ending")
        ):
            fundamental_payload = dict(normalized)
            fundamental_payload["broadcast_timestamp"] = (
                _clean_str(normalized.get("broadcast_timestamp"))
                or _clean_str(normalized.get("trade_date"))
                or _clean_str(normalized.get("quarter_ending"))
            )
            fundamental_upserted = self.upsert_quarterly_fundamental(fundamental_payload)

        qualitative_fields = (
            "surveillance_stage",
            "announcement_category",
            "announcement_headline",
            "announcement_body_md",
            "concall_commentary_md",
            "concall_guidance_md",
            "sast_insider_activity",
            "bulk_block_deal_participants",
        )
        qualitative_inserted = False
        if any(normalized.get(field) is not None for field in qualitative_fields):
            qualitative_inserted = self.upsert_qualitative_filing(normalized)

        return {
            "market_record_inserted": market_inserted,
            "daily_upserted": daily_upserted,
            "fundamental_upserted": fundamental_upserted,
            "qualitative_inserted": qualitative_inserted,
        }

    # ------------------------------------------------------------------
    # Internal matchers & schema initialization
    # ------------------------------------------------------------------

    def _find_existing_nse_entity(
        self,
        isin: str | None,
        nse_symbol: str | None,
        normalized_name: str,
    ) -> sqlite3.Row | None:
        if isin:
            row = self._connection.execute(
                "SELECT * FROM nse_entities WHERE isin = ? LIMIT 1", (isin,)
            ).fetchone()
            if row is not None:
                return row

        if nse_symbol:
            rows = self._connection.execute(
                "SELECT * FROM nse_entities WHERE nse_symbol = ? ORDER BY id",
                (nse_symbol,),
            ).fetchall()
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                return row

        if normalized_name:
            rows = self._connection.execute(
                "SELECT * FROM nse_entities WHERE normalized_name = ? ORDER BY id",
                (normalized_name,),
            ).fetchall()
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                if nse_symbol and row["nse_symbol"] == nse_symbol:
                    return row
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                return row
        return None

    def _find_existing(
        self,
        company: CompanyRecord,
        normalized_name: str,
        normalized_website: str | None,
        normalized_country: str | None,
    ) -> sqlite3.Row | None:
        if company.source and company.source_id:
            row = self._connection.execute(
                "SELECT * FROM companies WHERE source = ? AND source_id = ?",
                (company.source, company.source_id),
            ).fetchone()
            if row is not None:
                return row

        if normalized_website:
            row = self._connection.execute(
                "SELECT * FROM companies WHERE normalized_website = ?",
                (normalized_website,),
            ).fetchone()
            if row is not None:
                return row

        if normalized_country:
            return self._connection.execute(
                """
                SELECT * FROM companies
                WHERE normalized_name = ? AND normalized_country = ?
                """,
                (normalized_name, normalized_country),
            ).fetchone()

        return self._connection.execute(
            """
            SELECT * FROM companies
            WHERE normalized_name = ? AND normalized_country IS NULL
            """,
            (normalized_name,),
        ).fetchone()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                website TEXT,
                normalized_website TEXT,
                country TEXT,
                normalized_country TEXT,
                industry TEXT,
                description TEXT,
                source TEXT,
                source_id TEXT,
                attributes_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS market_records (
                id INTEGER PRIMARY KEY,
                record_hash TEXT NOT NULL UNIQUE,
                record_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS companies_source_id_unique
                ON companies(source, source_id)
                WHERE source IS NOT NULL AND source_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS companies_website_unique
                ON companies(normalized_website)
                WHERE normalized_website IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS companies_name_country_unique
                ON companies(normalized_name, normalized_country)
                WHERE normalized_country IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS companies_name_without_country_unique
                ON companies(normalized_name)
                WHERE normalized_country IS NULL;

            CREATE TABLE IF NOT EXISTS nse_entities (
                id INTEGER PRIMARY KEY,
                isin TEXT,
                nse_symbol TEXT,
                company_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                series TEXT,
                sector TEXT,
                industry TEXT,
                listing_date TEXT,
                listing_status TEXT NOT NULL,
                paid_up_value REAL,
                face_value REAL,
                latest_delisting_date TEXT,
                latest_delisting_type TEXT,
                source_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS nse_entities_isin_idx
                ON nse_entities(isin);
            CREATE UNIQUE INDEX IF NOT EXISTS nse_entities_symbol_idx
                ON nse_entities(nse_symbol);
            CREATE INDEX IF NOT EXISTS nse_entities_norm_name_idx
                ON nse_entities(normalized_name);
            CREATE INDEX IF NOT EXISTS nse_entities_status_idx
                ON nse_entities(listing_status);

            CREATE TABLE IF NOT EXISTS delisting_events (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL REFERENCES nse_entities(id),
                record_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                company_name TEXT NOT NULL,
                nse_symbol TEXT,
                isin TEXT,
                delisting_type TEXT,
                order_date TEXT,
                effective_delisting_date TEXT,
                calendar_year INTEGER,
                financial_year TEXT,
                is_priority_year INTEGER,
                regulatory_regime TEXT,
                exit_or_fair_value_inr REAL,
                source_url TEXT,
                local_artifact_path TEXT,
                confidence_status TEXT,
                source_count INTEGER,
                all_source_urls TEXT,
                all_artifact_paths TEXT,
                source_conflicts TEXT,
                review_flags TEXT,
                date_source TEXT
            );
            CREATE INDEX IF NOT EXISTS delisting_events_entity_idx
                ON delisting_events(entity_id);
            CREATE INDEX IF NOT EXISTS delisting_events_scope_idx
                ON delisting_events(scope);

            CREATE TABLE IF NOT EXISTS daily_market_data (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER REFERENCES nse_entities(id),
                isin TEXT,
                nse_symbol TEXT NOT NULL,
                series TEXT,
                sector TEXT,
                industry TEXT,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                total_traded_qty INTEGER,
                turnover_cr REAL,
                deliverable_qty INTEGER,
                delivery_pct REAL,
                no_of_trades INTEGER,
                market_cap_cr REAL,
                free_float_pct REAL,
                is_fno INTEGER,
                bulk_block_deal_flag INTEGER,
                futures_oi REAL,
                oi_change_pct REAL,
                pe_ratio REAL,
                sector_median_pe REAL,
                ev_ebitda REAL,
                peg_ratio REAL,
                z_factor_mcap REAL,
                mcap_percentile REAL,
                mcap_rank INTEGER,
                size_bracket TEXT,
                deflated_mcap_cr REAL,
                equivalent_mcap_cr REAL,
                in_target_bracket INTEGER,
                target_bracket_distance_z REAL,
                implied_bracket_min_cr REAL,
                implied_bracket_max_cr REAL,
                raw_json TEXT,
                UNIQUE(nse_symbol, trade_date)
            );
            CREATE INDEX IF NOT EXISTS daily_market_trade_date_idx
                ON daily_market_data(trade_date);
            CREATE INDEX IF NOT EXISTS daily_market_isin_date_idx
                ON daily_market_data(isin, trade_date);
            CREATE INDEX IF NOT EXISTS daily_market_entity_date_idx
                ON daily_market_data(entity_id, trade_date);

            CREATE TABLE IF NOT EXISTS z_factor_calibrations (
                id INTEGER PRIMARY KEY,
                target_min_cr REAL NOT NULL,
                target_max_cr REAL NOT NULL,
                ref_log_center REAL NOT NULL,
                ref_log_scale REAL NOT NULL,
                z_min REAL NOT NULL,
                z_max REAL NOT NULL,
                source TEXT NOT NULL,
                reference_start TEXT,
                reference_end TEXT,
                reference_dates_count INTEGER NOT NULL DEFAULT 0,
                robust INTEGER NOT NULL DEFAULT 0,
                mainboard_only_stats INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quarterly_fundamentals (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER REFERENCES nse_entities(id),
                isin TEXT,
                nse_symbol TEXT NOT NULL,
                quarter_ending TEXT NOT NULL,
                broadcast_timestamp TEXT NOT NULL,
                sales_growth_yoy REAL,
                sales_growth_qoq REAL,
                ebitda_margin_pct REAL,
                pat_growth_yoy REAL,
                eps_ttm REAL,
                roce_pct REAL,
                roe_pct REAL,
                debt_to_equity REAL,
                interest_coverage REAL,
                cash_flow_from_ops REAL,
                promoter_holding_pct REAL,
                promoter_pledge_pct REAL,
                fii_holding_chg_qoq REAL,
                dii_holding_chg_qoq REAL,
                retail_holding_chg_qoq REAL,
                UNIQUE(nse_symbol, quarter_ending, broadcast_timestamp)
            );
            CREATE INDEX IF NOT EXISTS quarterly_fundamentals_sym_ts_idx
                ON quarterly_fundamentals(nse_symbol, broadcast_timestamp);

            CREATE TABLE IF NOT EXISTS qualitative_filings (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER REFERENCES nse_entities(id),
                filing_hash TEXT NOT NULL UNIQUE,
                isin TEXT,
                nse_symbol TEXT,
                broadcast_timestamp TEXT,
                surveillance_stage TEXT,
                announcement_category TEXT,
                announcement_headline TEXT,
                announcement_body_md TEXT,
                concall_commentary_md TEXT,
                concall_guidance_md TEXT,
                sast_insider_activity TEXT,
                bulk_block_deal_participants TEXT,
                source_url TEXT,
                local_artifact_path TEXT,
                confidence_status TEXT
            );
            CREATE INDEX IF NOT EXISTS qualitative_filings_sym_ts_idx
                ON qualitative_filings(nse_symbol, broadcast_timestamp);
            CREATE INDEX IF NOT EXISTS qualitative_filings_entity_idx
                ON qualitative_filings(entity_id);
            """
        )

    def _migrate_schema(self) -> None:
        duplicate_isins = self._connection.execute(
            """
            SELECT isin
            FROM nse_entities
            WHERE isin IS NOT NULL
            GROUP BY isin
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate_isins is not None:
            raise sqlite3.IntegrityError(
                f"Cannot create unique NSE ISIN index; duplicate ISIN: {duplicate_isins['isin']}"
            )

        duplicate_symbols = self._connection.execute(
            """
            SELECT nse_symbol
            FROM nse_entities
            WHERE nse_symbol IS NOT NULL
            GROUP BY nse_symbol
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate_symbols is not None:
            raise sqlite3.IntegrityError(
                "Cannot create unique NSE symbol index; "
                f"duplicate symbol: {duplicate_symbols['nse_symbol']}"
            )

        with self._connection:
            self._connection.execute("DROP INDEX IF EXISTS nse_entities_isin_idx")
            self._connection.execute(
                "CREATE UNIQUE INDEX nse_entities_isin_idx ON nse_entities(isin)"
            )
            self._connection.execute("DROP INDEX IF EXISTS nse_entities_symbol_idx")
            self._connection.execute(
                "CREATE UNIQUE INDEX nse_entities_symbol_idx ON nse_entities(nse_symbol)"
            )

        existing_cols = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(daily_market_data)"
            ).fetchall()
        }
        new_cols = (
            ("mcap_rank", "INTEGER"),
            ("equivalent_mcap_cr", "REAL"),
            ("in_target_bracket", "INTEGER"),
            ("target_bracket_distance_z", "REAL"),
            ("implied_bracket_min_cr", "REAL"),
            ("implied_bracket_max_cr", "REAL"),
        )
        with self._connection:
            for col_name, col_type in new_cols:
                if col_name not in existing_cols:
                    self._connection.execute(
                        f"ALTER TABLE daily_market_data ADD COLUMN {col_name} {col_type}"
                    )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS daily_market_target_bracket_idx
                    ON daily_market_data(trade_date, in_target_bracket)
                """
            )


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalize_nse_entity_name(value: str | None) -> str:
    s = re.sub(r"[.,&()'\"\-/]", " ", (value or "").upper())
    return re.sub(r"\s+", " ", _NAME_STOP_RE.sub(" ", s)).strip()


def _normalize_website(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    domain = (parsed.hostname or "").casefold().removeprefix("www.")
    return domain or None


def _row_to_company(row: sqlite3.Row) -> CompanyRecord:
    return CompanyRecord(
        name=row["name"],
        website=row["website"],
        country=row["country"],
        industry=row["industry"],
        description=row["description"],
        source=row["source"],
        source_id=row["source_id"],
        attributes=json.loads(row["attributes_json"]),
    )


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_upper(value: Any) -> str | None:
    text = _clean_str(value)
    return text.upper() if text else None


def _clean_symbol(value: Any) -> str | None:
    text = _clean_upper(value)
    if not text or text == "UNKNOWN":
        return None
    return text


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_sqlite_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return 1
    if text in {"0", "false", "no", "n"}:
        return 0
    return None


def _serialize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _max_non_empty(first: str | None, second: str | None) -> str | None:
    if first and second:
        return max(first, second)
    return first or second


def _resolve_merged_status(
    *,
    existing_status: str,
    existing_listing_date: str | None,
    existing_delisting_date: str | None,
    incoming_status: str,
    incoming_listing_date: str | None,
    incoming_delisting_date: str | None,
) -> str:
    delisted_statuses = {"DELISTED", "DELISTED_PRE_2006"}

    if existing_status == "BSE_ONLY" and incoming_status != "BSE_ONLY":
        return incoming_status
    if incoming_status == "BSE_ONLY" and existing_status != "BSE_ONLY":
        return existing_status

    if existing_status in delisted_statuses and incoming_status == "PROPOSED_DELISTING":
        return existing_status

    if existing_status == "ACTIVE" and incoming_status in delisted_statuses:
        if (
            existing_listing_date
            and incoming_delisting_date
            and incoming_delisting_date < existing_listing_date
        ):
            return "ACTIVE"
        return incoming_status

    if existing_status in delisted_statuses and incoming_status == "ACTIVE":
        if (
            incoming_listing_date
            and existing_delisting_date
            and existing_delisting_date < incoming_listing_date
        ):
            return "ACTIVE"
        return existing_status

    if existing_status in delisted_statuses and incoming_status in delisted_statuses:
        if existing_status == "DELISTED" or incoming_status == "DELISTED":
            return "DELISTED"
        return incoming_status

    return incoming_status or existing_status


def _is_row_active_at_trade_date(row: Mapping[str, Any]) -> bool:
    """Exclude post-delisting ghost prints and delisted gaps on re-listed stocks."""
    if row.get("entity_id") is None:
        return True
    trade_date = row.get("trade_date")
    status = row.get("entity_listing_status")
    listing_date = row.get("entity_listing_date")
    delisting_date = row.get("entity_delisting_date")
    if not trade_date:
        return True

    if status in ("DELISTED", "DELISTED_PRE_2006"):
        if delisting_date and trade_date >= delisting_date:
            return False
        if listing_date and trade_date < listing_date:
            return False
        return True

    if status == "ACTIVE":
        if delisting_date and listing_date and delisting_date < listing_date:
            if delisting_date <= trade_date < listing_date:
                return False
        return True

    return True
