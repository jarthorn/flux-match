import calendar
import csv
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from email.message import EmailMessage
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from test_fetch import FakeResponse

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError, FetchIncompleteError
from flux_match.steps.fetch.fetch_ninja import (
    API_TOKEN_ENV,
    CAPACITY_KW,
    CHUNK_ROWS,
    NinjaParams,
    RequestLimiter,
    collate,
    collated_path,
    download,
    raw_dir,
    read_cells,
    validate_raw,
)

TOKEN = "super-secret-ninja-token"
YEAR = 2025
HOUR = 3600
START = 1_800_000_000.0  # arbitrary epoch time for the fake clock

PARAMS = NinjaParams(
    turbine_height=105,
    turbine_model="Vestas V150 4500",
    request_sleep_s=5,
    max_requests_per_hour=50,
)


def ninja_csv(year: int = YEAR, hours: int | None = None, value: str = "1234.5") -> bytes:
    """A response body: `hours` hourly rows of the year (all of them by default)."""
    if hours is None:
        hours = 8784 if year % 4 == 0 else 8760
    start = datetime(year, 1, 1)
    rows = (f"{start + timedelta(hours=h):%Y-%m-%d %H:%M},{value}\n" for h in range(hours))
    return ("time,electricity\n" + "".join(rows)).encode()


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingOpener:
    """Serves ninja_csv() for every request unless a failure is given, and records requests.

    `failure` is raised on every request. `failures` is a finite sequence raised in order; after
    it is exhausted, later requests succeed (or raise `failure` if that is also set).
    """

    def __init__(
        self,
        failure: HTTPError | None = None,
        *,
        failures: list[HTTPError] | None = None,
    ) -> None:
        self.failure = failure
        self._failures = list(failures or [])
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self._failures:
            raise self._failures.pop(0)
        if self.failure:
            raise self.failure
        return FakeResponse(ninja_csv(), content_type="text/csv")

    @property
    def cells(self) -> list[str]:
        """The lat,lon of each request, in order."""
        return [
            "{lat},{lon}".format(
                **{k: v[0] for k, v in parse_qs(urlparse(r.full_url).query).items()}
            )
            for r in self.requests
        ]


def http_error(code: int, body: bytes = b"") -> HTTPError:
    url = f"https://www.renewables.ninja/api/data/wind?token={TOKEN}"
    return HTTPError(url, code, "error", EmailMessage(), BytesIO(body))


class NinjaTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(paths, "DATA_DIR", self.dir / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ninja_dir = paths.NINJA_DATA_DIR
        self.ninja_dir.mkdir(parents=True)
        self.grid = self.dir / "grid_cell_info.csv"
        self.write_grid(3)
        self.clock = Clock()
        self.sleeps: list[float] = []

    def write_grid(self, cells: int) -> None:
        rows = [f"{n},{-100 + n}.500,{40 + n}.250,Ontario.a" for n in range(1, cells + 1)]
        self.grid.write_text(
            "grid_cell,longitude,latitude,balancing_area\n" + "\n".join(rows) + "\n"
        )

    def download(self, opener, params: NinjaParams = PARAMS, environ=None) -> str:
        stdout = StringIO()
        with redirect_stdout(stdout):
            download(
                params,
                YEAR,
                self.grid,
                environ={API_TOKEN_ENV: TOKEN} if environ is None else environ,
                opener=opener,
                clock=self.clock,
                sleep=self.sleeps.append,
            )
        return stdout.getvalue()

    @property
    def raw(self) -> Path:
        return raw_dir(PARAMS, YEAR)

    def raw_names(self) -> list[str]:
        return sorted(p.name for p in self.raw.iterdir())


class DownloadTest(NinjaTestCase):
    def test_downloads_one_file_per_cell_with_expected_request(self):
        opener = RecordingOpener()
        self.download(opener)

        self.assertEqual(self.raw.name, "vestas_v150_4500_105m_2025")
        self.assertEqual(self.raw_names(), ["wind_1.csv", "wind_2.csv", "wind_3.csv"])
        self.assertEqual((self.raw / "wind_2.csv").read_bytes(), ninja_csv())
        request = opener.requests[1]
        url = urlparse(request.full_url)
        self.assertEqual(
            f"{url.scheme}://{url.netloc}{url.path}", "https://www.renewables.ninja/api/data/wind"
        )
        self.assertEqual(
            {k: v[0] for k, v in parse_qs(url.query).items()},
            {
                "lat": "42.25",
                "lon": "-98.5",
                "date_from": "2025-01-01",
                "date_to": "2025-12-31",
                "capacity": str(CAPACITY_KW),
                "dataset": "merra2",
                "height": "105",
                "turbine": "Vestas V150 4500",
                "format": "csv",
                "header": "false",
                "local_time": "false",
                "raw": "false",
                "interpolate": "false",
            },
        )
        self.assertEqual(request.get_header("Authorization"), f"Token {TOKEN}")
        self.assertEqual(request.get_header("User-agent"), "flux-match/1.0")
        self.assertNotIn(TOKEN, request.full_url)

    def test_existing_files_are_skipped_and_empty_ones_redownloaded(self):
        self.raw.mkdir(parents=True)
        (self.raw / "wind_1.csv").write_text("cached\n")
        (self.raw / "wind_3.csv").write_text("")
        opener = RecordingOpener()
        output = self.download(opener)

        self.assertEqual(opener.cells, ["42.25,-98.5", "43.25,-97.5"])
        self.assertEqual((self.raw / "wind_1.csv").read_text(), "cached\n")
        self.assertIn("1 of 3 cells already downloaded", output)

        opener.requests.clear()
        self.download(opener, environ={})  # nothing missing: no token or request needed
        self.assertEqual(opener.requests, [])

    def test_sleeps_between_requests_only(self):
        self.download(RecordingOpener())
        self.assertEqual(self.sleeps, [5, 5])

    def test_files_are_named_by_turbine_settings_and_year(self):
        other = NinjaParams(100, "Gamesa G114 2000", 5, 50)
        self.assertEqual(raw_dir(other, 2024).name, "gamesa_g114_2000_100m_2024")
        self.assertEqual(raw_dir(other, 2024).parent, paths.NINJA_DATA_DIR / "raw")

    def test_missing_token_fails_before_any_request(self):
        opener = RecordingOpener()
        with self.assertRaisesRegex(DataError, API_TOKEN_ENV):
            self.download(opener, environ={})
        self.assertEqual(opener.requests, [])

    def test_invalid_response_is_not_stored(self):
        class BadOpener(RecordingOpener):
            def __call__(self, request, timeout=None):
                self.requests.append(request)
                return FakeResponse(ninja_csv(hours=100), content_type="text/csv")

        with self.assertRaisesRegex(
            DataError, r"wind_1\.csv: expected 8760 hours for 2025, got 100"
        ):
            self.download(BadOpener())
        self.assertEqual(self.raw_names(), [])

    def test_rejected_token_fails_without_leaking_it(self):
        opener = RecordingOpener(http_error(401, b'{"detail": "Invalid token."}'))
        with self.assertRaises(DataError) as caught:
            self.download(opener)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("Invalid token.", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_http_403_is_reported_as_refused_not_only_a_bad_token(self):
        opener = RecordingOpener(http_error(403, b"error code: 1010"))
        with self.assertRaisesRegex(
            DataError, r"refused the request \(HTTP 403\): error code: 1010"
        ):
            self.download(opener)
        self.assertEqual(self.raw_names(), [])

    def test_other_http_error_names_the_cell(self):
        with self.assertRaisesRegex(DataError, r"HTTP 500 for cell 1 \(41\.25, -99\.5\)"):
            self.download(RecordingOpener(http_error(500)))
        self.assertEqual(self.raw_names(), [])

    def test_http_429_stops_the_run_as_incomplete(self):
        opener = RecordingOpener(http_error(429, b'{"detail": "Request was throttled."}'))
        with self.assertRaisesRegex(FetchIncompleteError, "HTTP 429.*Request was throttled"):
            self.download(opener)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.raw_names(), [])

    def test_http_502_is_retried_once_after_sleep(self):
        opener = RecordingOpener(failures=[http_error(502)])
        self.write_grid(1)
        self.download(opener)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(self.raw_names(), ["wind_1.csv"])

    def test_two_http_502_in_a_row_aborts(self):
        opener = RecordingOpener(failures=[http_error(502), http_error(502)])
        with self.assertRaisesRegex(DataError, r"HTTP 502 for cell 1 \(41\.25, -99\.5\)"):
            self.download(opener)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(self.raw_names(), [])


class RateLimitTest(NinjaTestCase):
    def limited(self, max_requests: int) -> NinjaParams:
        return NinjaParams(105, "Vestas V150 4500", 5, max_requests)

    def test_stops_at_the_hourly_cap_with_a_clear_message(self):
        self.write_grid(5)
        opener = RecordingOpener()
        with self.assertRaises(FetchIncompleteError) as caught:
            self.download(opener, self.limited(2))
        message = str(caught.exception)
        self.assertIn("limit of 2", message)
        self.assertIn("3 of 5 cells still to download", message)
        self.assertIn("rerun", message)
        self.assertEqual(self.raw_names(), ["wind_1.csv", "wind_2.csv"])
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [5])  # no sleep before stopping

    def test_cap_includes_requests_from_earlier_runs_until_they_leave_the_window(self):
        self.write_grid(5)
        with self.assertRaises(FetchIncompleteError):
            self.download(RecordingOpener(), self.limited(2))

        self.clock.now += 10 * 60
        opener = RecordingOpener()
        with self.assertRaisesRegex(FetchIncompleteError, "3 of 5 cells"):
            self.download(opener, self.limited(2))
        self.assertEqual(opener.requests, [])

        self.clock.now = START + HOUR + 1
        with self.assertRaisesRegex(FetchIncompleteError, "1 of 5 cells"):
            self.download(opener, self.limited(2))
        self.assertEqual(len(opener.requests), 2)
        self.clock.now += HOUR + 1
        self.download(opener, self.limited(2))
        self.assertEqual(self.raw_names(), [f"wind_{n}.csv" for n in range(1, 6)])

    def test_failed_requests_count_toward_the_cap(self):
        with self.assertRaises(DataError):
            self.download(RecordingOpener(http_error(500)))
        limiter = RequestLimiter(self.ninja_dir / ".requests.log", 50, self.clock)
        self.assertEqual(limiter.count, 1)

    def test_502_retry_counts_toward_the_cap(self):
        with self.assertRaises(DataError):
            self.download(RecordingOpener(failures=[http_error(502), http_error(502)]))
        limiter = RequestLimiter(self.ninja_dir / ".requests.log", 50, self.clock)
        self.assertEqual(limiter.count, 2)

    def test_limiter_is_a_rolling_window(self):
        log = self.dir / "requests.log"
        log.write_text("".join(f"{START - offset!r}\n" for offset in (10, 1000, 2000, 5000)))
        limiter = RequestLimiter(log, 3, self.clock)
        # The 5000 s old request is outside the window and is dropped from the file too.
        self.assertEqual(limiter.count, 3)
        self.assertEqual(len(log.read_text().splitlines()), 3)
        # Back under the cap once the oldest request in the window is an hour old.
        self.assertEqual(limiter.next_allowed(), START - 2000 + HOUR)
        self.clock.now = START - 2000 + HOUR + 1
        self.assertIsNone(limiter.next_allowed())
        limiter.record()
        self.assertEqual(limiter.next_allowed(), START - 1000 + HOUR)

    def test_limiter_needs_enough_requests_to_expire_when_over_the_cap(self):
        log = self.dir / "requests.log"
        log.write_text("".join(f"{START - offset!r}\n" for offset in (300, 200, 100)))
        limiter = RequestLimiter(log, 2, self.clock)  # e.g. max_requests_per_hour was lowered
        self.assertEqual(limiter.next_allowed(), START - 200 + HOUR)

    def test_corrupt_request_log_fails(self):
        log = self.dir / "requests.log"
        log.write_text("not a time\n")
        with self.assertRaisesRegex(DataError, "line 1"):
            RequestLimiter(log, 50, self.clock)


class ValidateRawTest(unittest.TestCase):
    def check(self, body: bytes, year: int = YEAR) -> None:
        validate_raw(body, year, "wind_1.csv")

    def test_full_years_pass_including_leap_year(self):
        self.check(ninja_csv(2025))
        self.check(ninja_csv(2024), 2024)
        self.check(ninja_csv(value=str(CAPACITY_KW)))
        self.check(ninja_csv(value="0.0"))

    def test_extra_columns_and_trailing_blank_lines_allowed(self):
        rows = ninja_csv().decode().splitlines()
        wide = ["local_time,time,electricity"] + [f"x,{row}" for row in rows[1:]]
        self.check(("\n".join(wide) + "\n\n").encode())

    def test_wrong_hour_count_fails(self):
        with self.assertRaisesRegex(DataError, "expected 8760 hours for 2025, got 8759"):
            self.check(ninja_csv(hours=8759))
        with self.assertRaisesRegex(DataError, "more than the 8760 hours"):
            self.check(ninja_csv() + b"2026-01-01 00:00,1\n")
        with self.assertRaisesRegex(DataError, "expected 8784 hours for 2024, got 8760"):
            self.check(ninja_csv(2024, hours=8760), 2024)

    def test_wrong_year_or_gap_fails(self):
        with self.assertRaisesRegex(DataError, "hour 1: expected time 2025-01-01 00:00:00"):
            self.check(ninja_csv(2024))
        lines = ninja_csv().decode().splitlines()
        del lines[11]
        with self.assertRaisesRegex(DataError, "hour 11: expected time 2025-01-01 10:00:00"):
            self.check(("\n".join(lines) + "\n").encode())

    def test_bad_values_fail(self):
        for value in ["", "abc", "nan", "-1", str(CAPACITY_KW * 1.01)]:
            with self.subTest(value=value), self.assertRaisesRegex(DataError, "hour 1:"):
                self.check(ninja_csv(value=value))

    def test_missing_columns_or_non_csv_fails(self):
        with self.assertRaisesRegex(DataError, "expected 'time' and 'electricity' columns"):
            self.check(b'{"detail": "Invalid token."}')
        with self.assertRaisesRegex(DataError, "expected 'time' and 'electricity' columns"):
            self.check(b"")
        with self.assertRaisesRegex(DataError, "not UTF-8"):
            self.check(b"\xff\xfe")


def indexed_csv(year: int = YEAR, scale: int = 1) -> bytes:
    """A response body whose value (kW) is `scale` times the zero-based hour of the year."""
    hours = 8784 if calendar.isleap(year) else 8760
    start = datetime(year, 1, 1)
    rows = (f"{start + timedelta(hours=h):%Y-%m-%d %H:%M},{h * scale}\n" for h in range(hours))
    return ("time,electricity\n" + "".join(rows)).encode()


class CollateTest(NinjaTestCase):
    def write_raw(self, cell: int, body: bytes, year: int = YEAR) -> Path:
        directory = raw_dir(PARAMS, year)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"wind_{cell}.csv"
        path.write_bytes(body)
        return path

    def collate(self, year: int = YEAR) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            collate(PARAMS, year, self.grid)
        return out.getvalue(), err.getvalue()

    def output(self, year: int = YEAR) -> list[list[str]]:
        with collated_path(year).open(newline="") as f:
            return list(csv.reader(f))

    def test_collates_capacity_factors_in_grid_order(self):
        self.write_raw(1, ninja_csv(value="50000"))
        self.write_raw(2, ninja_csv(value="25000.5"))
        self.write_raw(3, ninja_csv(value=str(CAPACITY_KW)))
        out, err = self.collate()

        rows = self.output()
        self.assertEqual(collated_path(YEAR).name, "wind_capacity_factor_2025.csv")
        self.assertEqual(rows[0], ["h", "1", "2", "3"])
        self.assertEqual(len(rows), 8761)
        self.assertEqual(rows[1], ["1", "0.5", "0.250005", "1"])
        self.assertEqual(rows[-1], ["8760", "0.5", "0.250005", "1"])
        self.assertEqual([r[0] for r in rows[1:]], [str(h) for h in range(1, 8761)])
        self.assertIn("writing wind_capacity_factor_2025.csv from 3 of 3 cells", out)
        self.assertEqual(err, "")
        self.assertEqual(list(self.ninja_dir.glob("*.tmp")), [])

    def test_grid_order_decides_column_order_not_download_order(self):
        self.write_grid(3)
        self.write_raw(3, ninja_csv(value="30000"))
        self.write_raw(1, ninja_csv(value="10000"))
        self.write_raw(2, ninja_csv(value="20000"))
        self.collate()
        self.assertEqual(self.output()[1], ["1", "0.1", "0.2", "0.3"])

    def test_missing_cells_are_zero_with_a_warning(self):
        self.write_raw(2, ninja_csv(value="50000"))
        out, err = self.collate()

        rows = self.output()
        self.assertEqual(rows[0], ["h", "1", "2", "3"])
        self.assertEqual(rows[1], ["1", "0", "0.5", "0"])
        self.assertEqual(rows[-1], ["8760", "0", "0.5", "0"])
        self.assertIn("from 1 of 3 cells", out)
        self.assertIn("2 of 3 cells have no NINJA data yet", err)

    def test_nothing_is_written_without_any_raw_files(self):
        out, err = self.collate()
        self.assertIn("no raw files yet", out)
        self.assertEqual(err, "")
        self.assertFalse(collated_path(YEAR).exists())

    def test_hours_are_read_across_chunk_boundaries(self):
        self.write_raw(1, indexed_csv())
        self.write_raw(2, indexed_csv(scale=2))
        self.collate()
        rows = self.output()
        for h in (1, 2, CHUNK_ROWS, CHUNK_ROWS + 1, 2 * CHUNK_ROWS + 1, 4000, 8760):
            cells = [round(float(v) * CAPACITY_KW) for v in rows[h][1:3]]
            self.assertEqual(cells, [h - 1, 2 * (h - 1)], f"hour {h}")

    def test_every_row_matches_its_source_hour(self):
        self.write_raw(1, indexed_csv())
        self.collate()
        values = [float(r[1]) * CAPACITY_KW for r in self.output()[1:]]
        self.assertEqual([round(v) for v in values], list(range(8760)))

    def test_leap_day_is_dropped_and_later_hours_renumbered(self):
        self.write_raw(1, indexed_csv(2024), 2024)
        self.write_raw(2, ninja_csv(2024, value="50000"), 2024)
        self.collate(2024)
        rows = self.output(2024)
        self.assertEqual(len(rows), 8761)
        values = [round(float(r[1]) * CAPACITY_KW) for r in rows[1:]]
        # Hours 1-1416 are Jan 1 - Feb 28; hour 1417 is Mar 1 00:00, raw hour 1440 (0-based).
        self.assertEqual(values[1415], 1415)
        self.assertEqual(values[1416], 1440)
        self.assertEqual(values[-1], 8783)
        self.assertEqual(values[:1416], list(range(1416)))
        self.assertEqual({r[2] for r in rows[1:]}, {"0.5"})

    def test_up_to_date_output_is_not_rebuilt(self):
        paths_ = [self.write_raw(n, ninja_csv(value="50000")) for n in (1, 2, 3)]
        self.collate()
        dest = collated_path(YEAR)
        dest.write_text("sentinel\n")
        newest = max(p.stat().st_mtime for p in paths_)

        os.utime(dest, (newest + 10, newest + 10))
        out, _ = self.collate()
        self.assertIn("up to date", out)
        self.assertEqual(dest.read_text(), "sentinel\n")

        os.utime(dest, (newest - 10, newest - 10))
        self.collate()
        self.assertEqual(self.output()[0], ["h", "1", "2", "3"])

    def test_new_raw_file_replaces_zeros_on_the_next_collate(self):
        self.write_raw(1, ninja_csv(value="50000"))
        _, err = self.collate()
        self.assertIn("2 of 3 cells", err)
        dest = collated_path(YEAR)
        old = dest.stat().st_mtime
        os.utime(dest, (old - 100, old - 100))

        self.write_raw(3, ninja_csv(value="100000"))
        _, err = self.collate()
        self.assertIn("1 of 3 cells", err)
        self.assertEqual(self.output()[1], ["1", "0.5", "0", "1"])

    def test_incomplete_or_malformed_raw_files_fail_without_output(self):
        for body, message in [
            (ninja_csv(hours=100), r"wind_2\.csv: file ends before hour 101"),
            (ninja_csv(value="-5"), r"wind_2\.csv hour 1: electricity -5"),
            (ninja_csv(value="abc"), r"wind_2\.csv hour 1: bad electricity value 'abc'"),
            (ninja_csv(2024), r"wind_2\.csv hour 1: expected time 2025-01-01 00:00"),
            (ninja_csv() + b"2026-01-01 00:00,1\n", r"wind_2\.csv: more than the 8760"),
            (b"a,b\n1,2\n", r"wind_2\.csv: expected 'time' and 'electricity' columns"),
        ]:
            with self.subTest(message=message):
                self.write_raw(1, ninja_csv())
                self.write_raw(2, body)
                with self.assertRaisesRegex(DataError, message):
                    self.collate()
                self.assertEqual(list(self.ninja_dir.glob("wind_capacity_factor*")), [])

    def test_gap_in_hours_fails(self):
        lines = ninja_csv().decode().splitlines()
        del lines[5000]
        self.write_raw(1, ("\n".join(lines) + "\n").encode())
        with self.assertRaisesRegex(DataError, r"wind_1\.csv hour 5000: expected time"):
            self.collate()

    def test_empty_raw_file_counts_as_missing(self):
        self.write_raw(1, ninja_csv(value="50000"))
        self.write_raw(2, b"")
        _, err = self.collate()
        self.assertIn("2 of 3 cells have no NINJA data", err)


class ReadCellsTest(NinjaTestCase):
    def test_reads_ids_and_coordinates_in_file_order(self):
        cells = read_cells(self.grid)
        self.assertEqual([c.grid_cell for c in cells], ["1", "2", "3"])
        self.assertEqual((cells[0].latitude, cells[0].longitude), (41.25, -99.5))

    def test_bad_grid_files_fail(self):
        for text, message in [
            ("grid_cell,latitude\n1,40\n", "missing required columns"),
            ("grid_cell,latitude,longitude\n", "no grid cells"),
            ("grid_cell,latitude,longitude\n1,40,-80\n1,41,-81\n", "duplicate grid cells"),
            ("grid_cell,latitude,longitude\n1,abc,-80\n", "line 2: bad coordinates"),
            ("grid_cell,latitude,longitude\n1,95,-80\n", "line 2: bad cell"),
        ]:
            with self.subTest(message=message):
                self.grid.write_text(text)
                with self.assertRaisesRegex(DataError, message):
                    read_cells(self.grid)

    def test_missing_grid_file_fails(self):
        self.grid.unlink()
        with self.assertRaisesRegex(DataError, "CODERS download must run first"):
            read_cells(self.grid)


class NinjaParamsTest(unittest.TestCase):
    TABLE = {
        "turbine_height": 105,
        "turbine_model": "Vestas V150 4500",
        "request_sleep_s": 5,
        "max_requests_per_hour": 50,
    }

    def parse(self, **changes) -> NinjaParams:
        return NinjaParams.from_config(Config({**self.TABLE, **changes}, Path("test.toml")))

    def test_invalid_values_are_rejected(self):
        for changes, message in [
            ({"turbine_height": 0}, "'turbine_height' must be positive"),
            ({"turbine_model": " !! "}, "'turbine_model' must contain letters or digits"),
            ({"request_sleep_s": 0.5}, "'request_sleep_s' must be at least 1"),
            ({"max_requests_per_hour": 0}, "'max_requests_per_hour' must be at least 1"),
            ({"turbine_height": "tall"}, "must be int"),
        ]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ConfigError, message):
                self.parse(**changes)


if __name__ == "__main__":
    unittest.main()
