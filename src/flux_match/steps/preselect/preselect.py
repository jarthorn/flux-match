"""Step 2: derived inputs for site selection, written to the shared data/ folder.

run() calls one function per derivation. Further derivations are added as further calls.

The first derivation is data/grid_cell_rankings_<wind source>.csv, from grid_cell_info and the
capacity-factor tables (see grid_cell_rankings.py). It is rebuilt when missing or older than any
of its inputs. Wind capacity factors come from fetch.wind_source; solar always comes from CODERS.
Each wind source has its own file. The NINJA file can be built only once fetch has finished
downloading and collating that source, so a missing wind table is a hard failure rather than
rankings from an incomplete download. Exactly one capacity_factor_years entry is required.

The second is data/normalized_demand_hourly.csv, from every fetch.demand_years hourly demand
file (UTC hour-ending). Each balancing area's demand is divided by that area's average hourly
demand for the year, so the year's normalized hours sum to 8760. Those curves are then averaged,
unweighted, across the configured years. It is rebuilt when missing or older than any of those
hourly files.
"""

import math
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import DataError, report_data_error
from flux_match.grid_cell_rankings import write_grid_cell_rankings
from flux_match.io import iter_csv, write_csv_atomic
from flux_match.schema import BA_DEMAND_HOURLY_COLUMNS, HOURS_PER_YEAR, NORMALIZED_DEMAND_COLUMNS
from flux_match.steps.fetch.fetch import (
    GRID_CELL_TABLE,
    SOLAR_CF_TABLE,
    FetchParams,
    _up_to_date,
)


def run(config: Config) -> None:
    write_rankings(config)
    write_normalized_demand(config)


def write_rankings(config: Config) -> None:
    """Derive the rankings file for fetch.wind_source if it is missing or older than any input."""
    fetch_config = config.section("fetch")
    params = FetchParams.from_config(fetch_config)
    (year,) = params.capacity_factor_years
    grid_cell_info = paths.csv_path(paths.CODERS_DATA_DIR, GRID_CELL_TABLE)
    solar = paths.csv_path(paths.CODERS_DATA_DIR, SOLAR_CF_TABLE, year=year)
    wind = paths.wind_capacity_factor_path(fetch_config)
    dest = paths.grid_cell_rankings_path(params.wind_source)
    if _up_to_date(dest, [grid_cell_info, solar, wind]):
        return
    print(f"writing {dest.name}", flush=True)
    write_grid_cell_rankings(grid_cell_info, solar, wind, dest)


def write_normalized_demand(config: Config) -> None:
    """Derive the multi-year normalized demand curve if it is missing or older than any input.

    Each year is normalized on its own, per balancing area, before the years are averaged.
    Averaging the megawatts first would let a high-demand year dominate the shape.
    """
    years = FetchParams.from_config(config.section("fetch")).demand_years
    sources = [paths.ba_demand_hourly_path(year) for year in years]
    missing = [path.name for path in sources if not path.is_file()]
    if missing:
        raise DataError(f"{', '.join(missing)} not found; run `flux-match fetch` first")
    dest = paths.normalized_demand_hourly_path()
    if _up_to_date(dest, sources):
        return
    by_year = [_read_hourly_demand(path) for path in sources]
    _require_same_areas(years, by_year)
    averaged = {
        ba: average_normalized_demand(
            [
                normalize_hourly_demand(demand[ba], where=f"{ba} in {year}")
                for year, demand in zip(years, by_year, strict=True)
            ]
        )
        for ba in sorted(by_year[0])
    }
    print(f"writing {dest.name}", flush=True)
    write_csv_atomic(dest, NORMALIZED_DEMAND_COLUMNS, _normalized_rows(averaged))


def normalize_hourly_demand(demand_mw: Sequence[float], *, where: str) -> list[float]:
    """Divide each hour by the mean hourly demand. The result sums to the number of hours."""
    if len(demand_mw) != HOURS_PER_YEAR:
        raise DataError(f"{where}: expected {HOURS_PER_YEAR} hours, got {len(demand_mw)}")
    total = math.fsum(demand_mw)
    if not math.isfinite(total) or total <= 0:
        raise DataError(f"{where}: average hourly demand is not positive: {total / HOURS_PER_YEAR}")
    mean = total / HOURS_PER_YEAR
    return [hour / mean for hour in demand_mw]


def average_normalized_demand(curves: Sequence[Sequence[float]]) -> list[float]:
    """Unweighted mean, hour by hour, of normalized curves that share one balancing area."""
    if not curves:
        raise DataError("no normalized demand curves to average")
    width = len(curves[0])
    if any(len(curve) != width for curve in curves):
        raise DataError("normalized demand curves have different lengths")
    count = len(curves)
    return [math.fsum(curve[hour] for curve in curves) / count for hour in range(width)]


def _read_hourly_demand(path: Path) -> dict[str, list[float]]:
    """demand_mw for hours 1..8760 per balancing area.

    Each area must appear once, as one contiguous block of hours 1..HOURS_PER_YEAR.
    """
    name = path.name
    by_area: dict[str, list[float]] = {}
    current: str | None = None
    values: list[float] = []
    for line, row in enumerate(iter_csv(path, BA_DEMAND_HOURLY_COLUMNS), start=2):
        ba = row["balancing_area"].strip()
        if not ba:
            raise DataError(f"{name}:{line}: blank balancing_area")
        if ba != current:
            _store_area(name, by_area, current, values)
            if ba in by_area:
                raise DataError(f"{name}:{line}: {ba} rows are not contiguous")
            current = ba
            values = []
        hour = _parse_hour(row["hour"], name, line)
        if hour != len(values) + 1:
            raise DataError(f"{name}:{line}: {ba}: expected hour {len(values) + 1}, got {hour}")
        values.append(_parse_demand(row["demand_mw"], name, line))
    if current is None:
        raise DataError(f"{name}: no rows")
    _store_area(name, by_area, current, values)
    return by_area


def _store_area(
    name: str, by_area: dict[str, list[float]], ba: str | None, values: list[float]
) -> None:
    if ba is None:
        return
    if len(values) != HOURS_PER_YEAR:
        raise DataError(f"{name}: {ba} has {len(values)} hours, expected {HOURS_PER_YEAR}")
    by_area[ba] = values


def _require_same_areas(
    years: Sequence[int], demand_by_ba_by_year: Sequence[Mapping[str, Sequence[float]]]
) -> None:
    """Validate that all balancing_area_demand_hourly files have the same balancing areas"""
    baseline_name = paths.ba_demand_hourly_filename(years[0])
    baseline = set(demand_by_ba_by_year[0])
    for year, demand in zip(years[1:], demand_by_ba_by_year[1:], strict=True):
        areas = set(demand)
        if areas == baseline:
            continue
        missing = sorted(baseline - areas)
        extra = sorted(areas - baseline)
        raise DataError(
            f"{paths.ba_demand_hourly_filename(year)} balancing areas differ from "
            f"{baseline_name}: missing {missing}, extra {extra}"
        )


def _normalized_rows(averaged: Mapping[str, Sequence[float]]) -> Iterator[dict[str, object]]:
    for ba, values in averaged.items():
        for hour, value in enumerate(values, start=1):
            yield {
                "balancing_area": ba,
                "hour": hour,
                "normalized_demand": value,
            }


def _parse_hour(text: str, name: str, line: int) -> int:
    try:
        return int(text)
    except ValueError:
        raise DataError(f"{name}:{line}: hour is not an integer: {text!r}") from None


def _parse_demand(text: str, name: str, line: int) -> float:
    try:
        value = float(text)
    except ValueError:
        raise DataError(f"{name}:{line}: demand_mw is not a number: {text!r}") from None
    if not math.isfinite(value):
        raise DataError(f"{name}:{line}: invalid demand_mw {text!r}")
    if value < 0:
        # Kept, so the normalized year still sums to 8760. A negative hour is a data-quality
        # problem to follow up, not a reason to drop the hour or refuse the curve.
        report_data_error(f"{name}:{line}: demand_mw is negative: {value}")
    return value
