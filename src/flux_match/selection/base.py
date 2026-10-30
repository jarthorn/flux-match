"""Interface for wind and solar site-selection strategies, the selected-sites file format, and
the capacity sizing and placement that strategies share.

Every strategy returns a list of Site records; the step writes them with write_sites, so all
strategies produce the same file. The file is a sparse CSV with one row per grid cell and
generation type that has capacity:

    grid_cell,generation_type,capacity_mw
    1,solar,250.0
    1,wind_ons,1000.0

grid_cell is a grid_cell id from data/coders/grid_cell_info.csv. generation_type is one of
the GenerationType values. capacity_mw is positive and finite. A (grid_cell, generation_type)
pair appears at most once. Rows are sorted by grid_cell, then generation_type.

For each balancing area, nameplate wind and solar capacity are a fraction of that area's
average load in the newest year of balancing_area_demand.csv:

    target_mw = annual_demand_mwh * ratio / HOURS_PER_YEAR

A strategy decides the order in which a balancing area's cells are filled; allocate then fills
them in that order, each up to the per-cell cap, until the target is met. If the cap is too low
for the cells available, it is raised to at least target / cell count, rounded up so the target
can still be placed. Wind is always onshore (wind_ons).
"""

import math
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Self

from flux_match.balancing_area_demand import read_ba_demand
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.grid_cell_rankings import RankedCell
from flux_match.io import read_csv, write_csv
from flux_match.run_context import RunContext
from flux_match.schema import HOURS_PER_YEAR


class GenerationType(StrEnum):
    WIND_ONSHORE = "wind_ons"
    WIND_OFFSHORE = "wind_ofs"
    SOLAR = "solar"


@dataclass(frozen=True, order=True)
class Site:
    grid_cell: int
    generation_type: GenerationType
    capacity_mw: float

    def __post_init__(self) -> None:
        if not isinstance(self.generation_type, GenerationType):
            raise ValueError(f"generation_type must be a GenerationType: {self.generation_type!r}")
        if not math.isfinite(self.capacity_mw) or self.capacity_mw <= 0:
            raise ValueError(
                f"capacity_mw must be positive and finite for grid cell {self.grid_cell} "
                f"{self.generation_type}: {self.capacity_mw}"
            )


SITE_COLUMNS = tuple(f.name for f in fields(Site))


def _reject_duplicates(sites: list[Site], source: object) -> None:
    counts = Counter((s.grid_cell, s.generation_type) for s in sites)
    duplicates = sorted(k for k, n in counts.items() if n > 1)
    if duplicates:
        listed = ", ".join(f"grid cell {cell} {kind}" for cell, kind in duplicates)
        raise DataError(f"{source}: duplicate selected sites: {listed}")


def write_sites(path: Path, sites: Iterable[Site]) -> None:
    ordered = sorted(sites)
    _reject_duplicates(ordered, path)
    write_csv(path, SITE_COLUMNS, (asdict(s) for s in ordered))


def read_sites(path: Path) -> list[Site]:
    sites = []
    # Line 1 is the header.
    for line, row in enumerate(read_csv(path, SITE_COLUMNS), start=2):
        try:
            sites.append(
                Site(
                    grid_cell=int(row["grid_cell"]),
                    generation_type=GenerationType(row["generation_type"]),
                    capacity_mw=float(row["capacity_mw"]),
                )
            )
        except ValueError as e:
            raise DataError(f"{path}:{line}: {e}") from e
    _reject_duplicates(sites, path)
    return sites


class Strategy(ABC):
    """Parameters come from the scenario's [selection] table. Read and validate them
    all in __init__ so that config errors surface before any work starts. Strategy-specific
    keys live in [selection.<name>] when a strategy needs its own table."""

    @abstractmethod
    def __init__(self, params: Config) -> None: ...

    @abstractmethod
    def select(self, ctx: RunContext) -> list[Site]:
        """Return the selected sites. Omit cells and generation types with no capacity."""


@dataclass(frozen=True)
class CapacityParams:
    """The [selection] keys that size and cap capacity, shared by every strategy."""

    solar_capacity_ratio: float
    wind_capacity_ratio: float
    max_wind_capacity_mw_per_cell: float
    max_solar_capacity_mw_per_cell: float

    @classmethod
    def from_config(cls, params: Config) -> Self:
        return cls(
            solar_capacity_ratio=non_negative_param(params, "solar_capacity_ratio"),
            wind_capacity_ratio=non_negative_param(params, "wind_capacity_ratio"),
            max_wind_capacity_mw_per_cell=positive_param(params, "max_wind_capacity_mw_per_cell"),
            max_solar_capacity_mw_per_cell=positive_param(params, "max_solar_capacity_mw_per_cell"),
        )

    def ratio(self, generation_type: GenerationType) -> float:
        if generation_type is GenerationType.SOLAR:
            return self.solar_capacity_ratio
        return self.wind_capacity_ratio

    def max_mw(self, generation_type: GenerationType) -> float:
        if generation_type is GenerationType.SOLAR:
            return self.max_solar_capacity_mw_per_cell
        return self.max_wind_capacity_mw_per_cell


# Wind is always onshore.
SELECTED_TYPES = (GenerationType.WIND_ONSHORE, GenerationType.SOLAR)


def rank(cell: RankedCell, generation_type: GenerationType) -> int:
    if generation_type is GenerationType.SOLAR:
        return cell.solar_rank
    return cell.wind_rank


def select_per_balancing_area(
    demand_path: Path,
    cells_by_ba: Mapping[str, Sequence[RankedCell]],
    capacity: CapacityParams,
    fill_order: Callable[[list[int]], list[int]],
) -> list[Site]:
    """Allocate each balancing area's wind and solar target.

    `fill_order` is given the area's grid cells best-ranked first, for one generation type,
    and returns them in the order to fill.
    """
    _, demand = read_ba_demand(demand_path)
    unknown = sorted(set(cells_by_ba) - set(demand))
    if unknown:
        raise DataError(f"grid cells in balancing areas with no demand data: {unknown}")

    sites: list[Site] = []
    for ba, demand_mwh in sorted(demand.items()):
        if demand_mwh < 0:
            raise DataError(f"{ba}: annual demand is negative: {demand_mwh}")
        cells = cells_by_ba.get(ba, ())
        for generation_type in SELECTED_TYPES:
            ranked = [c.grid_cell for c in sorted(cells, key=lambda c: rank(c, generation_type))]
            sites.extend(
                allocate(
                    fill_order(ranked),
                    target_capacity_mw(demand_mwh, capacity.ratio(generation_type)),
                    capacity.max_mw(generation_type),
                    generation_type,
                    ba,
                )
            )
    return sites


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
    """Fill `cells` (in order) up to `max_mw` each until `target_mw` is placed.

    If `max_mw` is too low to place `target_mw` across the available cells, raise it
    to a per-cell floor of `target_mw / len(cells)`, rounded up so that filling every
    cell at that cap covers the target in float64 arithmetic.
    """
    if target_mw < 0:
        raise DataError(
            f"{balancing_area}: target {generation_type} capacity is negative: {target_mw}"
        )
    if target_mw == 0:
        return []
    if not cells:
        raise DataError(
            f"{balancing_area}: not enough grid cells to place {target_mw:g} MW of "
            f"{generation_type}; {target_mw:g} MW unallocated (0 cells)"
        )
    min_mw = _min_mw_per_cell(target_mw, len(cells))
    if max_mw < min_mw:
        param = _max_capacity_param(generation_type)
        print(
            f"The value of {param} is too low to meet requested capacity in {balancing_area}. "
            f"Raising the maximum to {min_mw:g} so that capacity can be supplied",
            flush=True,
        )
        max_mw = min_mw
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


def _min_mw_per_cell(target_mw: float, n_cells: int) -> float:
    """Smallest per-cell cap that covers `target_mw` when subtracted cell by cell.

    `target_mw / n_cells` can still leave a tiny remainder because float64 division
    and repeated subtraction are not inverses, so round up until the leftover is 0.
    """
    max_mw = target_mw / n_cells
    while True:
        remaining = target_mw
        for _ in range(n_cells):
            remaining -= max_mw
            if remaining <= 0:
                return max_mw
        max_mw = math.nextafter(max_mw, math.inf)


def _max_capacity_param(generation_type: GenerationType) -> str:
    if generation_type is GenerationType.SOLAR:
        return "max_solar_capacity_mw_per_cell"
    return "max_wind_capacity_mw_per_cell"


def non_negative_param(params: Config, key: str) -> float:
    value = params.get(key, float)
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"{params.source}: '{key}' must be finite and >= 0, got {value}")
    return value


def positive_param(params: Config, key: str) -> float:
    value = params.get(key, float)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{params.source}: '{key}' must be finite and > 0, got {value}")
    return value
