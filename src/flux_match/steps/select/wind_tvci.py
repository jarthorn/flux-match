"""Wind-TVCI strategy: greedy solar by annual capacity, then TVCI wind seeded by that solar.

Solar sites are filled in annual-capacity rank order (same as greedy with
ranking_metric=annual_capacity). Wind sites then use the Total Variation Complementarity
Index against the sum of hourly capacity factors of the solar cells already placed in the
same balancing area, plus wind cells picked so far. Every wind pick including the first is
scored against that running total. With no solar in an area, wind falls back to classic
TVCI (best-ranked first).

Wind candidate pools and placement match the tvci strategy. Parameters, in
[selection.wind-tvci]: `candidate_multiple` (>= 1).
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import DataError
from flux_match.grid_cell_rankings import read_cells_by_ba
from flux_match.run_context import RunContext
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.fetch.fetch import GRID_CELL_TABLE, SOLAR_CF_TABLE, FetchParams
from flux_match.steps.select.base import (
    CapacityParams,
    GenerationType,
    Site,
    Strategy,
    allocate_in_order,
    plan_allocations,
)
from flux_match.steps.select.tvci import (
    ANNUAL_RANK_FIELDS,
    candidate_multiple,
    candidate_pool_size,
    read_profiles,
    sites_needed,
    tvci_order,
)

SOLAR = GenerationType.SOLAR
WIND = GenerationType.WIND_ONSHORE


class WindTvciStrategy(Strategy):
    def __init__(self, params: Config) -> None:
        self.capacity = CapacityParams.from_config(params)
        self.candidate_multiple = candidate_multiple(params.section("wind-tvci"))

    def select(self, ctx: RunContext) -> list[Site]:
        fetch_config = ctx.config.section("fetch")
        (year,) = FetchParams.from_config(fetch_config).capacity_factor_years
        return select_sites(
            demand_path=paths.ba_demand_path(),
            rankings_path=paths.grid_cell_rankings(ctx),
            grid_cell_info=paths.csv_path(paths.CODERS_DATA_DIR, GRID_CELL_TABLE),
            capacity_factor_paths={
                WIND: paths.wind_capacity_factor_path(fetch_config),
                SOLAR: paths.csv_path(paths.CODERS_DATA_DIR, SOLAR_CF_TABLE, year=year),
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
    """Place solar greedily by annual rank, then wind by TVCI seeded with that solar."""
    cells_by_ba = read_cells_by_ba(grid_cell_info, rankings_path, *ANNUAL_RANK_FIELDS)
    allocations = plan_allocations(demand_path, cells_by_ba, capacity)

    solar_by_ba: dict[str, list[Site]] = {}
    sites: list[Site] = []
    for allocation in allocations:
        if allocation.generation_type is not SOLAR:
            continue
        placed = allocate_in_order(allocation, allocation.ranked)
        solar_by_ba[allocation.balancing_area] = placed
        sites.extend(placed)

    solar_cells = sorted({s.grid_cell for placed in solar_by_ba.values() for s in placed})
    solar_index, solar_profiles = read_profiles(capacity_factor_paths[SOLAR], solar_cells)

    wind_allocations = [a for a in allocations if a.generation_type is WIND]
    counts = [sites_needed(a) for a in wind_allocations]
    candidate_cell_pools = [
        a.ranked[: candidate_pool_size(n, len(a.ranked), candidate_multiple)]
        for a, n in zip(wind_allocations, counts, strict=True)
    ]
    candidate_cells = [c for pool in candidate_cell_pools for c in pool]
    wind_index, wind_profiles = read_profiles(capacity_factor_paths[WIND], candidate_cells)

    for allocation, count, pool in zip(wind_allocations, counts, candidate_cell_pools, strict=True):
        initial_total = _solar_total(
            solar_by_ba.get(allocation.balancing_area, ()), solar_index, solar_profiles
        )
        try:
            picked = tvci_order(
                pool,
                wind_profiles[[wind_index[c] for c in pool]],
                min(count, len(pool)),
                initial_total=initial_total,
            )
        except DataError as e:
            raise DataError(f"{allocation.balancing_area} {allocation.generation_type}: {e}") from e
        order = picked + allocation.ranked[len(pool) :]
        sites.extend(allocate_in_order(allocation, order))
    return sites


def _solar_total(
    solar_sites: Sequence[Site],
    index: Mapping[int, int],
    profiles: NDArray[np.float64],
) -> NDArray[np.float64] | None:
    """Sum of hourly CF profiles for selected solar cells, or None when there are none."""
    if not solar_sites:
        return None
    total = np.zeros(HOURS_PER_YEAR, dtype=np.float64)
    for site in solar_sites:
        total += profiles[index[site.grid_cell]]
    return total
