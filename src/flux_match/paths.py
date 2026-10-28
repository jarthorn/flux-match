"""Locations of project folders and naming of run folders."""

from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if not (PROJECT_ROOT / "pyproject.toml").is_file():
    raise RuntimeError(
        f"Expected project root at {PROJECT_ROOT}; flux_match must be run from a source checkout"
    )

DATA_DIR = PROJECT_ROOT / "data"
CODERS_DATA_DIR = DATA_DIR / "coders"
OUTPUT_DIR = PROJECT_ROOT / "output"
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"

BA_DEMAND_FILENAME = "balancing_area_demand.csv"
RANKINGS_FILENAME = "grid_cell_rankings.csv"

TIMESTAMP_FORMAT = "%Y%m%d-%H%M"


def ba_demand_hourly_filename(year: int) -> str:
    return f"balancing_area_demand_hourly_{year}.csv"


def scenario_path(name_or_path: str) -> Path:
    """Resolve a scenario name (`greedy`) or an explicit path (`my/file.toml`)."""
    path = Path(name_or_path)
    if path.suffix == ".toml":
        return path
    return SCENARIOS_DIR / f"{name_or_path}.toml"


def run_dir_name(scenario_name: str, started: datetime) -> str:
    return f"{started.strftime(TIMESTAMP_FORMAT)}-{scenario_name}"
