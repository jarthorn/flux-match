"""Greedy strategy: pick the best-ranked sites, capped per grid cell.

Which ranking is "best" is `ranking_metric` in [selection.greedy]: annual capacity factor
or median daily capacity. Cells are filled in that rank order (1 = highest), each up to
the per-cell cap, until the target is met. Capacity sizing and the per-cell cap are
described in base.py.
"""

from pathlib import Path

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError
from flux_match.grid_cell_rankings import read_cells_by_ba
from flux_match.run_context import RunContext
from flux_match.selection.base import (
    CapacityParams,
    Site,
    Strategy,
    select_per_balancing_area,
)

GRID_CELL_INFO = "grid_cell_info.csv"

# ranking_metric -> (wind rank column, solar rank column) in grid_cell_rankings.csv.
RANKING_METRICS = {
    "annual_capacity": ("ba_annual_wind_rank", "ba_annual_solar_rank"),
    "median_daily_capacity": ("ba_median_wind_rank", "ba_median_solar_rank"),
}


class GreedyStrategy(Strategy):
    def __init__(self, params: Config) -> None:
        self.capacity = CapacityParams.from_config(params)
        self.ranking_metric = _ranking_metric(params.section("greedy"))

    def select(self, ctx: RunContext) -> list[Site]:
        return select_sites(
            demand_path=paths.DATA_DIR / paths.BA_DEMAND_FILENAME,
            rankings_path=paths.DATA_DIR / paths.RANKINGS_FILENAME,
            grid_cell_info=paths.CODERS_DATA_DIR / GRID_CELL_INFO,
            capacity=self.capacity,
            ranking_metric=self.ranking_metric,
        )


def select_sites(
    *,
    demand_path: Path,
    rankings_path: Path,
    grid_cell_info: Path,
    capacity: CapacityParams,
    ranking_metric: str,
) -> list[Site]:
    cells_by_ba = read_cells_by_ba(grid_cell_info, rankings_path, *RANKING_METRICS[ranking_metric])
    return select_per_balancing_area(demand_path, cells_by_ba, capacity, fill_order=list)


def _ranking_metric(params: Config) -> str:
    value = params.get("ranking_metric", str)
    if value not in RANKING_METRICS:
        allowed = ", ".join(repr(m) for m in RANKING_METRICS)
        raise ConfigError(
            f"{params.source}: 'ranking_metric' must be one of [{allowed}], got {value!r}"
        )
    return value
