"""Download raw hourly wind output per grid cell from the renewables.ninja API.

Authentication, HTTP, and response validation live here so other sources can be added beside
this module without sharing that logic. Shared fetch helpers (cache checks, error-body parsing)
come from fetch.py.

One request is made per cell in grid_cell_info.csv, and the response is stored as downloaded in
`data/ninja/raw/<turbine model>_<height>m_<year>/wind_<grid_cell>.csv`: hourly `time` (UTC) and
`electricity` (kW, for a CAPACITY_KW turbine fleet) columns. Cells whose file already exists are
skipped, so a run can be repeated until every cell is present. Folders are named by turbine
settings so that changing them never reuses files from another turbine.

NINJA allows 1 request per second and 50 per hour. Requests are spaced by `request_sleep_s`, and
the run stops with a FetchIncompleteError before sending a request that would exceed
`max_requests_per_hour` within a rolling hour. Request times are kept in `.requests.log` in the
data folder, so the limit also counts requests made by earlier runs.
"""

import calendar
import csv
import io
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flux_match.config import Config
from flux_match.errors import ConfigError, DataError, FetchIncompleteError
from flux_match.steps.fetch.fetch import (
    _already_present,
    _http_error_body,
    _message_from_body,
)

API_URL = "https://www.renewables.ninja/api/data/wind"
API_TOKEN_ENV = "NINJA_API_TOKEN"
TIMEOUT_S = 120
# Sent instead of urllib's default "Python-urllib/x.y", which NINJA's Cloudflare blocks (HTTP 403,
# "error code: 1010").
USER_AGENT = "flux-match/1.0"

# Turbine fleet size sent with every request, in kW. Output is divided by it to get a capacity
# factor, so it only needs to be large enough that NINJA's rounding of `electricity` is negligible.
CAPACITY_KW = 100_000
DATASET = "merra2"

RAW_SUBDIR = "raw"
REQUEST_LOG_NAME = ".requests.log"
RATE_WINDOW_S = 3600
MIN_REQUEST_SLEEP_S = 1  # NINJA's limit is 1 request per second
CAPACITY_TOLERANCE = 1e-6  # relative slack when checking output against CAPACITY_KW

GRID_CELL_COLUMNS = ("grid_cell", "latitude", "longitude")

Opener = Callable[..., Any]


@dataclass(frozen=True)
class NinjaParams:
    enabled: bool
    turbine_height: int
    turbine_model: str
    request_sleep_s: float
    max_requests_per_hour: int

    @classmethod
    def from_config(cls, params: Config) -> Self:
        parsed = cls(
            enabled=params.get("enabled", bool),
            turbine_height=params.get("turbine_height", int),
            turbine_model=params.get("turbine_model", str).strip(),
            request_sleep_s=params.get("request_sleep_s", float),
            max_requests_per_hour=params.get("max_requests_per_hour", int),
        )
        problems = []
        if parsed.turbine_height <= 0:
            problems.append("'turbine_height' must be positive")
        if not _slug(parsed.turbine_model):
            problems.append("'turbine_model' must contain letters or digits")
        if parsed.request_sleep_s < MIN_REQUEST_SLEEP_S:
            problems.append(
                f"'request_sleep_s' must be at least {MIN_REQUEST_SLEEP_S} (NINJA's rate limit)"
            )
        if parsed.max_requests_per_hour < 1:
            problems.append("'max_requests_per_hour' must be at least 1")
        if problems:
            raise ConfigError(f"{params.source}: fetch.ninja: " + "; ".join(problems))
        return parsed


@dataclass(frozen=True)
class Cell:
    grid_cell: str
    latitude: float
    longitude: float


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def raw_dir(ninja_dir: Path, params: NinjaParams, year: int) -> Path:
    """Folder of raw files for one turbine model, height and year."""
    name = f"{_slug(params.turbine_model)}_{params.turbine_height}m_{year}"
    return ninja_dir / RAW_SUBDIR / name


def raw_path(directory: Path, cell: Cell) -> Path:
    return directory / f"wind_{cell.grid_cell}.csv"


def read_cells(grid_cell_info: Path) -> list[Cell]:
    """Every grid cell with its centre coordinates, in file order."""
    if not grid_cell_info.is_file():
        raise DataError(f"{grid_cell_info} not found; the CODERS download must run first")
    with grid_cell_info.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in GRID_CELL_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise DataError(f"{grid_cell_info.name}: missing required columns {missing}")
        cells = [_parse_cell(row, grid_cell_info.name, n) for n, row in enumerate(reader, start=2)]
    if not cells:
        raise DataError(f"{grid_cell_info.name} has no grid cells")
    ids = [c.grid_cell for c in cells]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise DataError(f"{grid_cell_info.name}: duplicate grid cells {duplicates}")
    return cells


def _parse_cell(row: dict[str, str], name: str, line: int) -> Cell:
    grid_cell = (row["grid_cell"] or "").strip()
    try:
        latitude, longitude = float(row["latitude"]), float(row["longitude"])
    except TypeError, ValueError:
        raise DataError(
            f"{name} line {line}: bad coordinates for cell {grid_cell!r}: "
            f"latitude={row['latitude']!r}, longitude={row['longitude']!r}"
        ) from None
    if not grid_cell or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise DataError(
            f"{name} line {line}: bad cell {grid_cell!r} at latitude={latitude}, "
            f"longitude={longitude}"
        )
    return Cell(grid_cell, latitude, longitude)


class RequestLimiter:
    """Rolling-window request counter that persists in a log file, one epoch time per line."""

    def __init__(
        self, log_path: Path, max_requests: int, clock: Callable[[], float] = time.time
    ) -> None:
        self._path = log_path
        self._max = max_requests
        self._clock = clock
        self._times = self._load()

    def _load(self) -> list[float]:
        if not self._path.is_file():
            return []
        times = []
        for n, line in enumerate(self._path.read_text().splitlines(), start=1):
            try:
                times.append(float(line))
            except ValueError:
                raise DataError(f"{self._path} line {n}: expected a time, got {line!r}") from None
        recent = self._recent(sorted(times))
        if len(recent) != len(times):
            self._path.write_text("".join(f"{t!r}\n" for t in recent))
        return recent

    def _recent(self, times: list[float]) -> list[float]:
        cutoff = self._clock() - RATE_WINDOW_S
        return [t for t in times if t > cutoff]

    @property
    def count(self) -> int:
        """Requests sent within the last hour."""
        self._times = self._recent(self._times)
        return len(self._times)

    def next_allowed(self) -> float | None:
        """None if a request may be sent now, else the epoch time one first may be."""
        if self.count < self._max:
            return None
        # Enough of the oldest requests must leave the window to get back under the cap.
        return self._times[len(self._times) - self._max] + RATE_WINDOW_S

    def record(self) -> None:
        """Count a request, before it is sent: a failed request still counts against NINJA."""
        now = self._clock()
        with self._path.open("a") as f:
            f.write(f"{now!r}\n")
        self._times.append(now)


def download(
    params: NinjaParams,
    year: int,
    grid_cell_info: Path,
    ninja_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Opener | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Download the raw wind file of every cell that does not have one yet.

    Raises FetchIncompleteError if the hourly request limit is reached first; rerun later to
    continue.
    """
    cells = read_cells(grid_cell_info)
    directory = raw_dir(ninja_dir, params, year)
    missing = [
        (c, raw_path(directory, c)) for c in cells if not _already_present(raw_path(directory, c))
    ]
    print(
        f"NINJA wind: {len(cells) - len(missing)} of {len(cells)} cells already downloaded "
        f"({directory.relative_to(ninja_dir)})",
        flush=True,
    )
    if not missing:
        return
    token = _api_token(environ)
    directory.mkdir(parents=True, exist_ok=True)
    limiter = RequestLimiter(ninja_dir / REQUEST_LOG_NAME, params.max_requests_per_hour, clock)
    get = opener or urlopen
    for i, (cell, dest) in enumerate(missing):
        resume_at = limiter.next_allowed()
        if resume_at is not None:
            raise FetchIncompleteError(
                _limit_message(
                    params, limiter.count, len(missing) - i, len(cells), resume_at, clock
                )
            )
        if i:
            sleep(params.request_sleep_s)
        print(f"downloading {dest.name} ({i + 1}/{len(missing)})", flush=True)
        limiter.record()
        _fetch_one(params, year, cell, dest, token, get, limiter.count)


def _limit_message(
    params: NinjaParams,
    requests_sent: int,
    remaining: int,
    total: int,
    resume_at: float,
    clock: Callable[[], float],
) -> str:
    minutes = max(1, math.ceil((resume_at - clock()) / 60))
    resume_time = datetime.fromtimestamp(resume_at).strftime("%H:%M:%S")
    return (
        f"stopping NINJA wind download: {requests_sent} requests sent in the last hour reached the "
        f"limit of {params.max_requests_per_hour}. {remaining} of {total} cells still to download. "
        f"The next request is allowed at {resume_time} (in about {minutes} min); "
        f"rerun the fetch then to continue."
    )


def _api_token(environ: Mapping[str, str] | None) -> str:
    env = os.environ if environ is None else environ
    token = (env.get(API_TOKEN_ENV) or "").strip()
    if token:
        return token
    raise DataError(
        f"{API_TOKEN_ENV} is not set. Put it in the environment or in a .env file "
        f"at the project root."
    )


def _query(params: NinjaParams, year: int, cell: Cell) -> dict[str, str | int | float]:
    return {
        "lat": cell.latitude,
        "lon": cell.longitude,
        "date_from": f"{year}-01-01",
        "date_to": f"{year}-12-31",
        "capacity": CAPACITY_KW,
        "dataset": DATASET,
        "height": params.turbine_height,
        "turbine": params.turbine_model,
        "format": "csv",
        "header": "false",
        "local_time": "false",
        "raw": "false",
        "interpolate": "false",
    }


def _request(
    params: NinjaParams, year: int, cell: Cell, token: str, opener: Opener, requests_sent: int
) -> bytes:
    request = Request(
        f"{API_URL}?{urlencode(_query(params, year, cell))}",
        headers={"Authorization": f"Token {token}", "User-Agent": USER_AGENT},
    )
    where = f"cell {cell.grid_cell} ({cell.latitude}, {cell.longitude})"
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            return response.read()
    except HTTPError as e:
        detail = _message_from_body(_http_error_body(e))
        suffix = f": {detail}" if detail else ""
        if e.code == 401:
            raise DataError(f"NINJA rejected the API token (HTTP 401){suffix}") from None
        if e.code == 403:
            # Either a bad token or a block in front of the API (e.g. Cloudflare).
            raise DataError(
                f"NINJA refused the request (HTTP 403){suffix}. Check {API_TOKEN_ENV}; "
                f"the request may also have been blocked."
            ) from None
        if e.code == 429:
            raise FetchIncompleteError(
                f"NINJA rate limit hit (HTTP 429) at {where} after {requests_sent} requests in "
                f"the last hour{suffix}. Wait before rerunning the fetch."
            ) from None
        raise DataError(f"NINJA HTTP {e.code} for {where}{suffix}") from None
    except URLError as e:
        raise DataError(f"NINJA request failed for {where}: {e.reason}") from None


def _fetch_one(
    params: NinjaParams,
    year: int,
    cell: Cell,
    dest: Path,
    token: str,
    opener: Opener,
    requests_sent: int,
) -> None:
    body = _request(params, year, cell, token, opener, requests_sent)
    validate_raw(body, year, dest.name)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_bytes(body)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def validate_raw(body: bytes, year: int, name: str) -> None:
    """Hard-fail unless `body` is a full year of hourly UTC output, from Jan 1 00:00, with every
    value between 0 and CAPACITY_KW. Leap years have all 8784 hours; Feb 29 is dropped later."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DataError(f"{name}: not UTF-8 text: {e}") from None
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    if "time" not in header or "electricity" not in header:
        preview = text[:100].replace("\n", " | ")
        raise DataError(f"{name}: expected 'time' and 'electricity' columns, got {preview!r}")
    time_i, value_i = header.index("time"), header.index("electricity")
    expected_hours = 8784 if calendar.isleap(year) else 8760
    start = datetime(year, 1, 1)
    max_value = CAPACITY_KW * (1 + CAPACITY_TOLERANCE)
    hours = 0
    for row in reader:
        if not row:
            continue
        if hours >= expected_hours:
            raise DataError(f"{name}: more than the {expected_hours} hours of {year}")
        where = f"{name} hour {hours + 1}"
        if len(row) <= max(time_i, value_i):
            raise DataError(f"{where}: short row {row}")
        expected_time = start + timedelta(hours=hours)
        try:
            actual_time = datetime.fromisoformat(row[time_i][:16])
        except ValueError:
            raise DataError(f"{where}: unreadable time {row[time_i]!r}") from None
        if actual_time != expected_time:
            raise DataError(f"{where}: expected time {expected_time}, got {row[time_i]!r}")
        try:
            value = float(row[value_i])
        except ValueError:
            raise DataError(f"{where}: bad electricity value {row[value_i]!r}") from None
        if not 0 <= value <= max_value:  # also rejects NaN
            raise DataError(f"{where}: electricity {value} outside 0..{CAPACITY_KW}")
        hours += 1
    if hours != expected_hours:
        raise DataError(f"{name}: expected {expected_hours} hours for {year}, got {hours}")
