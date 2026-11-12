"""Step 1: download raw input data

Skipped for any file already present. Writes to the shared data/ folder rather than a run
folder, so downloads are reused across runs.

Source-specific download logic lives in sibling modules (fetch_coders.py, and fetch_ninja.py).
This module owns the public run() entry point, shared cache helpers, and the derived tables
written after the raw downloads complete.

Every hourly table is stored with exactly HOURS_PER_YEAR (8760) hours. In leap years,
February 29 (hour-ending 1417-1440) is dropped and later hours are renumbered, so hour
1441 becomes 1417. Other columns, such as local_time, keep their original values.

A blank demand value is reported on stderr and replaced with the previous hour's value.
JSON rows that don't all have the same columns are a hard failure, and no file is written.

Next, the step derives data/balancing_area_demand.csv: total annual demand per balancing area for
every demand year (see balancing_area_demand.py). Unlike the downloads, it uses every provincial
demand file in data/coders/, not only the configured demand_years, and every year found must
have all provinces. It is rebuilt when missing or older than any of those files.

Then, for each configured demand year it derives data/balancing_area_demand_hourly_<year>.csv,
the hourly demand per balancing area that the dispatch step uses. Each is rebuilt when missing or
older than any of that year's provincial demand files.

The CODERS solar and wind capacity-factor tables are always downloaded, whatever
fetch.wind_source is, so the CODERS wind table is there as a backup for NINJA's, which takes days
to download. Wind capacity factors used by later steps come from the source named by
fetch.wind_source. Only with "ninja" does the step also download per-cell wind output from
renewables.ninja into data/ninja/raw/ and collate it into data/ninja/wind_capacity_factor_<year>.csv
(see fetch_ninja.py). Cells not downloaded yet are filled with 0 in the collated file, so a
download that stopped early at NINJA's rate limit can still be inspected, but the run then fails
before deriving anything from it. Solar capacity factors always come from CODERS.

Finally, the step derives data/grid_cell_rankings_<wind source>.csv from grid_cell_info and the
capacity-factor tables (see grid_cell_rankings.py). It is rebuilt when missing or older than any
of its inputs. Each wind source has its own file, and the NINJA one is only built once its
download is complete (and removed again if a later download stops early), so later steps fail
on the missing file rather than use incomplete NINJA data. The rankings are derived from one
capacity-factor year, so exactly one must be configured.

paths.wind_capacity_factor_path() is how later steps find the wind table for the configured
source. It fails if the table is missing.
"""

import csv
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError

from flux_match import paths
from flux_match.balancing_area_demand import (
    demand_file_paths,
    find_demand_files,
    write_balancing_area_demand,
    write_hourly_ba_demand,
)
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError, FetchIncompleteError, report_data_error
from flux_match.grid_cell_rankings import write_grid_cell_rankings
from flux_match.schema import HOURS_PER_YEAR

GRID_CELL_TABLE = "grid_cell_info"
SOLAR_CF_TABLE = "solar_capacity_factor"
WIND_CF_TABLE = "wind_capacity_factor"

WIND_SOURCES = ("coders", "ninja")

LEAP_DAY_HOURS = range(1417, 1441)  # hour-ending of February 29: 59 days * 24 = 1416 before it
LEAP_YEAR_HOURS = HOURS_PER_YEAR + len(LEAP_DAY_HOURS)

Opener = Callable[..., Any]


@dataclass(frozen=True)
class FetchParams:
    provinces: tuple[str, ...]
    demand_years: tuple[int, ...]
    capacity_factor_years: tuple[int, ...]
    wind_source: str

    @classmethod
    def from_config(cls, params: Config) -> Self:
        capacity_factor_years = tuple(params.get_list("capacity_factor_years", int, unique=True))
        if len(capacity_factor_years) != 1:
            # The rankings file is derived from a single year of capacity factors.
            raise ConfigError(
                f"{params.source}: 'fetch.capacity_factor_years' must have exactly one year, "
                f"got {list(capacity_factor_years)}"
            )
        wind_source = params.get("wind_source", str)
        if wind_source not in WIND_SOURCES:
            raise ConfigError(
                f"{params.source}: 'fetch.wind_source' must be one of {list(WIND_SOURCES)}, "
                f"got {wind_source!r}"
            )
        return cls(
            provinces=tuple(params.get_list("provinces", str, unique=True)),
            demand_years=tuple(params.get_list("demand_years", int, unique=True)),
            capacity_factor_years=capacity_factor_years,
            wind_source=wind_source,
        )


def run(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> None:
    # Imported lazily so source modules can use helpers from this file without a cycle.
    from flux_match.steps.fetch import fetch_coders

    fetch_config = config.section("fetch")
    params = FetchParams.from_config(fetch_config)
    paths.CODERS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetch_coders.download(params, environ=environ, opener=opener)
    _write_balancing_area_demand(params.demand_years)
    if params.wind_source == "ninja":
        # Before the rankings, which are only built from a complete NINJA download. Can stop
        # early at NINJA's rate limit and fail the run; the CODERS derivations above are in
        # place when it does.
        _fetch_ninja_wind(fetch_config, params, environ, opener)
    _write_rankings(fetch_config, params)


def _fetch_ninja_wind(
    fetch_config: Config,
    params: FetchParams,
    environ: Mapping[str, str] | None,
    opener: Opener | None,
) -> None:
    from flux_match.steps.fetch import fetch_ninja

    ninja = fetch_ninja.NinjaParams.from_config(fetch_config.section("ninja"))
    (year,) = params.capacity_factor_years
    grid_cell_info = paths.csv_path(paths.CODERS_DATA_DIR, GRID_CELL_TABLE)
    stopped = None
    try:
        fetch_ninja.download(ninja, year, grid_cell_info, environ=environ, opener=opener)
    except FetchIncompleteError as e:
        stopped = e  # still collate what was downloaded, then fail the run
    fetch_ninja.collate(ninja, year, grid_cell_info)
    if stopped:
        # The collated table is now partial (zero-filled), so rankings built from an earlier,
        # complete download (e.g. other turbine settings) no longer match it.
        paths.grid_cell_rankings_path("ninja").unlink(missing_ok=True)
        raise stopped


def _write_rankings(fetch_config: Config, params: FetchParams) -> None:
    """Derive the rankings file for the wind source if it is missing or older than any of its
    inputs. Reached for NINJA only once its download is complete."""
    (year,) = params.capacity_factor_years
    grid_cell_info = paths.csv_path(paths.CODERS_DATA_DIR, GRID_CELL_TABLE)
    solar = paths.csv_path(paths.CODERS_DATA_DIR, SOLAR_CF_TABLE, year=year)
    wind = paths.wind_capacity_factor_path(fetch_config)
    dest = paths.grid_cell_rankings_path(params.wind_source)
    if _up_to_date(dest, [grid_cell_info, solar, wind]):
        return
    print(f"writing {dest.name}", flush=True)
    write_grid_cell_rankings(grid_cell_info, solar, wind, dest)


def _write_balancing_area_demand(demand_years: Sequence[int]) -> None:
    """Derive balancing_area_demand.csv from every provincial demand file, and the hourly file
    for each of `demand_years`, each only if missing or older than its inputs."""
    files = find_demand_files(paths.CODERS_DATA_DIR)
    dest = paths.ba_demand_path()
    if not _up_to_date(dest, demand_file_paths(files)):
        print(f"writing {dest.name}", flush=True)
        write_balancing_area_demand(files, dest)
    for year in demand_years:
        if year not in files:
            raise DataError(
                f"No provincial demand files for demand year {year} in {paths.CODERS_DATA_DIR}"
            )
        dest = paths.ba_demand_hourly_path(year)
        if not _up_to_date(dest, list(files[year].values())):
            print(f"writing {dest.name}", flush=True)
            write_hourly_ba_demand(files[year], dest)


def _up_to_date(dest: Path, inputs: Sequence[Path]) -> bool:
    return _already_present(dest) and dest.stat().st_mtime >= max(p.stat().st_mtime for p in inputs)


def _already_present(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _http_error_body(error: HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        error.close()


def _message_from_body(body: str) -> str:
    """The error text from an API error response, for including in our own error message."""
    body = body.strip()
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.splitlines()[0][:200]
    if isinstance(payload, dict):
        for key in ("message", "error", "description", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return body.splitlines()[0][:200]


def _fill_missing_from_previous_hour(
    records: list[dict[str, object]], hour_column: str, columns: tuple[str, ...], name: str
) -> None:
    """Replace blank values with the previous hour's value, reporting each one on stderr."""
    for i, row in enumerate(records):
        if str(row[hour_column]) != str(i + 1):
            raise DataError(
                f"{name}: expected hour {i + 1} on row {i + 1}, got {row[hour_column]!r}"
            )
        for column in columns:
            if row[column] is not None and str(row[column]).strip():
                continue
            if i == 0:
                raise DataError(f"{name}: missing {column} at hour 1, no previous hour to copy")
            row[column] = records[i - 1][column]
            report_data_error(
                f"{name}: missing {column} at hour {i + 1}; copied {row[column]} from hour {i}"
            )


def _drop_leap_day(path: Path, hour_column: str, name: str) -> None:
    """Rewrite a leap-year hourly CSV without February 29. Streams rows, since
    capacity-factor files are hundreds of MB."""
    with path.open(newline="") as f:
        hours = sum(1 for _ in csv.reader(f)) - 1
    if hours == HOURS_PER_YEAR:
        return
    if hours != LEAP_YEAR_HOURS:
        raise DataError(
            f"{name}: expected {HOURS_PER_YEAR} or {LEAP_YEAR_HOURS} hours, got {hours}"
        )

    out = path.with_name(path.name + ".noleap")
    try:
        with path.open(newline="") as src, out.open("w", newline="") as dst:
            reader = csv.reader(src)
            writer = csv.writer(dst)
            header = next(reader)
            if hour_column not in header:
                raise DataError(f"{name}: missing hour column '{hour_column}'")
            hour_i = header.index(hour_column)
            time_i = header.index("local_time") if "local_time" in header else None
            writer.writerow(header)
            for expected, row in enumerate(reader, start=1):
                if row[hour_i] != str(expected):
                    raise DataError(
                        f"{name}: expected hour {expected} on row {expected}, got {row[hour_i]!r}"
                    )
                if expected in LEAP_DAY_HOURS:
                    if time_i is not None and row[time_i][5:10] != "02-29":
                        raise DataError(
                            f"{name}: hour {expected} should be February 29, "
                            f"got local_time {row[time_i]!r}"
                        )
                    continue
                if expected > LEAP_DAY_HOURS[-1]:
                    row[hour_i] = str(expected - len(LEAP_DAY_HOURS))
                writer.writerow(row)
        out.replace(path)
    finally:
        out.unlink(missing_ok=True)
