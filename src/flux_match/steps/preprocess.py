"""Step 3: hourly wind and solar generation per balancing area for the selected sites.

Joins selected_sites.csv with grid_cell_info balancing areas, multiplies each site's
nameplate by its hourly capacity factor, and sums those MW values within each balancing
area. Onshore and offshore wind both use the wind capacity-factor table; solar uses the
solar table. The year is fetch.capacity_factor_years (exactly one).

Writes vre_by_ba_hourly.csv: one row per balancing area per hour (1-8760), with wind_mw,
solar_mw, and variable_generation_mw (their sum). Values are rounded to whole MW, the precision
of the CODERS demand data; the sum is of the rounded values, so each row adds up exactly. Every
balancing area in grid_cell_info is included; areas with no selected sites are zero.
Capacity-factor files are streamed.

A selected grid cell missing from grid_cell_info or from the relevant capacity-factor
table is a hard failure. Capacity factors must be in [0, 1], with 8760 sequential hours.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from flux_match import paths
from flux_match.errors import DataError
from flux_match.grid_cell_rankings import iter_hourly_capacity_factors, read_grid_cells
from flux_match.io import write_csv_atomic
from flux_match.run_context import RunContext
from flux_match.schema import HOURS_PER_YEAR, VRE_HOURLY_COLUMNS
from flux_match.selection.base import GenerationType, Site, read_sites
from flux_match.steps.fetch import GRID_CELL_TABLE, FetchParams, csv_path

WIND_TYPES = frozenset({GenerationType.WIND_ONSHORE, GenerationType.WIND_OFFSHORE})


@dataclass(frozen=True)
class LocatedSite:
    grid_cell: int
    balancing_area: str
    capacity_mw: float


def run(
    ctx: RunContext,
    data_dir: Path = paths.CODERS_DATA_DIR,
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> None:
    params = FetchParams.from_config(ctx.config.section("fetch"))
    (year,) = params.capacity_factor_years
    dest = ctx.vre_hourly_path
    print(f"writing {dest.name}", flush=True)
    write_vre_hourly(
        sites_path=ctx.selected_sites_path,
        grid_cell_info=csv_path(data_dir, GRID_CELL_TABLE),
        wind_cf=csv_path(data_dir, "wind_capacity_factor", year=year),
        solar_cf=csv_path(data_dir, "solar_capacity_factor", year=year),
        dest=dest,
        hours_in_year=hours_in_year,
    )


def write_vre_hourly(
    sites_path: Path,
    grid_cell_info: Path,
    wind_cf: Path,
    solar_cf: Path,
    dest: Path,
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> None:
    """Build `dest` from selected sites, grid cells, and the two capacity-factor tables."""
    sites = read_sites(sites_path)
    cells, areas = read_grid_cells(grid_cell_info)
    cell_to_ba = dict(zip(cells, areas, strict=True))
    balancing_areas = sorted(set(areas))
    wind_sites, solar_sites = locate_sites(sites, cell_to_ba, sites_path)
    wind = hourly_generation_by_ba(wind_cf, wind_sites, hours_in_year=hours_in_year)
    solar = hourly_generation_by_ba(solar_cf, solar_sites, hours_in_year=hours_in_year)
    write_csv_atomic(
        dest,
        VRE_HOURLY_COLUMNS,
        _rows(balancing_areas, wind, solar, hours_in_year),
    )


def locate_sites(
    sites: Sequence[Site],
    cell_to_ba: dict[str, str],
    source: Path,
) -> tuple[list[LocatedSite], list[LocatedSite]]:
    """Split selected sites into wind and solar, tagged with their balancing area."""
    missing: list[int] = []
    wind: list[LocatedSite] = []
    solar: list[LocatedSite] = []
    for site in sites:
        ba = cell_to_ba.get(str(site.grid_cell))
        if ba is None:
            missing.append(site.grid_cell)
            continue
        located = LocatedSite(site.grid_cell, ba, site.capacity_mw)
        if site.generation_type is GenerationType.SOLAR:
            solar.append(located)
        elif site.generation_type in WIND_TYPES:
            wind.append(located)
        else:
            raise DataError(
                f"{source}: unsupported generation type {site.generation_type!r} "
                f"for grid cell {site.grid_cell}"
            )
    if missing:
        raise DataError(
            f"{source.name}: selected grid cells not in grid_cell_info: {sorted(set(missing))}"
        )
    return wind, solar


def hourly_generation_by_ba(
    cf_path: Path,
    sites: Sequence[LocatedSite],
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> dict[str, list[float]]:
    """MW generation per balancing area for each hour, from `sites` and one CF table.

    Sites that share a balancing area are summed. An empty site list returns no areas,
    so the caller can treat a missing area as zero without streaming the file.
    """
    if not sites:
        return {}
    columns = [str(site.grid_cell) for site in sites]
    totals = {site.balancing_area: [0.0] * hours_in_year for site in sites}
    for hour, values in enumerate(
        iter_hourly_capacity_factors(cf_path, columns, hours_in_year=hours_in_year)
    ):
        for site, factor in zip(sites, values, strict=True):
            totals[site.balancing_area][hour] += factor * site.capacity_mw
    return totals


def _rows(
    balancing_areas: Sequence[str],
    wind: dict[str, list[float]],
    solar: dict[str, list[float]],
    hours_in_year: int,
) -> Iterator[dict[str, object]]:
    zeros = (0.0,) * hours_in_year
    for ba in balancing_areas:
        wind_hours = wind.get(ba, zeros)
        solar_hours = solar.get(ba, zeros)
        for hour in range(hours_in_year):
            wind_mw = round(wind_hours[hour])
            solar_mw = round(solar_hours[hour])
            yield {
                "balancing_area": ba,
                "hour": hour + 1,
                "wind_mw": wind_mw,
                "solar_mw": solar_mw,
                "variable_generation_mw": wind_mw + solar_mw,
            }
