"""Download raw tables from the CODERS API into a local cache directory.

Authentication, HTTP, and response deserialization live here so other sources can be
added beside this module without sharing that logic. Shared fetch helpers (cache checks,
leap-day normalization, env-file parsing) come from fetch.py.
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flux_match.errors import DataError
from flux_match.io import write_csv
from flux_match.steps.fetch.fetch import (
    CAPACITY_FACTOR_TABLES,
    GRID_CELL_TABLE,
    FetchParams,
    _already_present,
    _drop_leap_day,
    _fill_missing_from_previous_hour,
    _read_env_file,
    csv_filename,
)

API_BASE = "https://api.sesit.ca"
API_KEY_ENV = "CODERS_API_KEY"
TIMEOUT_S = 120
CHUNK_SIZE = 1024 * 1024

DEMAND_TABLE = "provincial_demand"

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
class Download:
    table: str
    params: dict[str, str | int]
    required_columns: tuple[str, ...] | None = None
    hour_column: str | None = None
    # Blank values in these columns are copied from the previous hour. Needs hour_column.
    fill_columns: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return csv_filename(self.table, **self.params)


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
                    fill_columns=("demand_MWh",),
                )
            )
    for year in params.capacity_factor_years:
        for table in CAPACITY_FACTOR_TABLES:
            items.append(Download(table, {"year": year}, hour_column="h"))
    return items


def download(
    params: FetchParams,
    data_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
    opener: Opener | None = None,
) -> None:
    """Download any missing CODERS tables into data_dir."""
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
        print(f"downloading {dest.name}", flush=True)
        _fetch_one(item, dest, api_key, get)


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
    columns = set(records[0])
    for i, row in enumerate(records):
        if set(row) != columns:
            raise DataError(
                f"CODERS {item.table} ({_params_desc(item.params)}) row {i} has different "
                f"columns from row 0: missing {sorted(columns - set(row))}, "
                f"extra {sorted(set(row) - columns)}"
            )
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


def _fetch_one(item: Download, dest: Path, api_key: str, opener: Opener) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with _open(item, api_key, opener) as response:
            if _is_csv_response(response.headers):
                _stream_to_file(response, tmp)
            else:
                records = _json_records(response.read(), item)
                if item.fill_columns:
                    _fill_missing_from_previous_hour(
                        records, item.hour_column, item.fill_columns, dest.name
                    )
                write_csv(tmp, tuple(records[0].keys()), records)
        if item.hour_column:
            _drop_leap_day(tmp, item.hour_column, dest.name)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
