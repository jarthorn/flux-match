"""Locations of project folders and naming of run folders and data files.

Filename and path construction for the project lives here. Constants are names that do not
change at runtime; parameterized paths are functions. When a path depends on scenario config,
pass a RunContext (or, for fetch which has no run folder yet, the fetch Config section) and let
the function here read the keys it needs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flux_match.config import Config
    from flux_match.run_context import RunContext

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if not (PROJECT_ROOT / "pyproject.toml").is_file():
    raise RuntimeError(
        f"Expected project root at {PROJECT_ROOT}; flux_match must be run from a source checkout"
    )

DATA_DIR = PROJECT_ROOT / "data"
CODERS_DATA_DIR = DATA_DIR / "coders"
NINJA_DATA_DIR = DATA_DIR / "ninja"
OUTPUT_DIR = PROJECT_ROOT / "output"
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"

DEFAULTS_NAME = "defaults.toml"
CONFIG_NAME = "config.toml"
MANIFEST_NAME = "run.json"

SELECTED_SITES_FILENAME = "selected_sites.csv"
CLUSTERS_FILENAME = "clusters.csv"
VRE_HOURLY_FILENAME = "vre_by_ba_hourly.csv"
BA_DEMAND_FILENAME = "balancing_area_demand.csv"

TIMESTAMP_FORMAT = "%Y%m%d-%H%M"


def rankings_filename(wind_source: str) -> str:
    """Rankings depend on where the wind capacity factors came from, so each source has its own."""
    return f"grid_cell_rankings_{wind_source}.csv"


def ba_demand_hourly_filename(year: int) -> str:
    return f"balancing_area_demand_hourly_{year}.csv"


def dispatch_hourly_filename(year: int) -> str:
    return f"dispatch_hourly_{year}.csv"


def csv_filename(table: str, province: str | None = None, year: int | None = None) -> str:
    """Name of a cached table: `<table>[_<province>][_<year>].csv`."""
    parts = [table, province, None if year is None else str(year)]
    return "_".join(p for p in parts if p is not None) + ".csv"


def csv_path(
    data_dir: Path, table: str, province: str | None = None, year: int | None = None
) -> Path:
    return data_dir / csv_filename(table, province, year)


def defaults_path() -> Path:
    return SCENARIOS_DIR / DEFAULTS_NAME


def scenario_path(name_or_path: str) -> Path:
    """Resolve a scenario name (`greedy`) or an explicit path (`my/file.toml`)."""
    path = Path(name_or_path)
    if path.suffix == ".toml":
        return path
    return SCENARIOS_DIR / f"{name_or_path}.toml"


def run_dir_name(scenario_name: str, started: datetime) -> str:
    return f"{started.strftime(TIMESTAMP_FORMAT)}-{scenario_name}"


def batch_run_dir_name(batch_index: int) -> str:
    """Name of a run folder within a batch folder: the zero-padded batch index."""
    return f"{batch_index:04d}"


def ba_demand_path(data_dir: Path | None = None) -> Path:
    # Resolve DATA_DIR at call time so tests can patch it.
    return (DATA_DIR if data_dir is None else data_dir) / BA_DEMAND_FILENAME


def ba_demand_hourly_path(year: int, data_dir: Path | None = None) -> Path:
    return (DATA_DIR if data_dir is None else data_dir) / ba_demand_hourly_filename(year)


def grid_cell_rankings_path(wind_source: str, data_dir: Path | None = None) -> Path:
    return (DATA_DIR if data_dir is None else data_dir) / rankings_filename(wind_source)


def grid_cell_rankings(ctx: RunContext) -> Path:
    """The grid cell rankings for the run's fetch.wind_source. Fails if fetch has not built them
    (for NINJA, that means its download is not complete)."""
    from flux_match.errors import DataError
    from flux_match.steps.fetch.fetch import FetchParams

    wind_source = FetchParams.from_config(ctx.config.section("fetch")).wind_source
    path = grid_cell_rankings_path(wind_source)
    if not path.is_file():
        raise DataError(
            f"{path} not found; run `flux-match fetch` with wind_source={wind_source!r}"
        )
    return path


def selected_sites_path(ctx: RunContext) -> Path:
    return ctx.run_dir / SELECTED_SITES_FILENAME


def clusters_path(ctx: RunContext) -> Path:
    return ctx.run_dir / CLUSTERS_FILENAME


def vre_hourly_path(ctx: RunContext) -> Path:
    return ctx.run_dir / VRE_HOURLY_FILENAME


def dispatch_hourly_path(ctx: RunContext, year: int) -> Path:
    return ctx.run_dir / dispatch_hourly_filename(year)


def config_path(run_dir: Path) -> Path:
    return run_dir / CONFIG_NAME


def manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_NAME


def wind_capacity_factor_path(
    fetch_config: Config,
    coders_dir: Path | None = None,
    ninja_dir: Path | None = None,
) -> Path:
    """The hourly wind capacity-factor table for `fetch.wind_source`, for steps after fetch."""
    from flux_match.errors import DataError
    from flux_match.steps.fetch.fetch import WIND_CF_TABLE, FetchParams

    params = FetchParams.from_config(fetch_config)
    (year,) = params.capacity_factor_years
    if coders_dir is None:
        coders_dir = CODERS_DATA_DIR
    if ninja_dir is None:
        ninja_dir = NINJA_DATA_DIR
    wind_dir = ninja_dir if params.wind_source == "ninja" else coders_dir
    path = csv_path(wind_dir, WIND_CF_TABLE, year=year)
    if not (path.is_file() and path.stat().st_size > 0):
        raise DataError(
            f"{path} not found (fetch.wind_source = {params.wind_source!r}); "
            f"run `flux-match fetch` first"
        )
    return path
