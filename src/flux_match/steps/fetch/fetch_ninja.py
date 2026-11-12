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

collate() then combines the raw files into `data/ninja/wind_capacity_factor_<year>.csv`, laid out
like CODERS' wind_capacity_factor table: an `h` column (hour 1-8760) and one column per grid cell,
in grid_cell_info order, holding the capacity factor (electricity / CAPACITY_KW). February 29 is
dropped in leap years. Cells without a raw file yet are filled with 0 and counted in a warning,
so a partial download can be inspected; it is rebuilt when a raw file is newer. fetch.py only
derives anything from it once the download is complete.
The file is far too big to build in memory, so it is written CHUNK_ROWS hours at a time, reading
only that slice of each raw file.
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
from typing import Any, BinaryIO, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError, FetchIncompleteError, report_data_error
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.fetch.fetch import (
    _already_present,
    _http_error_body,
    _message_from_body,
    _up_to_date,
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
CHUNK_ROWS = 5 * 24  # hours of every raw file held in memory at once while collating

GRID_CELL_COLUMNS = ("grid_cell", "latitude", "longitude")

Opener = Callable[..., Any]


@dataclass(frozen=True)
class NinjaParams:
    turbine_height: int
    turbine_model: str
    request_sleep_s: float
    max_requests_per_hour: int

    @classmethod
    def from_config(cls, params: Config) -> Self:
        parsed = cls(
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


def raw_dir(params: NinjaParams, year: int) -> Path:
    """Folder of raw files for one turbine model, height and year."""
    name = f"{_slug(params.turbine_model)}_{params.turbine_height}m_{year}"
    return paths.NINJA_DATA_DIR / RAW_SUBDIR / name


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
    directory = raw_dir(params, year)
    missing = [
        (c, raw_path(directory, c)) for c in cells if not _already_present(raw_path(directory, c))
    ]
    print(
        f"NINJA wind: {len(cells) - len(missing)} of {len(cells)} cells already downloaded "
        f"({directory.relative_to(paths.NINJA_DATA_DIR)})",
        flush=True,
    )
    if not missing:
        return
    token = _api_token(environ)
    directory.mkdir(parents=True, exist_ok=True)
    limiter = RequestLimiter(
        paths.NINJA_DATA_DIR / REQUEST_LOG_NAME, params.max_requests_per_hour, clock
    )
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
        _fetch_one(params, year, cell, dest, token, get, limiter, sleep=sleep)


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
    params: NinjaParams,
    year: int,
    cell: Cell,
    token: str,
    opener: Opener,
    limiter: RequestLimiter,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    request = Request(
        f"{API_URL}?{urlencode(_query(params, year, cell))}",
        headers={"Authorization": f"Token {token}", "User-Agent": USER_AGENT},
    )
    where = f"cell {cell.grid_cell} ({cell.latitude}, {cell.longitude})"
    retried = False
    while True:
        limiter.record()
        try:
            with opener(request, timeout=TIMEOUT_S) as response:
                return response.read()
        except HTTPError as e:
            detail = _message_from_body(_http_error_body(e))
            suffix = f": {detail}" if detail else ""
            if e.code == 502 and not retried:
                # Transient gateway errors often succeed on a single retry.
                sleep(params.request_sleep_s)
                retried = True
                continue
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
                    f"NINJA rate limit hit (HTTP 429) at {where} after {limiter.count} requests "
                    f"in the last hour{suffix}. Wait before rerunning the fetch."
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
    limiter: RequestLimiter,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    body = _request(params, year, cell, token, opener, limiter, sleep=sleep)
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


def collated_path(year: int) -> Path:
    return paths.NINJA_DATA_DIR / f"wind_capacity_factor_{year}.csv"


def collate(params: NinjaParams, year: int, grid_cell_info: Path) -> None:
    """Write wind_capacity_factor_<year>.csv from the raw files downloaded so far.

    Skipped if there are no raw files, or the output is newer than all of them. Cells that have
    no raw file are filled with capacity factor 0 and reported in a warning.
    """
    cells = read_cells(grid_cell_info)
    directory = raw_dir(params, year)
    dest = collated_path(year)
    present = [c for c in cells if _already_present(raw_path(directory, c))]
    if not present:
        print(f"NINJA wind: no raw files yet, not writing {dest.name}", flush=True)
        return
    if _up_to_date(dest, [raw_path(directory, c) for c in present]):
        print(f"NINJA wind: {dest.name} is up to date", flush=True)
        return
    print(
        f"writing {dest.name} from {len(present)} of {len(cells)} cells "
        f"({directory.relative_to(paths.NINJA_DATA_DIR)})",
        flush=True,
    )
    readers = {c.grid_cell: _RawReader(raw_path(directory, c)) for c in present}
    _write_collated(dest, cells, readers, year)
    if len(present) < len(cells):
        report_data_error(
            f"{dest.name}: {len(cells) - len(present)} of {len(cells)} cells have no NINJA data "
            f"yet and are filled with capacity factor 0; rerun the fetch to download them"
        )


def _expected_times(year: int) -> list[str]:
    """The `YYYY-MM-DD HH:MM` of each of the 8760 output hours, without February 29."""
    start = datetime(year, 1, 1)
    hours = (start + timedelta(hours=h) for h in range(HOURS_PER_YEAR + 24))
    times = [f"{t:%Y-%m-%d %H:%M}" for t in hours if (t.month, t.day) != (2, 29)]
    return times[:HOURS_PER_YEAR]


def _write_collated(
    dest: Path, cells: list[Cell], readers: dict[str, _RawReader], year: int
) -> None:
    """Write the table CHUNK_ROWS rows at a time: each chunk reads that slice of every raw file
    into memory, writes it, and is dropped. Written to a temporary file, then renamed."""
    times = _expected_times(year)
    skip_leap_day = calendar.isleap(year)
    zeros = ["0"] * CHUNK_ROWS
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tmp.open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["h", *(c.grid_cell for c in cells)])
            for first in range(0, HOURS_PER_YEAR, CHUNK_ROWS):
                chunk_times = times[first : first + CHUNK_ROWS]
                columns = [
                    readers[c.grid_cell].read(chunk_times, first, skip_leap_day)
                    if c.grid_cell in readers
                    else zeros
                    for c in cells
                ]
                for i in range(len(chunk_times)):
                    writer.writerow([first + i + 1, *(column[i] for column in columns)])
        for reader in readers.values():
            reader.check_finished()
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


class _RawReader:
    """Reads one raw file a slice of hours at a time, remembering where the last slice ended so
    that no file needs to stay open (or be re-read from the start) between slices."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._columns: tuple[int, int] | None = None  # indexes of `time` and `electricity`

    def read(self, times: list[str], first_hour: int, skip_leap_day: bool) -> list[str]:
        """Capacity factors for the next `times`, which start at output hour `first_hour` + 1.
        Fails unless each row's time matches. A leap year's February 29 rows are passed over."""
        values = []
        with self.path.open("rb") as f:
            f.seek(self._offset)
            if self._columns is None:
                self._columns = self._read_header(f)
            time_i, value_i = self._columns
            for n, expected in enumerate(times, start=first_hour + 1):
                row = self._next_row(f, n, skip_leap_day, time_i)
                where = f"{self.path.name} hour {n}"
                if len(row) <= max(time_i, value_i):
                    raise DataError(f"{where}: short row {row}")
                if row[time_i][:16] != expected:
                    raise DataError(f"{where}: expected time {expected}, got {row[time_i]!r}")
                values.append(_capacity_factor(row[value_i], where))
            self._offset = f.tell()
        return values

    def check_finished(self) -> None:
        """Fail if the file has anything left after the last hour."""
        with self.path.open("rb") as f:
            f.seek(self._offset)
            if f.read().strip():
                raise DataError(f"{self.path.name}: more than the {HOURS_PER_YEAR} expected hours")

    def _read_header(self, f: BinaryIO) -> tuple[int, int]:
        header = next(csv.reader([f.readline().decode("utf-8")]), [])
        if "time" not in header or "electricity" not in header:
            raise DataError(f"{self.path.name}: expected 'time' and 'electricity' columns")
        return header.index("time"), header.index("electricity")

    def _next_row(self, f: BinaryIO, hour: int, skip_leap_day: bool, time_i: int) -> list[str]:
        while True:
            line = f.readline()
            if not line:
                raise DataError(f"{self.path.name}: file ends before hour {hour}")
            if not line.strip():
                continue
            row = next(csv.reader([line.decode("utf-8")]))
            if skip_leap_day and len(row) > time_i and row[time_i][5:10] == "02-29":
                continue
            return row


def _capacity_factor(text: str, where: str) -> str:
    """Output in kW as a capacity factor, to NINJA's precision (0.001 kW of CAPACITY_KW)."""
    try:
        value = float(text)
    except ValueError:
        raise DataError(f"{where}: bad electricity value {text!r}") from None
    if not 0 <= value <= CAPACITY_KW * (1 + CAPACITY_TOLERANCE):  # also rejects NaN
        raise DataError(f"{where}: electricity {value} outside 0..{CAPACITY_KW}")
    return f"{min(value / CAPACITY_KW, 1.0):.8f}".rstrip("0").rstrip(".")
