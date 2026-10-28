"""Per-cell summaries and per-balancing-area rankings of wind and solar capacity factors.

Builds `grid_cell_rankings.csv`, one row per grid cell in grid_cell_info.csv, from the hourly
capacity-factor tables. Those tables have one row per hour (column `h`) and one column per grid
cell, and are hundreds of MB, so they are streamed one row at a time. Only the per-cell totals
and per-day sums (365 values per cell) are kept in memory.

Daily capacity is the sum of a day's 24 hourly capacity factors, so it ranges from 0 to 24 and
is in the same units as the annual sum. Day `d` is hours 24*(d-1)+1 to 24*d of the 8760-hour year.

Ranks start at 1 for the highest value within the cell's balancing area. Ties are broken by
the cell's position in grid_cell_info.csv, so every rank within a balancing area is unique.

Any malformed value, or any disagreement between the cell lists of the input files, is a hard
failure and no output file is written.
"""

import csv
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from flux_match.errors import DataError
from flux_match.io import read_csv, write_csv_atomic
from flux_match.schema import HOURS_PER_YEAR

HOURS_PER_DAY = 24
DAYS_PER_YEAR = HOURS_PER_YEAR // HOURS_PER_DAY

RANKINGS_COLUMNS = (
    "grid_cell",
    "annual_wind_capacity_factor",
    "annual_solar_capacity_factor",
    "ba_median_daily_solar_capacity",
    "ba_median_daily_wind_capacity",
    "ba_annual_solar_rank",
    "ba_annual_wind_rank",
    "ba_median_solar_rank",
    "ba_median_wind_rank",
)

HOUR_COLUMN = "h"


@dataclass(frozen=True)
class CapacitySummary:
    """Per-cell totals from one capacity-factor table, in the table's column order."""

    cells: tuple[str, ...]
    annual: tuple[float, ...]
    median_daily: tuple[float, ...]


def summarize_capacity_factors(path: Path) -> CapacitySummary:
    """Stream a capacity-factor table, returning each cell's annual sum and median daily sum."""
    cells = capacity_factor_cells(path)
    annual = [0.0] * len(cells)
    day_sums = [0.0] * len(cells)
    daily: list[list[float]] = []
    hours = 0
    for hours, values in enumerate(iter_hourly_capacity_factors(path, cells), start=1):
        for i, v in enumerate(values):
            annual[i] += v
            day_sums[i] += v
        if hours % HOURS_PER_DAY == 0:
            daily.append(day_sums)
            day_sums = [0.0] * len(cells)

    if len(daily) != DAYS_PER_YEAR:
        raise AssertionError(f"{path.name}: {len(daily)} days from {hours} hours")
    median_daily = tuple(statistics.median(day) for day in zip(*daily, strict=True))
    return CapacitySummary(cells, tuple(annual), median_daily)


def capacity_factor_cells(path: Path) -> tuple[str, ...]:
    """Grid-cell ids from a capacity-factor header, in column order."""
    name = path.name
    if not path.is_file():
        raise DataError(f"Input file not found: {path}")
    with path.open(newline="") as f:
        header = next(csv.reader(f), None)
    return _header_cells(header, name)


def iter_hourly_capacity_factors(
    path: Path,
    cells: Sequence[str],
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> Iterator[list[float]]:
    """Yield `cells` capacity factors for each hour, in that order.

    Extra columns in the file are ignored. A requested cell missing from the header is a
    hard failure. Values must be in [0, 1].
    """
    name = path.name
    if not path.is_file():
        raise DataError(f"Input file not found: {path}")
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        file_cells = _header_cells(header, name)
        index = {cell: i for i, cell in enumerate(file_cells)}
        missing = [cell for cell in dict.fromkeys(cells) if cell not in index]
        if missing:
            raise DataError(
                f"{name}: missing grid cell columns "
                f"{_abbreviate(sorted(missing, key=_cell_sort_key))}"
            )
        columns = [index[cell] for cell in cells]
        hours = 0
        for hours, row in enumerate(reader, start=1):
            if hours > hours_in_year:
                raise DataError(f"{name}: more than {hours_in_year} hourly rows")
            yield _parse_selected_hour(row, hours, file_cells, columns, cells, name)
        if hours != hours_in_year:
            raise DataError(f"{name}: expected {hours_in_year} hourly rows, got {hours}")


def _header_cells(header: list[str] | None, name: str) -> tuple[str, ...]:
    if not header or header[0] != HOUR_COLUMN:
        got = header[:1] if header else header
        raise DataError(f"{name}: first column must be '{HOUR_COLUMN}', got {got!r}")
    cells = tuple(header[1:])
    if len(set(cells)) != len(cells):
        duplicates = sorted({c for c in cells if cells.count(c) > 1})
        raise DataError(f"{name}: duplicate grid cell columns {duplicates}")
    return cells


def _parse_selected_hour(
    row: list[str],
    hour: int,
    file_cells: Sequence[str],
    columns: Sequence[int],
    cells: Sequence[str],
    name: str,
) -> list[float]:
    if len(row) != len(file_cells) + 1:
        raise DataError(
            f"{name}: row for hour {hour} has {len(row)} fields, expected {len(file_cells) + 1}"
        )
    if row[0] != str(hour):
        raise DataError(f"{name}: expected hour {hour} on row {hour}, got {row[0]!r}")
    values = []
    for cell, column in zip(cells, columns, strict=True):
        text = row[column + 1]
        try:
            value = float(text)
        except ValueError:
            raise DataError(
                f"{name}: hour {hour}, grid cell {cell}: not a number: {text!r}"
            ) from None
        if not 0.0 <= value <= 1.0:  # also rejects NaN
            raise DataError(
                f"{name}: hour {hour}, grid cell {cell}: capacity factor {text!r} is outside 0-1"
            )
        values.append(value)
    return values


def rank_within_groups(values: Sequence[float], groups: Sequence[str]) -> list[int]:
    """Rank each item within its group, 1 = highest value.

    Ties are broken by position, so ranks within a group are exactly 1..n.
    """
    members: dict[str, list[int]] = {}
    for i, group in enumerate(groups):
        members.setdefault(group, []).append(i)
    ranks = [0] * len(values)
    for indices in members.values():
        for rank, i in enumerate(sorted(indices, key=lambda i: (-values[i], i)), start=1):
            ranks[i] = rank
    return ranks


def build_rankings(
    grid_cells: Sequence[str],
    balancing_areas: Sequence[str],
    solar: CapacitySummary,
    wind: CapacitySummary,
) -> list[dict[str, object]]:
    """Rows for RANKINGS_COLUMNS, in grid_cells order. Inputs must cover the same cells."""
    _check_cells("solar_capacity_factor", solar.cells, grid_cells)
    _check_cells("wind_capacity_factor", wind.cells, grid_cells)
    solar_by_cell = dict(zip(solar.cells, range(len(solar.cells)), strict=True))
    wind_by_cell = dict(zip(wind.cells, range(len(wind.cells)), strict=True))
    si = [solar_by_cell[c] for c in grid_cells]
    wi = [wind_by_cell[c] for c in grid_cells]

    annual_solar = [solar.annual[i] for i in si]
    annual_wind = [wind.annual[i] for i in wi]
    median_solar = [solar.median_daily[i] for i in si]
    median_wind = [wind.median_daily[i] for i in wi]

    annual_solar_rank = rank_within_groups(annual_solar, balancing_areas)
    annual_wind_rank = rank_within_groups(annual_wind, balancing_areas)
    median_solar_rank = rank_within_groups(median_solar, balancing_areas)
    median_wind_rank = rank_within_groups(median_wind, balancing_areas)
    return [
        {
            "grid_cell": cell,
            "annual_wind_capacity_factor": annual_wind[n],
            "annual_solar_capacity_factor": annual_solar[n],
            "ba_median_daily_solar_capacity": median_solar[n],
            "ba_median_daily_wind_capacity": median_wind[n],
            "ba_annual_solar_rank": annual_solar_rank[n],
            "ba_annual_wind_rank": annual_wind_rank[n],
            "ba_median_solar_rank": median_solar_rank[n],
            "ba_median_wind_rank": median_wind_rank[n],
        }
        for n, cell in enumerate(grid_cells)
    ]


def _check_cells(name: str, cells: Sequence[str], expected: Sequence[str]) -> None:
    if set(cells) == set(expected):
        return
    missing = sorted(set(expected) - set(cells), key=_cell_sort_key)
    extra = sorted(set(cells) - set(expected), key=_cell_sort_key)
    raise DataError(
        f"{name} grid cells differ from grid_cell_info: "
        f"missing {_abbreviate(missing)}, extra {_abbreviate(extra)}"
    )


def _cell_sort_key(cell: str) -> tuple[int, str]:
    return (0, f"{int(cell):012d}") if cell.isdigit() else (1, cell)


def _abbreviate(items: Sequence[str], limit: int = 10) -> str:
    shown = ", ".join(items[:limit])
    return f"[{shown}{', ...' if len(items) > limit else ''}] ({len(items)} total)"


def read_grid_cells(path: Path) -> tuple[list[str], list[str]]:
    """Grid cell ids and their balancing areas, in file order."""
    rows = read_csv(path, ("grid_cell", "balancing_area"))
    cells = [r["grid_cell"].strip() for r in rows]
    areas = [r["balancing_area"].strip() for r in rows]
    if not cells:
        raise DataError(f"{path.name}: no grid cells")
    if "" in cells:
        raise DataError(f"{path.name}: blank grid_cell on row {cells.index('') + 1}")
    if len(set(cells)) != len(cells):
        duplicates = sorted({c for c in cells if cells.count(c) > 1}, key=_cell_sort_key)
        raise DataError(f"{path.name}: duplicate grid cells {_abbreviate(duplicates)}")
    if "" in areas:
        blank = [cells[i] for i, a in enumerate(areas) if not a]
        raise DataError(f"{path.name}: blank balancing_area for grid cells {_abbreviate(blank)}")
    return cells, areas


def write_grid_cell_rankings(
    grid_cell_info: Path,
    solar_capacity_factors: Path,
    wind_capacity_factors: Path,
    dest: Path,
) -> None:
    """Build `dest` from the three input files. Leaves no partial file on failure."""
    cells, areas = read_grid_cells(grid_cell_info)
    solar = summarize_capacity_factors(solar_capacity_factors)
    wind = summarize_capacity_factors(wind_capacity_factors)
    rows = build_rankings(cells, areas, solar, wind)
    write_csv_atomic(dest, RANKINGS_COLUMNS, rows)
