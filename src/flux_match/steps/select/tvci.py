"""TVCI strategy: pick sites whose hourly output complements the sites already picked, by the
Total Variation Complementarity Index.

See notes/tvci-approach.md. Each balancing area and generation type is handled on its
own. With N = ceil(target / per-cell cap) sites needed, the candidates are the area's top
ceil(candidate_multiple * N) cells by annual capacity factor rank. The best-ranked candidate is
picked first. Each later pick is the remaining candidate with the highest Total Variation
Complementarity Index (total_variation.py) against the sum of hourly capacity factors of the cells
picked so far; ties go to the better-ranked cell. Picking stops after N cells.

Capacity is then placed as described in base.py, in pick order followed by the area's other
cells in rank order, so the last pick takes any partial capacity. With candidate_multiple = 1
the cells used are the same as the greedy strategy's (by annual capacity), though the partial
one may differ.

Parameters, in [selection.tvci]: `candidate_multiple` (>= 1).
"""

import math
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.grid_cell_rankings import iter_hourly_capacity_factors, read_cells_by_ba
from flux_match.run_context import RunContext
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.fetch.fetch import GRID_CELL_TABLE, SOLAR_CF_TABLE, FetchParams
from flux_match.steps.select.base import (
    SELECTED_TYPES,
    Allocation,
    CapacityParams,
    GenerationType,
    Site,
    Strategy,
    allocate_in_order,
    plan_allocations,
)
from flux_match.steps.select.total_variation import (
    total_variation,
    total_variation_complementarity,
)

ANNUAL_RANK_FIELDS = ("ba_annual_wind_rank", "ba_annual_solar_rank")


class TvciStrategy(Strategy):
    def __init__(self, params: Config) -> None:
        self.capacity = CapacityParams.from_config(params)
        self.candidate_multiple = _candidate_multiple(params.section("tvci"))

    def select(self, ctx: RunContext) -> list[Site]:
        fetch_config = ctx.config.section("fetch")
        (year,) = FetchParams.from_config(fetch_config).capacity_factor_years
        return select_sites(
            demand_path=paths.ba_demand_path(),
            rankings_path=paths.grid_cell_rankings(ctx),
            grid_cell_info=paths.csv_path(paths.CODERS_DATA_DIR, GRID_CELL_TABLE),
            capacity_factor_paths={
                # Onshore and offshore wind share one table, from fetch.wind_source.
                GenerationType.WIND_ONSHORE: paths.wind_capacity_factor_path(fetch_config),
                GenerationType.SOLAR: paths.csv_path(
                    paths.CODERS_DATA_DIR, SOLAR_CF_TABLE, year=year
                ),
            },
            capacity=self.capacity,
            candidate_multiple=self.candidate_multiple,
        )


def select_sites(
    *,
    demand_path: Path,
    rankings_path: Path,
    grid_cell_info: Path,
    capacity_factor_paths: Mapping[GenerationType, Path],
    capacity: CapacityParams,
    candidate_multiple: float,
) -> list[Site]:
    """`capacity_factor_paths` maps each of SELECTED_TYPES to its hourly capacity-factor table.

    Each table is streamed once, for the candidates of every balancing area. A table is not
    read when no area has a target for its generation type.
    """
    cells_by_ba = read_cells_by_ba(grid_cell_info, rankings_path, *ANNUAL_RANK_FIELDS)
    allocations = plan_allocations(demand_path, cells_by_ba, capacity)
    sites: list[Site] = []
    for generation_type in SELECTED_TYPES:
        allocations_of_type = [a for a in allocations if a.generation_type is generation_type]
        counts = [sites_needed(a) for a in allocations_of_type]
        candidate_cell_pools = [
            a.ranked[: candidate_pool_size(n, len(a.ranked), candidate_multiple)]
            for a, n in zip(allocations_of_type, counts, strict=True)
        ]
        candidate_cells = [c for pool in candidate_cell_pools for c in pool]
        index, profiles = read_profiles(capacity_factor_paths[generation_type], candidate_cells)
        for allocation, count, pool in zip(
            allocations_of_type, counts, candidate_cell_pools, strict=True
        ):
            try:
                picked = tvci_order(pool, profiles[[index[c] for c in pool]], min(count, len(pool)))
            except DataError as e:
                raise DataError(
                    f"{allocation.balancing_area} {allocation.generation_type}: {e}"
                ) from e
            order = picked + allocation.ranked[len(pool) :]
            sites.extend(allocate_in_order(allocation, order))
    return sites


def sites_needed(allocation: Allocation) -> int:
    """N: the number of cells that the target fills at the configured per-cell cap."""
    return math.ceil(allocation.target_mw / allocation.max_mw)


def candidate_pool_size(sites: int, cells: int, candidate_multiple: float) -> int:
    """ceil(candidate_multiple * sites), but no more than the `cells` available."""
    return min(cells, math.ceil(candidate_multiple * sites))


def read_profiles(path: Path, cells: Sequence[int]) -> tuple[dict[int, int], NDArray[np.float64]]:
    """Hourly capacity factors of `cells` from one table, one row per cell.

    Returns the row of each cell and an array of shape (len(cells), HOURS_PER_YEAR). With no
    cells the file is not read.
    """
    if not cells:
        return {}, np.empty((0, HOURS_PER_YEAR))
    hourly = iter_hourly_capacity_factors(path, [str(c) for c in cells])
    profiles = np.array(list(hourly), dtype=np.float64).T
    return {cell: row for row, cell in enumerate(cells)}, profiles


def tvci_order(
    cells: Sequence[int], capacity_factors: NDArray[np.float64], count: int
) -> list[int]:
    """The first `count` cells to fill, in pick order.

    `cells` are best-ranked first, and row i of `capacity_factors` is the time series of
    cells[i]. The first pick is cells[0]. Each later pick is the remaining cell with the
    highest complementarity index against the mean series of the cells picked so far, with
    ties going to the earlier cell.
    """
    if capacity_factors.ndim != 2 or capacity_factors.shape[0] != len(cells):
        raise ValueError(
            f"capacity_factors must have one row per cell ({len(cells)}), "
            f"got shape {capacity_factors.shape}"
        )
    if not 0 <= count <= len(cells):
        raise ValueError(f"count must be in 0..{len(cells)}, got {count}")
    if count == 0:
        return []
    constant = total_variation(capacity_factors) == 0.0
    remaining = list(range(1, len(cells)))
    picked = [0]
    total = capacity_factors[0].copy()
    while len(picked) < count:
        if total_variation(total) == 0.0 and constant[remaining].any():
            raise DataError(
                "complementarity index is undefined: the mean of grid cells "
                f"{[cells[i] for i in picked]} and candidate grid cells "
                f"{[cells[i] for i in remaining if constant[i]]} are all constant"
            )
        scores = total_variation_complementarity(total, capacity_factors[remaining])
        best = remaining.pop(int(np.argmax(scores)))
        picked.append(best)
        total += capacity_factors[best]
    return [cells[i] for i in picked]


def _candidate_multiple(params: Config) -> float:
    value = params.get("candidate_multiple", float)
    if not math.isfinite(value) or value < 1:
        raise ConfigError(
            f"{params.source}: 'candidate_multiple' must be finite and >= 1, got {value}"
        )
    return value
