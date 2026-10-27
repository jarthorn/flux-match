"""Step 1: download raw input data from CODERS into data/coders/.

Skipped for any file already present. Writes to the shared data folder rather than a run
folder, so downloads are reused across runs.

Every hourly table is stored with exactly HOURS_PER_YEAR (8760) hours. In leap years,
February 29 (hour-ending 1417-1440) is dropped and later hours are renumbered, so hour
1441 becomes 1417. Other columns, such as local_time, keep their original values.
"""

import csv
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import DataError
from flux_match.io import write_csv
from flux_match.schema import HOURS_PER_YEAR

API_BASE = "https://api.sesit.ca"
API_KEY_ENV = "CODERS_API_KEY"
TIMEOUT_S = 120
CHUNK_SIZE = 1024 * 1024

GRID_CELL_TABLE = "grid_cell_info"
DEMAND_TABLE = "provincial_demand"
CAPACITY_FACTOR_TABLES = ("solar_capacity_factor", "wind_capacity_factor")

LEAP_DAY_HOURS = range(1417, 1441)  # hour-ending of February 29: 59 days * 24 = 1416 before it
LEAP_YEAR_HOURS = HOURS_PER_YEAR + len(LEAP_DAY_HOURS)

GRID_CELL_COLUMNS = (
    "grid_cell",
    "longitude",
    "latitude",
    "balancing_area",
    "province",
    "distance_to_grid",
    "population",
    "surface_area_offshore",
    "surface_area_onshore",
)
DEMAND_COLUMNS = (
    "local_time",
    "province",
    "daily_hour_ending",
    "annual_hour_ending",
    "demand_MWh",
)

Opener = Callable[..., Any]


@dataclass(frozen=True)
class FetchParams:
    provinces: tuple[str, ...]
    demand_years: tuple[int, ...]
    capacity_factor_years: tuple[int, ...]

    @classmethod
    def from_config(cls, params: Config) -> Self:
        return cls(
            provinces=tuple(params.get_list("provinces", str, unique=True)),
            demand_years=tuple(params.get_list("demand_years", int, unique=True)),
            capacity_factor_years=tuple(params.get_list("capacity_factor_years", int, unique=True)),
        )


@dataclass(frozen=True)
class Download:
    table: str
    params: dict[str, str | int]
    required_columns: tuple[str, ...] | None = None
    hour_column: str | None = None

    @property
    def filename(self) -> str:
        return csv_filename(self.table, **self.params)


def csv_filename(table: str, province: str | None = None, year: int | None = None) -> str:
    """Name of a cached CODERS table: `<table>[_<province>][_<year>].csv`."""
    parts = [table, province, None if year is None else str(year)]
    return "_".join(p for p in parts if p is not None) + ".csv"


def csv_path(
    data_dir: Path, table: str, province: str | None = None, year: int | None = None
) -> Path:
    return data_dir / csv_filename(table, province, year)


def downloads(params: FetchParams) -> list[Download]:
    items = [Download(GRID_CELL_TABLE, {}, GRID_CELL_COLUMNS)]
    for year in params.demand_years:
        for province in params.provinces:
            items.append(
                Download(
                    DEMAND_TABLE,
                    {"province": province, "year": year},
                    DEMAND_COLUMNS,
                    hour_column="annual_hour_ending",
                )
            )
    for year in params.capacity_factor_years:
        for table in CAPACITY_FACTOR_TABLES:
            items.append(Download(table, {"year": year}, hour_column="h"))
    return items


def run(
    config: Config,
    data_dir: Path = paths.CODERS_DATA_DIR,
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = paths.PROJECT_ROOT / ".env",
    opener: Opener | None = None,
) -> None:
    params = FetchParams.from_config(config.section("fetch"))
    data_dir.mkdir(parents=True, exist_ok=True)
    missing: list[tuple[Download, Path]] = []
    for item in downloads(params):
        dest = data_dir / item.filename
        if not _already_present(dest):
            missing.append((item, dest))
    if not missing:
        return
    api_key = _coders_api_key(environ, dotenv_path)
    get = opener or urlopen
    for item, dest in missing:
        print(f"downloading {dest.name}")
        _fetch_one(item, dest, api_key, get)


def _already_present(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def _coders_api_key(environ: Mapping[str, str] | None, dotenv_path: Path | None) -> str:
    env = os.environ if environ is None else environ
    key = (env.get(API_KEY_ENV) or "").strip()
    if key:
        return key
    if dotenv_path is not None:
        key = _read_env_file(dotenv_path).get(API_KEY_ENV, "").strip()
        if key:
            return key
    location = dotenv_path if dotenv_path is not None else Path(".env")
    raise DataError(f"{API_KEY_ENV} is not set. Put it in the environment or in {location}.")


def _params_desc(params: Mapping[str, str | int]) -> str:
    if not params:
        return "all rows"
    return " ".join(f"{k}={v}" for k, v in params.items())


def _http_error_body(error: HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        error.close()


def _message_from_body(body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.splitlines()[0][:200]
    if isinstance(payload, dict):
        for key in ("message", "error", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return body.splitlines()[0][:200]


def _open(item: Download, api_key: str, opener: Opener) -> Any:
    query = urlencode({**item.params, "key": api_key})
    request = Request(f"{API_BASE}/{item.table}?{query}")
    try:
        return opener(request, timeout=TIMEOUT_S)
    except HTTPError as e:
        detail = _message_from_body(_http_error_body(e))
        where = f"{item.table} ({_params_desc(item.params)})"
        if e.code in {401, 403}:
            raise DataError(f"CODERS rejected the API key (HTTP {e.code})") from None
        suffix = f": {detail}" if detail else ""
        raise DataError(f"CODERS HTTP {e.code} for {where}{suffix}") from None
    except URLError as e:
        raise DataError(
            f"CODERS request failed for {item.table} ({_params_desc(item.params)}): {e.reason}"
        ) from None


def _is_csv_response(headers: Mapping[str, str] | Any) -> bool:
    content_type = headers.get("Content-Type") if headers is not None else None
    return "csv" in (content_type or "").lower()


def _json_records(body: bytes, item: Download) -> list[dict[str, object]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DataError(
            f"CODERS returned non-JSON for {item.table} ({_params_desc(item.params)}): {e}"
        ) from None
    if not isinstance(payload, list) or not payload:
        raise DataError(f"CODERS returned no rows for {item.table} ({_params_desc(item.params)})")
    records: list[dict[str, object]] = []
    for i, row in enumerate(payload):
        if not isinstance(row, dict):
            raise DataError(f"CODERS {item.table} row {i} is not an object: {type(row).__name__}")
        records.append(row)
    if item.required_columns:
        missing = [c for c in item.required_columns if c not in records[0]]
        if missing:
            raise DataError(f"CODERS {item.table} missing required columns {missing}")
    return records


def _stream_to_file(response: Any, dest: Path) -> None:
    with dest.open("wb") as out:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
    if dest.stat().st_size == 0:
        raise DataError(f"CODERS returned an empty file for {dest.name}")


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


def _fetch_one(item: Download, dest: Path, api_key: str, opener: Opener) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with _open(item, api_key, opener) as response:
            if _is_csv_response(response.headers):
                _stream_to_file(response, tmp)
            else:
                records = _json_records(response.read(), item)
                write_csv(tmp, tuple(records[0].keys()), records)
        if item.hour_column:
            _drop_leap_day(tmp, item.hour_column, dest.name)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
