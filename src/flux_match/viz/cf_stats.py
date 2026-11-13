"""Hourly population standard deviation of capacity factors within each balancing area.

    uv run python -m flux_match.viz.cf_stats --type wind|solar [--year Y] \\
        [--wind-source coders|ninja]

Streams a capacity-factor CSV (one column per grid cell, one row per hour) and, for each hour,
computes the population standard deviation of capacity factors across all cells in each balancing
area. Output is a CSV with columns `h` and one column per balancing area (alphabetical), and one
row per hour 1-8760.

For `--type wind`, `--wind-source` chooses whether `wind_capacity_factor_<year>.csv` is read from
CODERS (`data/coders/`) or renewables.ninja (`data/ninja/`). Solar always comes from CODERS.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from flux_match import paths
from flux_match.errors import DataError, FluxMatchError
from flux_match.grid_cell_rankings import (
    _check_cells,
    _header_cells,
    iter_hourly_capacity_factors,
    read_grid_cells,
)
from flux_match.schema import HOURS_PER_YEAR

GENERATION_TYPES = ("wind", "solar")
WIND_SOURCES = ("coders", "ninja")
DEFAULT_YEAR = 2021
HOUR_COLUMN = "h"


def capacity_factor_path(generation_type: str, year: int, wind_source: str = "coders") -> Path:
    """Path to the hourly capacity-factor CSV for `generation_type` and `year`.

    Solar always comes from CODERS. Wind comes from CODERS or NINJA according to `wind_source`.
    """
    if wind_source not in WIND_SOURCES:
        raise ValueError(f"wind_source must be one of {list(WIND_SOURCES)}, got {wind_source!r}")
    if generation_type == "wind" and wind_source == "ninja":
        directory = paths.NINJA_DATA_DIR
    else:
        directory = paths.CODERS_DATA_DIR
    return directory / f"{generation_type}_capacity_factor_{year}.csv"


def output_path(generation_type: str, now: datetime) -> Path:
    return paths.OUTPUT_DIR / (
        f"cf-stats-{generation_type}-{now.strftime(paths.TIMESTAMP_FORMAT)}.csv"
    )


def population_std(values: Sequence[float]) -> float:
    """Population standard deviation (ddof=0). One value yields 0.0; empty is an error."""
    if not values:
        raise DataError("cannot compute standard deviation of an empty cell set")
    return statistics.pstdev(values)


def cells_by_balancing_area(cells: Sequence[str], areas: Sequence[str]) -> dict[str, list[int]]:
    """Map each balancing area to indices into `cells`, keyed in alphabetical BA order."""
    if len(cells) != len(areas):
        raise DataError(f"grid_cell_info: {len(cells)} grid cells but {len(areas)} balancing areas")
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, area in enumerate(areas):
        grouped[area].append(index)
    return {ba: grouped[ba] for ba in sorted(grouped)}


def require_matching_cells(cf_path: Path, grid_cells: Sequence[str]) -> None:
    """Hard-fail if the capacity-factor header and grid_cell_info disagree on cell ids."""
    if not cf_path.is_file():
        raise DataError(f"Input file not found: {cf_path}")
    with cf_path.open(newline="") as f:
        header = next(csv.reader(f), None)
    cf_cells = _header_cells(header, cf_path.name)
    _check_cells(cf_path.name, cf_cells, grid_cells)


def write_ba_hourly_std(
    cf_path: Path,
    grid_path: Path,
    dest: Path,
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> None:
    """Write hourly BA population stds for `cf_path` to `dest`."""
    cells, areas = read_grid_cells(grid_path)
    require_matching_cells(cf_path, cells)
    by_ba = cells_by_balancing_area(cells, areas)
    ba_names = list(by_ba)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow([HOUR_COLUMN, *ba_names])
        for hour, values in enumerate(
            iter_hourly_capacity_factors(cf_path, cells, hours_in_year=hours_in_year),
            start=1,
        ):
            row: list[object] = [hour]
            for ba in ba_names:
                row.append(population_std([values[i] for i in by_ba[ba]]))
            writer.writerow(row)


def build(
    generation_type: str,
    year: int,
    dest: Path,
    wind_source: str = "coders",
) -> None:
    if generation_type not in GENERATION_TYPES:
        raise ValueError(f"generation_type must be one of {list(GENERATION_TYPES)}")
    cf_path = capacity_factor_path(generation_type, year, wind_source)
    grid_path = paths.CODERS_DATA_DIR / "grid_cell_info.csv"
    print(f"Reading {generation_type} capacity factors for {year} from {cf_path}...")
    write_ba_hourly_std(cf_path, grid_path, dest)
    print(f"Wrote {dest}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", dest="generation_type", required=True, choices=GENERATION_TYPES)
    parser.add_argument(
        "--year", type=int, default=DEFAULT_YEAR, help="capacity-factor year (default: %(default)s)"
    )
    parser.add_argument(
        "--wind-source",
        choices=WIND_SOURCES,
        default="coders",
        help="for --type wind: which data folder supplies wind_capacity_factor_<year>.csv "
        "(default: %(default)s)",
    )
    parser.add_argument("--out", type=Path, help="output CSV file (default: under output/)")
    args = parser.parse_args(argv)
    try:
        dest = args.out or output_path(args.generation_type, datetime.now())
        build(args.generation_type, args.year, dest, wind_source=args.wind_source)
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
