"""Step 2: derived inputs for site selection, written to the shared data/ folder.

run() calls one function per derivation. Further derivations are added as further calls.

The first derivation is data/grid_cell_rankings_<wind source>.csv, from grid_cell_info and the
capacity-factor tables (see grid_cell_rankings.py). It is rebuilt when missing or older than any
of its inputs. Wind capacity factors come from fetch.wind_source; solar always comes from CODERS.
Each wind source has its own file. The NINJA file can be built only once fetch has finished
downloading and collating that source, so a missing wind table is a hard failure rather than
rankings from an incomplete download. Exactly one capacity_factor_years entry is required.
"""

from flux_match import paths
from flux_match.config import Config
from flux_match.grid_cell_rankings import write_grid_cell_rankings
from flux_match.steps.fetch.fetch import (
    GRID_CELL_TABLE,
    SOLAR_CF_TABLE,
    FetchParams,
    _up_to_date,
)


def run(config: Config) -> None:
    write_rankings(config)


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
