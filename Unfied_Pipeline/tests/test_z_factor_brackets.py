"""Tests for target-bracket Z-factor standardization (3,500-12,500 Cr) across historical NSE eras."""

from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from company_pipeline.storage import CompanyStore
from company_pipeline.z_factor import (
    DEFAULT_NSE_MAINBOARD_LOG_MEAN,
    DEFAULT_NSE_MAINBOARD_LOG_STD,
    TargetBracketCalibration,
    calibrate_target_bracket,
    compute_cross_sectional_z_factors,
    normalize_bracket_bounds,
    recompute_store_z_factors,
    summarize_historical_brackets,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _synthetic_cross_section(trade_date: str, median_cr: float, std_log: float = 2.45, n: int = 21) -> list[dict]:
    """Generate a symmetric log-normal market-cap cross-section centered at median_cr."""
    mu = math.log(median_cr)
    # Deterministic symmetric grid around mu with exact population mean=mu and pstdev=std_log
    half = n // 2
    raw_zs = [float(i - half) for i in range(n)]
    scale = math.sqrt(sum(z * z for z in raw_zs) / n)
    zs = [z / scale for z in raw_zs]
    rows: list[dict] = []
    for idx, z in enumerate(zs):
        rows.append(
            {
                "nse_symbol": f"STK{idx:02d}",
                "series": "EQ",
                "trade_date": trade_date,
                "market_cap_cr": round(math.exp(mu + std_log * z), 4),
            }
        )
    return rows


class TargetBracketZFactorTests(unittest.TestCase):
    def test_nse_entity_unique_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unique.sqlite3"
            with CompanyStore(db_path) as store:
                store.upsert_nse_entity(
                    {
                        "isin": "INE000A01010",
                        "nse_symbol": "UNIQUE1",
                        "company_name": "Unique One Limited",
                    }
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        """
                        INSERT INTO nse_entities
                            (isin, nse_symbol, company_name, normalized_name, listing_status)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "INE000A01010",
                            "UNIQUE2",
                            "Duplicate ISIN Limited",
                            "UNIQUE ISIN LIMITED",
                            "ACTIVE",
                        ),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        """
                        INSERT INTO nse_entities
                            (isin, nse_symbol, company_name, normalized_name, listing_status)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "INE000A01011",
                            "UNIQUE1",
                            "Duplicate Symbol Limited",
                            "UNIQUE SYMBOL LIMITED",
                            "ACTIVE",
                        ),
                    )

    def test_bound_normalization_and_validation(self) -> None:
        self.assertEqual(normalize_bracket_bounds(12500, 3500), (3500.0, 12500.0))
        self.assertEqual(normalize_bracket_bounds(3500, 12500), (3500.0, 12500.0))

        for bad_a, bad_b in (
            (0, 12500),
            (-100, 3500),
            (3500, 3500),
            (math.nan, 12500),
            (math.inf, 12500),
            (True, 12500),
        ):
            with self.assertRaises(ValueError):
                normalize_bracket_bounds(bad_a, bad_b)

    def test_15_year_nse_growth_bracket_translation(self) -> None:
        # 2010 median ~120 Cr, 2017 median ~380 Cr, 2025 median ~950 Cr (matching modern NSE prior)
        rows_2010 = _synthetic_cross_section("2010-06-01", median_cr=120.0)
        rows_2017 = _synthetic_cross_section("2017-06-01", median_cr=380.0)
        rows_2025 = _synthetic_cross_section("2025-06-01", median_cr=950.0)

        scored = compute_cross_sectional_z_factors(
            rows_2010 + rows_2017 + rows_2025,
            target_bracket_cr=(12500, 3500),
        )
        schedule = summarize_historical_brackets(
            rows_2010 + rows_2017 + rows_2025,
            target_bracket_cr=(3500, 12500),
        )
        by_date = {row["trade_date"]: row for row in schedule}

        # Verify historical implied rupee brackets grow proportionally with the market
        self.assertLess(by_date["2010-06-01"]["implied_min_cr"], 600.0)
        self.assertLess(by_date["2010-06-01"]["implied_max_cr"], 2000.0)
        self.assertGreater(by_date["2017-06-01"]["implied_min_cr"], by_date["2010-06-01"]["implied_min_cr"])
        self.assertGreater(by_date["2025-06-01"]["implied_min_cr"], 3400.0)
        self.assertLess(by_date["2025-06-01"]["implied_max_cr"], 13000.0)

        # Verify naive nominal [3500, 12500] Cr in 2010 hits Z ~ +1.38..+1.90 (Large/Mega Cap),
        # whereas Z-bracket matches equivalent mid-cap rank across all 3 eras.
        matches_2010 = [r for r in scored if r["trade_date"] == "2010-06-01" and r["in_target_bracket"]]
        matches_2017 = [r for r in scored if r["trade_date"] == "2017-06-01" and r["in_target_bracket"]]
        matches_2025 = [r for r in scored if r["trade_date"] == "2025-06-01" and r["in_target_bracket"]]

        self.assertGreaterEqual(len(matches_2010), 1)
        self.assertEqual(
            {r["mcap_rank"] for r in matches_2010},
            {r["mcap_rank"] for r in matches_2017},
        )
        self.assertEqual(
            {r["mcap_rank"] for r in matches_2017},
            {r["mcap_rank"] for r in matches_2025},
        )
        for row in scored:
            if row["in_target_bracket"]:
                self.assertGreaterEqual(row["equivalent_mcap_cr"], 3500.0 - 1e-3)
                self.assertLessEqual(row["equivalent_mcap_cr"], 12500.0 + 1e-3)
                self.assertEqual(row["target_bracket_distance_z"], 0.0)
            else:
                self.assertNotEqual(row["target_bracket_distance_z"], 0.0)

    def test_empirical_reference_window_calibration(self) -> None:
        rows_2010 = _synthetic_cross_section("2010-06-01", median_cr=120.0)
        rows_2024 = _synthetic_cross_section("2024-06-01", median_cr=944.0)
        rows_2025 = _synthetic_cross_section("2025-06-01", median_cr=950.0)
        rows_2026 = _synthetic_cross_section("2026-06-01", median_cr=960.0)

        cal = calibrate_target_bracket(
            rows_2010 + rows_2024 + rows_2025 + rows_2026,
            target_bracket_cr=(3500.0, 12500.0),
            reference_years=3,
        )
        self.assertEqual(cal.source, "reference_window")
        self.assertEqual(cal.reference_dates_count, 3)
        self.assertAlmostEqual(cal.ref_log_center, math.log(950.0), places=3)
        self.assertAlmostEqual(cal.ref_log_scale, 2.45, places=3)

        scored = compute_cross_sectional_z_factors(
            rows_2010 + rows_2025,
            calibration=cal,
        )
        matches_2010 = [r for r in scored if r["trade_date"] == "2010-06-01" and r["in_target_bracket"]]
        matches_2025 = [r for r in scored if r["trade_date"] == "2025-06-01" and r["in_target_bracket"]]
        self.assertEqual(
            [r["nse_symbol"] for r in matches_2010],
            [r["nse_symbol"] for r in matches_2025],
        )

    def test_strict_zero_lookahead_with_causal_trailing_days(self) -> None:
        rows_2010 = [
            *_synthetic_cross_section("2010-01-01", median_cr=115.0),
            *_synthetic_cross_section("2010-01-02", median_cr=118.0),
            *_synthetic_cross_section("2010-01-03", median_cr=120.0),
        ]
        rows_2026_boom = _synthetic_cross_section("2026-01-01", median_cr=12000.0)
        rows_2026_crash = _synthetic_cross_section("2026-01-01", median_cr=120.0)

        fixed_cal = TargetBracketCalibration.from_fixed_z(
            (math.log(3500.0) - DEFAULT_NSE_MAINBOARD_LOG_MEAN) / DEFAULT_NSE_MAINBOARD_LOG_STD,
            (math.log(12500.0) - DEFAULT_NSE_MAINBOARD_LOG_MEAN) / DEFAULT_NSE_MAINBOARD_LOG_STD,
        )

        for cal in (None, fixed_cal):
            base = compute_cross_sectional_z_factors(
                rows_2010, calibration=cal, trailing_days=3
            )
            with_boom = compute_cross_sectional_z_factors(
                rows_2010 + rows_2026_boom, calibration=cal, trailing_days=3
            )[: len(rows_2010)]
            with_crash = compute_cross_sectional_z_factors(
                rows_2010 + rows_2026_crash, calibration=cal, trailing_days=3
            )[: len(rows_2010)]

            keys = (
                "z_factor",
                "mcap_rank",
                "equivalent_mcap_cr",
                "in_target_bracket",
                "target_bracket_distance_z",
                "implied_bracket_min_cr",
                "implied_bracket_max_cr",
            )
            for a, b, c in zip(base, with_boom, with_crash):
                for k in keys:
                    self.assertEqual(a[k], b[k], f"Mismatch on {k} vs boom")
                    self.assertEqual(a[k], c[k], f"Mismatch on {k} vs crash")

    def test_sme_microcap_distortion_immunity(self) -> None:
        mainboard_2025 = _synthetic_cross_section("2025-06-01", median_cr=950.0)
        sme_swarm = [
            {
                "nse_symbol": f"SME{i:03d}",
                "series": "SM" if i % 2 == 0 else "ST",
                "trade_date": "2025-06-01",
                "market_cap_cr": 25.0 + i,
            }
            for i in range(60)
        ]

        baseline = compute_cross_sectional_z_factors(mainboard_2025, mainboard_only_stats=True)
        with_sme = compute_cross_sectional_z_factors(
            mainboard_2025 + sme_swarm, mainboard_only_stats=True
        )[: len(mainboard_2025)]

        for b, w in zip(baseline, with_sme):
            self.assertEqual(b["z_factor"], w["z_factor"])
            self.assertEqual(b["in_target_bracket"], w["in_target_bracket"])
            self.assertEqual(b["implied_bracket_min_cr"], w["implied_bracket_min_cr"])
            self.assertEqual(b["implied_bracket_max_cr"], w["implied_bracket_max_cr"])

    def test_survivorship_free_store_and_ghost_print_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            with CompanyStore(db_path) as store:
                # Active entity with re-listing after earlier delisting
                store.upsert_nse_entity(
                    {
                        "isin": "INE177C01022",
                        "nse_symbol": "BATLIBOI",
                        "company_name": "Batliboi Limited",
                        "series": "EQ",
                        "listing_date": "2026-04-20",
                        "listing_status": "ACTIVE",
                        "latest_delisting_date": "2012-06-28",
                    }
                )
                # Permanently delisted entity
                store.upsert_nse_entity(
                    {
                        "isin": "INE999A01011",
                        "nse_symbol": "OLDCO",
                        "company_name": "Old Delisted Limited",
                        "series": "EQ",
                        "listing_status": "DELISTED",
                        "latest_delisting_date": "2012-06-28",
                    }
                )

                # Create cross-sections across 2010 (pre-delisting), 2015 (delisted gap), 2026 (post-relisting)
                for td, med in (("2010-06-01", 120.0), ("2015-06-01", 300.0), ("2026-06-01", 950.0)):
                    for row in _synthetic_cross_section(td, median_cr=med):
                        store.upsert_daily_market_row(row)
                    # Equivalent target-bracket market cap (~650 Cr in 2010, ~1600 Cr in 2015, ~5200 Cr in 2026)
                    target_mcap = round(math.exp(math.log(med) + 2.45 * 0.70), 4)
                    store.upsert_daily_market_row({"nse_symbol": "BATLIBOI", "trade_date": td, "market_cap_cr": target_mcap})
                    store.upsert_daily_market_row({"nse_symbol": "OLDCO", "trade_date": td, "market_cap_cr": target_mcap})

                summary = recompute_store_z_factors(store, pit_universe_only=True)
                self.assertEqual(summary["dates_processed"], 3)

                matches_2010 = {r["nse_symbol"] for r in store.list_target_bracket_stocks(trade_date="2010-06-01")}
                matches_2015 = {r["nse_symbol"] for r in store.list_target_bracket_stocks(trade_date="2015-06-01")}
                matches_2026 = {r["nse_symbol"] for r in store.list_target_bracket_stocks(trade_date="2026-06-01")}

                self.assertIn("OLDCO", matches_2010)
                self.assertNotIn("OLDCO", matches_2015)
                self.assertNotIn("OLDCO", matches_2026)

                self.assertIn("BATLIBOI", matches_2010)
                self.assertNotIn("BATLIBOI", matches_2015)
                self.assertIn("BATLIBOI", matches_2026)

    def test_schema_migration_and_cli_target_bracket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pre_migration.sqlite3"
            # Create a pre-migration daily_market_data table without the 6 new columns
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE daily_market_data (
                    id INTEGER PRIMARY KEY,
                    entity_id INTEGER,
                    isin TEXT,
                    nse_symbol TEXT NOT NULL,
                    series TEXT,
                    sector TEXT,
                    industry TEXT,
                    trade_date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    total_traded_qty INTEGER, turnover_cr REAL,
                    deliverable_qty INTEGER, delivery_pct REAL, no_of_trades INTEGER,
                    market_cap_cr REAL, free_float_pct REAL, is_fno INTEGER,
                    bulk_block_deal_flag INTEGER, futures_oi REAL, oi_change_pct REAL,
                    pe_ratio REAL, sector_median_pe REAL, ev_ebitda REAL, peg_ratio REAL,
                    z_factor_mcap REAL, mcap_percentile REAL, size_bracket TEXT,
                    deflated_mcap_cr REAL, raw_json TEXT,
                    UNIQUE(nse_symbol, trade_date)
                );
                """
            )
            conn.commit()
            conn.close()

            with CompanyStore(db_path) as store:
                cols = {
                    r["name"]
                    for r in store._connection.execute("PRAGMA table_info(daily_market_data)").fetchall()
                }
                self.assertTrue(
                    {
                        "mcap_rank",
                        "equivalent_mcap_cr",
                        "in_target_bracket",
                        "target_bracket_distance_z",
                        "implied_bracket_min_cr",
                        "implied_bracket_max_cr",
                    }
                    <= cols
                )
                for row in _synthetic_cross_section("2010-06-01", median_cr=120.0) + _synthetic_cross_section("2025-06-01", median_cr=950.0):
                    store.upsert_daily_market_row(row)

            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT / "src")}
            r_z = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "company_pipeline.cli",
                    "--database",
                    str(db_path),
                    "compute-z-factors",
                    "--target-min-cr",
                    "3500",
                    "--target-max-cr",
                    "12500",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            z_out = json.loads(r_z.stdout)
            self.assertEqual(z_out["rows_updated"], 42)
            self.assertGreater(z_out["target_bracket_matches"], 0)

            sched_csv = Path(tmp) / "schedule.csv"
            r_sched = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "company_pipeline.cli",
                    "--database",
                    str(db_path),
                    "target-bracket",
                    "--schedule",
                    "--output",
                    str(sched_csv),
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            sched_out = json.loads(r_sched.stdout)
            self.assertEqual(sched_out["dates"], 2)
            with sched_csv.open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), 2)

            r_list = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "company_pipeline.cli",
                    "--database",
                    str(db_path),
                    "target-bracket",
                    "--date",
                    "2010-06-01",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            list_out = json.loads(r_list.stdout)
            self.assertGreater(list_out["count"], 0)
            self.assertGreaterEqual(list_out["sample"][0]["equivalent_mcap_cr"], 3500.0)


if __name__ == "__main__":
    unittest.main()
