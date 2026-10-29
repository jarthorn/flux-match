"""Greedy strategy: pick the best-ranked sites, capped per grid cell.

For each balancing area, nameplate wind and solar capacity are a fraction of that area's
average load in the newest year of balancing_area_demand.csv:

    target_mw = annual_demand_mwh * ratio / HOURS_PER_YEAR

Which ranking is "best" is `ranking_metric` in [selection.greedy]: annual capacity factor
or median daily capacity. Cells are filled in that rank order (1 = highest), each up to
the per-cell cap, until the target is met. Wind is always onshore (wind_ons).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from flux_match import paths
from flux_match.balancing_area_demand import read_ba_demand
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.grid_cell_rankings import RANKINGS_COLUMNS, read_grid_cells
from flux_match.io import read_csv
from flux_match.run_context import RunContext
from flux_match.schema import HOURS_PER_YEAR
from flux_match.selection.base import GenerationType, Site, Strategy

GRID_CELL_INFO = "grid_cell_info.csv"

# ranking_metric -> (wind rank column, solar rank column) in grid_cell_rankings.csv.
RANKING_METRICS = {
    "annual_capacity": ("ba_annual_wind_rank", "ba_annual_solar_rank"),
    "median_daily_capacity": ("ba_median_wind_rank", "ba_median_solar_rank"),
}


@dataclass(frozen=True)
class RankedCell:
    grid_cell: int
    wind_rank: int
    solar_rank: int


class GreedyStrategy(Strategy):
    def __init__(self, params: Config) -> None:
        self.solar_capacity_ratio = _non_negative(params, "solar_capacity_ratio")
        self.wind_capacity_ratio = _non_negative(params, "wind_capacity_ratio")
        self.max_wind_capacity_mw_per_cell = _positive(params, "max_wind_capacity_mw_per_cell")
        self.max_solar_capacity_mw_per_cell = _positive(params, "max_solar_capacity_mw_per_cell")
        self.ranking_metric = _ranking_metric(params.section("greedy"))

    def select(self, ctx: RunContext) -> list[Site]:
        return select_sites(
            demand_path=paths.DATA_DIR / paths.BA_DEMAND_FILENAME,
            rankings_path=paths.DATA_DIR / paths.RANKINGS_FILENAME,
            grid_cell_info=paths.CODERS_DATA_DIR / GRID_CELL_INFO,
            wind_capacity_ratio=self.wind_capacity_ratio,
            solar_capacity_ratio=self.solar_capacity_ratio,
            max_wind_mw=self.max_wind_capacity_mw_per_cell,
            max_solar_mw=self.max_solar_capacity_mw_per_cell,
            ranking_metric=self.ranking_metric,
        )


def target_capacity_mw(demand_mwh: float, ratio: float) -> float:
    """Nameplate MW equal to `ratio` times average load."""
    return demand_mwh * ratio / HOURS_PER_YEAR


def allocate(
    cells: Sequence[int],
    target_mw: float,
    max_mw: float,
    generation_type: GenerationType,
    balancing_area: str,
) -> list[Site]:
    """Fill `cells` (best first) up to `max_mw` each until `target_mw` is placed."""
    if target_mw < 0:
        raise DataError(
            f"{balancing_area}: target {generation_type} capacity is negative: {target_mw}"
        )
    if target_mw == 0:
        return []
    remaining = target_mw
    sites: list[Site] = []
    for cell in cells:
        if remaining <= 0:
            break
        capacity = min(max_mw, remaining)
        sites.append(Site(cell, generation_type, capacity))
        remaining -= capacity
    if remaining > 0:
        raise DataError(
            f"{balancing_area}: not enough grid cells to place {target_mw:g} MW of "
            f"{generation_type}; {remaining:g} MW unallocated "
            f"({len(cells)} cells, max {max_mw:g} MW each)"
        )
    return sites


def select_sites(
    *,
    demand_path: Path,
    rankings_path: Path,
    grid_cell_info: Path,
    wind_capacity_ratio: float,
    solar_capacity_ratio: float,
    max_wind_mw: float,
    max_solar_mw: float,
    ranking_metric: str,
) -> list[Site]:
    _, demand = read_ba_demand(demand_path)
    cells_by_ba = read_cells_by_ba(grid_cell_info, rankings_path, ranking_metric)
    unknown = sorted(set(cells_by_ba) - set(demand))
    if unknown:
        raise DataError(f"grid cells in balancing areas with no demand data: {unknown}")

    sites: list[Site] = []
    for ba, demand_mwh in sorted(demand.items()):
        if demand_mwh < 0:
            raise DataError(f"{ba}: annual demand is negative: {demand_mwh}")
        cells = cells_by_ba.get(ba, ())
        wind_cells = [c.grid_cell for c in sorted(cells, key=lambda c: c.wind_rank)]
        solar_cells = [c.grid_cell for c in sorted(cells, key=lambda c: c.solar_rank)]
        sites.extend(
            allocate(
                wind_cells,
                target_capacity_mw(demand_mwh, wind_capacity_ratio),
                max_wind_mw,
                GenerationType.WIND_ONSHORE,
                ba,
            )
        )
        sites.extend(
            allocate(
                solar_cells,
                target_capacity_mw(demand_mwh, solar_capacity_ratio),
                max_solar_mw,
                GenerationType.SOLAR,
                ba,
            )
        )
    return sites


def read_cells_by_ba(
    grid_cell_info: Path, rankings_path: Path, ranking_metric: str
) -> dict[str, list[RankedCell]]:
    """Join rankings to balancing areas. Rankings must cover exactly the info-file cells."""
    wind_field, solar_field = RANKING_METRICS[ranking_metric]
    cells, areas = read_grid_cells(grid_cell_info)
    cell_to_ba = dict(zip(cells, areas, strict=True))
    ranking_rows = read_csv(rankings_path, RANKINGS_COLUMNS)
    ranking_cells = [row["grid_cell"].strip() for row in ranking_rows]
    if set(ranking_cells) != set(cells):
        missing = sorted(set(cells) - set(ranking_cells))
        extra = sorted(set(ranking_cells) - set(cells))
        raise DataError(
            f"{rankings_path.name} grid cells differ from {grid_cell_info.name}: "
            f"missing {missing}, extra {extra}"
        )

    cells_by_ba: dict[str, list[RankedCell]] = {}
    for row in ranking_rows:
        cell_id = row["grid_cell"].strip()
        try:
            grid_cell = int(cell_id)
        except ValueError:
            raise DataError(
                f"{rankings_path.name}: grid_cell {cell_id!r} is not an integer"
            ) from None
        ranked = RankedCell(
            grid_cell=grid_cell,
            wind_rank=_parse_rank(row[wind_field], wind_field, cell_id),
            solar_rank=_parse_rank(row[solar_field], solar_field, cell_id),
        )
        cells_by_ba.setdefault(cell_to_ba[cell_id], []).append(ranked)

    for ba, ranked_cells in cells_by_ba.items():
        _check_ranks(ba, "wind", [c.wind_rank for c in ranked_cells], rankings_path.name)
        _check_ranks(ba, "solar", [c.solar_rank for c in ranked_cells], rankings_path.name)
    return cells_by_ba


def _parse_rank(text: str, field: str, cell_id: str) -> int:
    try:
        rank = int(text)
    except ValueError:
        raise DataError(f"grid cell {cell_id}: {field} is not an integer: {text!r}") from None
    if rank < 1:
        raise DataError(f"grid cell {cell_id}: {field} must be >= 1, got {rank}")
    return rank


def _check_ranks(ba: str, kind: str, ranks: Sequence[int], source: str) -> None:
    expected = set(range(1, len(ranks) + 1))
    if set(ranks) == expected:
        return
    raise DataError(f"{source}: {ba} {kind} ranks must be 1..{len(ranks)}, got {sorted(ranks)}")


def _ranking_metric(params: Config) -> str:
    value = params.get("ranking_metric", str)
    if value not in RANKING_METRICS:
        allowed = ", ".join(repr(m) for m in RANKING_METRICS)
        raise ConfigError(
            f"{params.source}: 'ranking_metric' must be one of [{allowed}], got {value!r}"
        )
    return value


def _non_negative(params: Config, key: str) -> float:
    value = params.get(key, float)
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"{params.source}: '{key}' must be finite and >= 0, got {value}")
    return value


def _positive(params: Config, key: str) -> float:
    value = params.get(key, float)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{params.source}: '{key}' must be finite and > 0, got {value}")
    return value
