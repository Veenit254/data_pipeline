"""Point-in-time cross-sectional Z-factor standardization (zero lookahead bias)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
import math
import statistics
from typing import Any

from company_pipeline.storage import CompanyStore

DEFAULT_TARGET_BRACKET_CR: tuple[float, float] = (3500.0, 12500.0)
DEFAULT_NSE_MAINBOARD_LOG_MEAN: float = 6.85
DEFAULT_NSE_MAINBOARD_LOG_STD: float = 2.45
SME_SERIES: frozenset[str] = frozenset({"SM", "ST", "SZ"})


def normalize_bracket_bounds(bound_a: float, bound_b: float) -> tuple[float, float]:
    """Validate and sort market-cap bracket bounds into (low_cr, high_cr)."""
    if isinstance(bound_a, bool) or isinstance(bound_b, bool):
        raise ValueError("Target bracket bounds must be positive finite numbers.")
    try:
        a = float(bound_a)
        b = float(bound_b)
    except (TypeError, ValueError) as exc:
        raise ValueError("Target bracket bounds must be positive finite numbers.") from exc
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0 or b <= 0:
        raise ValueError("Target bracket bounds must be positive finite numbers.")
    low, high = min(a, b), max(a, b)
    if low == high:
        raise ValueError("Target bracket lower and upper bounds must differ.")
    return low, high


@dataclass(frozen=True)
class TargetBracketCalibration:
    """Mapping between a modern rupee target bracket and cross-sectional Z-score bounds."""

    target_min_cr: float
    target_max_cr: float
    ref_log_center: float
    ref_log_scale: float
    z_min: float
    z_max: float
    source: str
    reference_start: str | None = None
    reference_end: str | None = None
    reference_dates_count: int = 0
    robust: bool = False
    mainboard_only_stats: bool = True

    @classmethod
    def default_nse(
        cls,
        target_bracket_cr: tuple[float, float] = DEFAULT_TARGET_BRACKET_CR,
        robust: bool = False,
        mainboard_only_stats: bool = True,
    ) -> "TargetBracketCalibration":
        """Build a zero-lookahead calibration from the default modern NSE mainboard prior."""
        low, high = normalize_bracket_bounds(*target_bracket_cr)
        z_min = (math.log(low) - DEFAULT_NSE_MAINBOARD_LOG_MEAN) / DEFAULT_NSE_MAINBOARD_LOG_STD
        z_max = (math.log(high) - DEFAULT_NSE_MAINBOARD_LOG_MEAN) / DEFAULT_NSE_MAINBOARD_LOG_STD
        return cls(
            target_min_cr=low,
            target_max_cr=high,
            ref_log_center=DEFAULT_NSE_MAINBOARD_LOG_MEAN,
            ref_log_scale=DEFAULT_NSE_MAINBOARD_LOG_STD,
            z_min=z_min,
            z_max=z_max,
            source="nse_prior",
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )

    @classmethod
    def from_fixed_z(
        cls,
        z_min: float,
        z_max: float,
        target_bracket_cr: tuple[float, float] = DEFAULT_TARGET_BRACKET_CR,
        robust: bool = False,
        mainboard_only_stats: bool = True,
    ) -> "TargetBracketCalibration":
        """Build a calibration from fixed explicit Z-score bounds."""
        low, high = normalize_bracket_bounds(*target_bracket_cr)
        if isinstance(z_min, bool) or isinstance(z_max, bool):
            raise ValueError("z_min and z_max must be finite numbers.")
        z_a, z_b = float(z_min), float(z_max)
        if not (math.isfinite(z_a) and math.isfinite(z_b)):
            raise ValueError("z_min and z_max must be finite numbers.")
        z_lo, z_hi = min(z_a, z_b), max(z_a, z_b)
        if z_hi <= z_lo:
            raise ValueError("z_max must be greater than z_min.")
        ref_log_scale = (math.log(high) - math.log(low)) / (z_hi - z_lo)
        ref_log_center = math.log(low) - ref_log_scale * z_lo
        return cls(
            target_min_cr=low,
            target_max_cr=high,
            ref_log_center=ref_log_center,
            ref_log_scale=ref_log_scale,
            z_min=z_lo,
            z_max=z_hi,
            source="fixed_z",
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_size_bracket(z_factor: float | None) -> str | None:
    """Map a cross-sectional Z-factor to a canonical market-cap bracket."""
    if z_factor is None:
        return None
    if z_factor >= 1.5:
        return "MEGA_CAP"
    if z_factor >= 0.5:
        return "LARGE_CAP"
    if z_factor >= -0.5:
        return "MID_CAP"
    if z_factor >= -1.5:
        return "SMALL_CAP"
    return "MICRO_CAP"


def calibrate_target_bracket(
    records: Sequence[Mapping[str, Any]],
    target_bracket_cr: tuple[float, float] = DEFAULT_TARGET_BRACKET_CR,
    reference_start: str | None = None,
    reference_end: str | None = None,
    reference_years: int = 3,
    value_field: str = "market_cap_cr",
    date_field: str = "trade_date",
    robust: bool = False,
    mainboard_only_stats: bool = True,
) -> TargetBracketCalibration:
    """Calibrate Z-score bracket bounds from a recent multi-stock NSE reference window."""
    if reference_years < 1:
        raise ValueError("reference_years must be >= 1.")
    low, high = normalize_bracket_bounds(*target_bracket_cr)

    grouped: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for rec in records:
        date_key = _clean_date_key(rec.get(date_field))
        if not date_key:
            continue
        raw_val = _parse_valid_number(rec.get(value_field), require_positive=True)
        if raw_val is None:
            continue
        series = _extract_series(rec)
        grouped[date_key].append((raw_val, series))

    if not grouped:
        raise ValueError(
            "Insufficient multi-stock reference-window data to calibrate target bracket; "
            "ingest recent market-cap data or use TargetBracketCalibration.default_nse()."
        )

    all_dates = sorted(grouped)
    ref_end = reference_end.strip() if reference_end else None
    ref_start = reference_start.strip() if reference_start else None

    if ref_end is None:
        ref_end = all_dates[-1]
    if ref_start is None:
        end_dt = date.fromisoformat(ref_end[:10])
        try:
            start_dt = end_dt.replace(year=end_dt.year - reference_years)
        except ValueError:
            start_dt = end_dt.replace(year=end_dt.year - reference_years, day=28)
        ref_start = start_dt.isoformat()

    mu_values: list[float] = []
    sigma_values: list[float] = []
    for date_key in all_dates:
        if not (ref_start <= date_key <= ref_end):
            continue
        items = grouped[date_key]
        pool = _select_stats_pool(items, mainboard_only_stats=mainboard_only_stats)
        xs = [math.log(val) for val, _ in pool]
        center, scale = _compute_distribution_stats(xs, robust=robust)
        if scale > 0:
            mu_values.append(center)
            sigma_values.append(scale)

    if not mu_values:
        raise ValueError(
            "Insufficient multi-stock reference-window data to calibrate target bracket; "
            "ingest recent market-cap data or use TargetBracketCalibration.default_nse()."
        )

    ref_log_center = statistics.median(mu_values)
    ref_log_scale = statistics.median(sigma_values)
    z_min = (math.log(low) - ref_log_center) / ref_log_scale
    z_max = (math.log(high) - ref_log_center) / ref_log_scale

    return TargetBracketCalibration(
        target_min_cr=low,
        target_max_cr=high,
        ref_log_center=ref_log_center,
        ref_log_scale=ref_log_scale,
        z_min=z_min,
        z_max=z_max,
        source="reference_window",
        reference_start=ref_start,
        reference_end=ref_end,
        reference_dates_count=len(mu_values),
        robust=robust,
        mainboard_only_stats=mainboard_only_stats,
    )


def compute_cross_sectional_z_factors(
    records: Sequence[Mapping[str, Any]],
    value_field: str = "market_cap_cr",
    date_field: str = "trade_date",
    log_transform: bool = True,
    robust: bool = False,
    anchor_date: str | None = None,
    target_bracket_cr: tuple[float, float] = DEFAULT_TARGET_BRACKET_CR,
    calibration: TargetBracketCalibration | None = None,
    mainboard_only_stats: bool = True,
    trailing_days: int = 1,
    z_buffer: float = 0.0,
) -> list[dict[str, Any]]:
    """Compute strictly point-in-time Z-scores, percentiles, brackets, and target-bracket matches.

    Each trade_date is standardized exclusively using the active cross-section observed
    on that exact date (or a causal trailing median over dates <= t when trailing_days > 1),
    preventing lookahead bias across 15+ year horizons.
    """
    if trailing_days < 1:
        raise ValueError("trailing_days must be >= 1.")
    if z_buffer < 0:
        raise ValueError("z_buffer must be >= 0.")
    if not records:
        return []

    if log_transform and calibration is None:
        calibration = TargetBracketCalibration.default_nse(
            target_bracket_cr=target_bracket_cr,
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )

    output: list[dict[str, Any]] = [
        {
            **dict(rec),
            "z_factor": None,
            "percentile": None,
            "mcap_rank": None,
            "size_bracket": None,
            "deflated_value": None,
            "equivalent_mcap_cr": None,
            "in_target_bracket": None,
            "target_bracket_distance_z": None,
            "implied_bracket_min_cr": None,
            "implied_bracket_max_cr": None,
        }
        for rec in records
    ]

    grouped_indices: dict[str, list[tuple[int, float, str]]] = defaultdict(list)
    for idx, rec in enumerate(records):
        date_key = _clean_date_key(rec.get(date_field))
        if not date_key:
            continue
        raw_val = _parse_valid_number(rec.get(value_field), require_positive=log_transform)
        if raw_val is None:
            continue
        grouped_indices[date_key].append((idx, raw_val, _extract_series(rec)))

    if not grouped_indices:
        return output

    date_medians: dict[str, float] = {}
    for date_key, items in grouped_indices.items():
        stats_items = _select_stats_pool_with_index(
            items, mainboard_only_stats=mainboard_only_stats
        )
        pos_values = [raw for _, raw, _ in stats_items if raw > 0]
        if not pos_values:
            pos_values = [raw for _, raw, _ in items if raw > 0]
        if pos_values:
            date_medians[date_key] = statistics.median(pos_values)

    sorted_dates = sorted(grouped_indices)
    anchor_median: float | None = None
    if anchor_date is not None:
        if anchor_date not in grouped_indices:
            raise ValueError(f"anchor_date {anchor_date!r} not found in valid dates.")
        anchor_median = date_medians.get(anchor_date)
    else:
        for d in sorted_dates:
            if d in date_medians and date_medians[d] > 0:
                anchor_median = date_medians[d]
                break

    history_centers: list[float] = []
    history_scales: list[float] = []

    for date_key in sorted_dates:
        items = grouped_indices[date_key]
        stats_items = _select_stats_pool_with_index(
            items, mainboard_only_stats=mainboard_only_stats
        )
        stats_xs = [
            math.log(raw) if log_transform else raw for _, raw, _ in stats_items
        ]
        center_t, scale_t = _compute_distribution_stats(stats_xs, robust=robust)
        history_centers.append(center_t)
        history_scales.append(scale_t)

        if trailing_days == 1:
            eff_center, eff_scale = center_t, scale_t
        else:
            eff_center = statistics.median(history_centers[-trailing_days:])
            eff_scale = statistics.median(history_scales[-trailing_days:])

        transformed = [
            (idx, raw, math.log(raw) if log_transform else raw) for idx, raw, _ in items
        ]
        z_values = [
            (x - eff_center) / eff_scale if eff_scale > 0 else 0.0
            for _, _, x in transformed
        ]
        percentiles = _compute_percentiles([x for _, _, x in transformed])
        ranks = _compute_descending_ranks([raw for _, raw, _ in items])
        date_median = date_medians.get(date_key)

        implied_min_cr: float | None = None
        implied_max_cr: float | None = None
        if log_transform and calibration is not None and eff_scale > 0:
            implied_min_cr = round(
                math.exp(eff_center + eff_scale * calibration.z_min), 4
            )
            implied_max_cr = round(
                math.exp(eff_center + eff_scale * calibration.z_max), 4
            )

        for (idx, raw, _), z_val, pct, rank in zip(
            transformed, z_values, percentiles, ranks
        ):
            z_rounded = round(z_val, 6)
            if z_rounded == 0:
                z_rounded = 0.0

            deflated: float | None = None
            if (
                anchor_median is not None
                and anchor_median > 0
                and date_median is not None
                and date_median > 0
                and raw > 0
            ):
                deflated = round(raw * (anchor_median / date_median), 6)

            output[idx]["z_factor"] = z_rounded
            output[idx]["percentile"] = round(pct, 4)
            output[idx]["mcap_rank"] = rank
            output[idx]["size_bracket"] = classify_size_bracket(z_rounded)
            output[idx]["deflated_value"] = deflated

            if log_transform and calibration is not None:
                equivalent_cr = round(
                    math.exp(
                        calibration.ref_log_center + calibration.ref_log_scale * z_val
                    ),
                    4,
                )
                in_bracket = (
                    (calibration.z_min - z_buffer)
                    <= z_val
                    <= (calibration.z_max + z_buffer)
                )
                if calibration.z_min <= z_val <= calibration.z_max:
                    dist_z = 0.0
                elif z_val < calibration.z_min:
                    dist_z = round(z_val - calibration.z_min, 6)
                else:
                    dist_z = round(z_val - calibration.z_max, 6)
                if dist_z == 0:
                    dist_z = 0.0

                output[idx]["equivalent_mcap_cr"] = equivalent_cr
                output[idx]["in_target_bracket"] = in_bracket
                output[idx]["target_bracket_distance_z"] = dist_z
                output[idx]["implied_bracket_min_cr"] = implied_min_cr
                output[idx]["implied_bracket_max_cr"] = implied_max_cr

    return output


def summarize_historical_brackets(
    records: Sequence[Mapping[str, Any]],
    value_field: str = "market_cap_cr",
    date_field: str = "trade_date",
    robust: bool = False,
    target_bracket_cr: tuple[float, float] = DEFAULT_TARGET_BRACKET_CR,
    calibration: TargetBracketCalibration | None = None,
    mainboard_only_stats: bool = True,
    trailing_days: int = 1,
    z_buffer: float = 0.0,
) -> list[dict[str, Any]]:
    """Summarize the equivalent rupee bracket and match counts for each historical date."""
    if calibration is None:
        calibration = TargetBracketCalibration.default_nse(
            target_bracket_cr=target_bracket_cr,
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )

    scored = compute_cross_sectional_z_factors(
        records,
        value_field=value_field,
        date_field=date_field,
        log_transform=True,
        robust=robust,
        target_bracket_cr=(calibration.target_min_cr, calibration.target_max_cr),
        calibration=calibration,
        mainboard_only_stats=mainboard_only_stats,
        trailing_days=trailing_days,
        z_buffer=z_buffer,
    )

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        date_key = _clean_date_key(row.get(date_field))
        if not date_key or row.get("z_factor") is None:
            continue
        by_date[date_key].append(row)

    schedule: list[dict[str, Any]] = []
    for date_key in sorted(by_date):
        day_rows = by_date[date_key]
        mainboard_raws: list[float] = []
        all_raws: list[float] = []
        for r in day_rows:
            val = float(r[value_field])
            all_raws.append(val)
            if _extract_series(r) not in SME_SERIES:
                mainboard_raws.append(val)

        median_pool = mainboard_raws if mainboard_raws else all_raws
        schedule.append(
            {
                "trade_date": date_key,
                "universe_count": len(day_rows),
                "mainboard_count": len(mainboard_raws),
                "median_mcap_cr": round(statistics.median(median_pool), 4),
                "implied_min_cr": day_rows[0]["implied_bracket_min_cr"],
                "implied_max_cr": day_rows[0]["implied_bracket_max_cr"],
                "z_min": round(calibration.z_min, 6),
                "z_max": round(calibration.z_max, 6),
                "matching_companies_count": sum(
                    1 for r in day_rows if r["in_target_bracket"] is True
                ),
                "nominal_naive_count": sum(
                    1
                    for r in day_rows
                    if calibration.target_min_cr
                    <= float(r[value_field])
                    <= calibration.target_max_cr
                ),
            }
        )
    return schedule


def recompute_store_z_factors(
    store: CompanyStore,
    value_field: str = "market_cap_cr",
    robust: bool = False,
    anchor_date: str | None = None,
    target_bracket_cr: tuple[float, float] = DEFAULT_TARGET_BRACKET_CR,
    calibration_mode: str = "nse_prior",
    reference_start: str | None = None,
    reference_end: str | None = None,
    reference_years: int = 3,
    z_min: float | None = None,
    z_max: float | None = None,
    mainboard_only_stats: bool = True,
    trailing_days: int = 1,
    z_buffer: float = 0.0,
    pit_universe_only: bool = True,
) -> dict[str, Any]:
    """Recompute and persist cross-sectional Z-factors and target-bracket flags on daily_market_data."""
    if calibration_mode not in ("nse_prior", "reference_window", "fixed_z"):
        raise ValueError(
            f"Invalid calibration_mode {calibration_mode!r}; expected 'nse_prior', 'reference_window', or 'fixed_z'."
        )

    rows = store.list_daily_market_rows_for_z(pit_universe_only=pit_universe_only)
    if not rows:
        return {
            "rows_updated": 0,
            "dates_processed": 0,
            "anchor_date_used": anchor_date,
            "robust": robust,
            "target_bracket_matches": 0,
            "calibration": None,
        }

    if calibration_mode == "nse_prior":
        calibration = TargetBracketCalibration.default_nse(
            target_bracket_cr=target_bracket_cr,
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )
    elif calibration_mode == "reference_window":
        calibration = calibrate_target_bracket(
            rows,
            target_bracket_cr=target_bracket_cr,
            reference_start=reference_start,
            reference_end=reference_end,
            reference_years=reference_years,
            value_field=value_field,
            date_field="trade_date",
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )
    else:
        if z_min is None or z_max is None:
            raise ValueError("calibration_mode='fixed_z' requires both z_min and z_max.")
        calibration = TargetBracketCalibration.from_fixed_z(
            z_min=z_min,
            z_max=z_max,
            target_bracket_cr=target_bracket_cr,
            robust=robust,
            mainboard_only_stats=mainboard_only_stats,
        )

    computed = compute_cross_sectional_z_factors(
        rows,
        value_field=value_field,
        date_field="trade_date",
        log_transform=True,
        robust=robust,
        anchor_date=anchor_date,
        target_bracket_cr=(calibration.target_min_cr, calibration.target_max_cr),
        calibration=calibration,
        mainboard_only_stats=mainboard_only_stats,
        trailing_days=trailing_days,
        z_buffer=z_buffer,
    )

    valid_dates = sorted(
        {
            str(r["trade_date"]).strip()
            for r in computed
            if r.get("z_factor") is not None and r.get("trade_date")
        }
    )
    anchor_used = (
        anchor_date
        if anchor_date is not None
        else (valid_dates[0] if valid_dates else None)
    )

    store.save_z_calibration(calibration.to_dict())

    updated_ids: set[int] = set()
    rows_updated = 0
    target_matches = 0
    with store._connection:
        for row in computed:
            if row.get("z_factor") is None:
                continue
            row_id = int(row["id"])
            updated_ids.add(row_id)
            in_bracket = 1 if row.get("in_target_bracket") else 0
            target_matches += in_bracket
            store._connection.execute(
                """
                UPDATE daily_market_data SET
                    z_factor_mcap = ?,
                    mcap_percentile = ?,
                    mcap_rank = ?,
                    size_bracket = ?,
                    deflated_mcap_cr = ?,
                    equivalent_mcap_cr = ?,
                    in_target_bracket = ?,
                    target_bracket_distance_z = ?,
                    implied_bracket_min_cr = ?,
                    implied_bracket_max_cr = ?
                WHERE id = ?
                """,
                (
                    row["z_factor"],
                    row["percentile"],
                    row["mcap_rank"],
                    row["size_bracket"],
                    row["deflated_value"],
                    row["equivalent_mcap_cr"],
                    in_bracket,
                    row["target_bracket_distance_z"],
                    row["implied_bracket_min_cr"],
                    row["implied_bracket_max_cr"],
                    row_id,
                ),
            )
            rows_updated += 1

        # Clear stale Z-factor/bracket values on excluded ghost rows or invalid rows.
        all_db_ids = {
            int(r["id"])
            for r in store._connection.execute(
                "SELECT id FROM daily_market_data WHERE z_factor_mcap IS NOT NULL OR in_target_bracket IS NOT NULL"
            ).fetchall()
        }
        for stale_id in all_db_ids - updated_ids:
            store._connection.execute(
                """
                UPDATE daily_market_data SET
                    z_factor_mcap = NULL,
                    mcap_percentile = NULL,
                    mcap_rank = NULL,
                    size_bracket = NULL,
                    deflated_mcap_cr = NULL,
                    equivalent_mcap_cr = NULL,
                    in_target_bracket = NULL,
                    target_bracket_distance_z = NULL,
                    implied_bracket_min_cr = NULL,
                    implied_bracket_max_cr = NULL
                WHERE id = ?
                """,
                (stale_id,),
            )

    return {
        "rows_updated": rows_updated,
        "dates_processed": len(valid_dates),
        "anchor_date_used": anchor_used,
        "robust": robust,
        "target_bracket_matches": target_matches,
        "calibration": calibration.to_dict(),
    }


def _clean_date_key(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _extract_series(rec: Mapping[str, Any]) -> str:
    val = rec.get("effective_series")
    if val is None:
        val = rec.get("series")
    return str(val or "").strip().upper()


def _select_stats_pool(
    items: Sequence[tuple[float, str]], *, mainboard_only_stats: bool
) -> list[tuple[float, str]]:
    if not mainboard_only_stats:
        return list(items)
    non_sme = [item for item in items if item[1] not in SME_SERIES]
    return non_sme if non_sme else list(items)


def _select_stats_pool_with_index(
    items: Sequence[tuple[int, float, str]], *, mainboard_only_stats: bool
) -> list[tuple[int, float, str]]:
    if not mainboard_only_stats:
        return list(items)
    non_sme = [item for item in items if item[2] not in SME_SERIES]
    return non_sme if non_sme else list(items)


def _compute_distribution_stats(
    xs: Sequence[float], *, robust: bool
) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    if robust:
        center = statistics.median(xs)
        mad = statistics.median(abs(x - center) for x in xs)
        scale = 1.4826 * mad
        if scale <= 0:
            mean_x = statistics.mean(xs)
            pstdev_x = statistics.pstdev(xs) if len(xs) > 1 else 0.0
            return mean_x, pstdev_x
        return center, scale
    center = statistics.mean(xs)
    scale = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return center, scale


def _parse_valid_number(value: Any, *, require_positive: bool) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    if require_positive and num <= 0:
        return None
    return num


def _compute_percentiles(values: Sequence[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [50.0]
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    percentiles = [0.0] * n
    pos = 0
    while pos < n:
        end = pos
        val = indexed[pos][1]
        while end + 1 < n and indexed[end + 1][1] == val:
            end += 1
        avg_rank = (pos + end) / 2.0
        pct = 100.0 * avg_rank / (n - 1)
        for k in range(pos, end + 1):
            percentiles[indexed[k][0]] = pct
        pos = end + 1
    return percentiles


def _compute_descending_ranks(values: Sequence[float]) -> list[int]:
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda item: item[1], reverse=True)
    ranks = [0] * n
    current_rank = 1
    for pos, (orig_idx, val) in enumerate(indexed):
        if pos > 0 and val != indexed[pos - 1][1]:
            current_rank = pos + 1
        ranks[orig_idx] = current_rank
    return ranks
