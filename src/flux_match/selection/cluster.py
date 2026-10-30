"""Cluster strategy: spread sites across geographic clusters within each balancing area.

Nearby cells are assumed to have correlated output. So each balancing area's cells are split
into `cluster_count` clusters by K-means on their location (kmeans.py), and sites are picked in
rounds: each round takes the best remaining cell, by annual capacity factor rank, from every
cluster, best first. Rounds repeat until the capacity target is met. Capacity is then placed
in that order as described in base.py, so a balancing area that needs only one site gets the
same site as the greedy strategy. Wind and solar share the clusters.

The clusters are written to clusters.csv in the run folder, one row per grid cell, with
cluster_id 1..K within each balancing area.

Parameters, in [selection.cluster]: `cluster_count` (K), and `seed` and `n_init` for K-means,
so that runs are reproducible.
"""

from collections.abc import Mapping, Sequence
from itertools import zip_longest
from pathlib import Path

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError
from flux_match.grid_cell_rankings import RankedCell, read_cells_by_ba
from flux_match.io import write_csv
from flux_match.run_context import RunContext
from flux_match.selection.base import (
    CapacityParams,
    Site,
    Strategy,
    select_per_balancing_area,
)
from flux_match.selection.kmeans import GeoKMeans, GeoPoint

GRID_CELL_INFO = "grid_cell_info.csv"
ANNUAL_RANK_FIELDS = ("ba_annual_wind_rank", "ba_annual_solar_rank")
CLUSTER_COLUMNS = (
    "grid_cell",
    "balancing_area",
    "cluster_id",
    "latitude",
    "longitude",
    "annual_wind_capacity_factor",
    "annual_solar_capacity_factor",
)
# sklearn's random_state must fit in 32 bits.
MAX_SEED = 2**32 - 1


class ClusterStrategy(Strategy):
    def __init__(self, params: Config) -> None:
        self.capacity = CapacityParams.from_config(params)
        own = params.section("cluster")
        self.kmeans = GeoKMeans(
            k=_int_param(own, "cluster_count", 1),
            seed=_int_param(own, "seed", 0, MAX_SEED),
            n_init=_int_param(own, "n_init", 1),
        )

    def select(self, ctx: RunContext) -> list[Site]:
        return select_sites(
            demand_path=paths.DATA_DIR / paths.BA_DEMAND_FILENAME,
            rankings_path=paths.DATA_DIR / paths.RANKINGS_FILENAME,
            grid_cell_info=paths.CODERS_DATA_DIR / GRID_CELL_INFO,
            clusters_path=ctx.clusters_path,
            capacity=self.capacity,
            kmeans=self.kmeans,
        )


def select_sites(
    *,
    demand_path: Path,
    rankings_path: Path,
    grid_cell_info: Path,
    clusters_path: Path,
    capacity: CapacityParams,
    kmeans: GeoKMeans,
) -> list[Site]:
    cells_by_ba = read_cells_by_ba(grid_cell_info, rankings_path, *ANNUAL_RANK_FIELDS)
    cluster_of: dict[int, int] = {}
    for ba, cells in sorted(cells_by_ba.items()):
        points = [GeoPoint(c.grid_cell, c.latitude, c.longitude) for c in cells]
        for cluster_id, members in enumerate(kmeans.cluster(points, ba), start=1):
            cluster_of.update(dict.fromkeys(members, cluster_id))
    write_clusters(clusters_path, cells_by_ba, cluster_of)
    return select_per_balancing_area(
        demand_path,
        cells_by_ba,
        capacity,
        fill_order=lambda ranked: round_robin_order(ranked, cluster_of),
    )


def round_robin_order(ranked: Sequence[int], cluster_of: Mapping[int, int]) -> list[int]:
    """Cells in fill order: round r takes the r-th best cell of every cluster, best first.

    This is the same as repeatedly taking the best candidate and dropping the rest of its
    cluster from the candidates, refilling the candidates when they run out.
    """
    position = {cell: i for i, cell in enumerate(ranked)}
    # Map of cluster_id to cells in that cluster sorted by rank
    by_cluster: dict[int, list[int]] = {}
    for cell in ranked:
        by_cluster.setdefault(cluster_of[cell], []).append(cell)
    order: list[int] = []
    for round_cells in zip_longest(*by_cluster.values()):
        order.extend(sorted((c for c in round_cells if c is not None), key=position.__getitem__))
    return order


def write_clusters(
    path: Path, cells_by_ba: Mapping[str, Sequence[RankedCell]], cluster_of: Mapping[int, int]
) -> None:
    rows = sorted(
        (
            {
                "grid_cell": c.grid_cell,
                "balancing_area": ba,
                "cluster_id": cluster_of[c.grid_cell],
                "latitude": c.latitude,
                "longitude": c.longitude,
                "annual_wind_capacity_factor": c.annual_wind_capacity_factor,
                "annual_solar_capacity_factor": c.annual_solar_capacity_factor,
            }
            for ba, cells in cells_by_ba.items()
            for c in cells
        ),
        key=lambda row: row["grid_cell"],
    )
    write_csv(path, CLUSTER_COLUMNS, rows)


def _int_param(params: Config, key: str, minimum: int, maximum: int | None = None) -> int:
    value = params.get(key, int)
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"in {minimum}..{maximum}"
        raise ConfigError(f"{params.source}: '{key}' must be {bound}, got {value}")
    return value
