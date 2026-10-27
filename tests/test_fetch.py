import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from email.message import EmailMessage
from io import BytesIO, StringIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.fetch import (
    API_KEY_ENV,
    FetchParams,
    _read_env_file,
    csv_path,
    downloads,
    run,
)

SOURCE = Path("test.toml")
SECRET = "super-secret-test-key"
LEAP_YEAR_HOURS = 8784

GRID_ROW = {
    "grid_cell": 1,
    "longitude": "-79.0",
    "latitude": "43.5",
    "balancing_area": "Ontario.a",
    "province": "Ontario",
    "distance_to_grid": "1.0",
    "population": 10,
    "surface_area_offshore": "0.00",
    "surface_area_onshore": "12.0",
}


def config(data: dict) -> Config:
    return Config(data, SOURCE)


def fetch_config(
    provinces: list[str] | None = None,
    demand_years: list[int] | None = None,
    capacity_factor_years: list[int] | None = None,
) -> Config:
    return config(
        {
            "fetch": {
                "provinces": provinces or ["ON"],
                "demand_years": demand_years or [2024],
                "capacity_factor_years": capacity_factor_years or [2021],
            }
        }
    )


def demand_rows(year: int, hours: int) -> list[dict]:
    start = datetime(year, 1, 1)
    rows = []
    for h in range(hours):
        t = start + timedelta(hours=h)
        rows.append(
            {
                "local_time": f"{t:%Y-%m-%d %H:%M:%S}",
                "province": "ON",
                "daily_hour_ending": t.hour + 1,
                "annual_hour_ending": h + 1,
                "demand_MWh": 100 + h,
            }
        )
    return rows


def capacity_factor_csv(hours: int, value: str = "0.2") -> bytes:
    return ("h,1\n" + "".join(f"{h},{value}\n" for h in range(1, hours + 1))).encode()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._buf = BytesIO(body)
        self.status = 200
        self.headers = {"Content-Type": content_type}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._buf.read()
        return self._buf.read(n)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def json_body(rows: list[dict]) -> bytes:
    return json.dumps(rows).encode()


def responses(
    grid: list[dict] | None = None,
    demand: list[dict] | None = None,
    solar: bytes | None = None,
    wind: bytes | None = None,
) -> dict[tuple[str, tuple[tuple[str, str], ...]], FakeResponse]:
    """Responses for every download in fetch_config(), non-leap-year by default."""
    return {
        ("grid_cell_info", ()): FakeResponse(json_body(grid or [GRID_ROW])),
        ("provincial_demand", (("province", "ON"), ("year", "2024"))): FakeResponse(
            json_body(demand or demand_rows(2023, HOURS_PER_YEAR))
        ),
        ("solar_capacity_factor", (("year", "2021"),)): FakeResponse(
            solar or capacity_factor_csv(HOURS_PER_YEAR), content_type="text/csv"
        ),
        ("wind_capacity_factor", (("year", "2021"),)): FakeResponse(
            wind or capacity_factor_csv(HOURS_PER_YEAR, "0.3"), content_type="text/csv"
        ),
    }


class RecordingOpener:
    def __init__(
        self, responses: dict[tuple[str, tuple[tuple[str, str], ...]], FakeResponse]
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request, timeout=None):
        parsed = urlparse(request.full_url)
        table = parsed.path.lstrip("/")
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        key = query.pop("key", None)
        self.calls.append((table, query))
        if key != SECRET:
            raise AssertionError("request used the wrong API key")
        lookup = (table, tuple(sorted(query.items())))
        if lookup not in self.responses:
            raise AssertionError(f"unexpected request {lookup}")
        return self.responses[lookup]


class FetchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "coders"
        self.dotenv = Path(tmp.name) / ".env"

    def _run(self, opener, cfg=None, environ=None, dotenv_path=None):
        with redirect_stdout(StringIO()):
            run(
                cfg or fetch_config(),
                self.data_dir,
                environ=environ if environ is not None else {API_KEY_ENV: SECRET},
                dotenv_path=self.dotenv if dotenv_path is None else dotenv_path,
                opener=opener,
            )

    def test_download_plan_names_files_by_table_province_and_year(self):
        params = FetchParams.from_config(
            fetch_config(
                provinces=["ON", "QC"],
                demand_years=[2023, 2024],
                capacity_factor_years=[2021],
            ).section("fetch")
        )
        names = [item.filename for item in downloads(params)]
        self.assertEqual(
            names,
            [
                "grid_cell_info.csv",
                "provincial_demand_ON_2023.csv",
                "provincial_demand_QC_2023.csv",
                "provincial_demand_ON_2024.csv",
                "provincial_demand_QC_2024.csv",
                "solar_capacity_factor_2021.csv",
                "wind_capacity_factor_2021.csv",
            ],
        )
        self.assertEqual(
            csv_path(self.data_dir, "provincial_demand", province="ON", year=2024),
            self.data_dir / "provincial_demand_ON_2024.csv",
        )

    def test_duplicate_config_values_rejected(self):
        with self.assertRaisesRegex(ConfigError, "fetch.demand_years has duplicates"):
            FetchParams.from_config(fetch_config(demand_years=[2024, 2024]).section("fetch"))

    def test_default_scenario_lists_all_provinces_and_years(self):
        params = FetchParams.from_config(
            Config.load(paths.SCENARIOS_DIR / "default.toml").section("fetch")
        )
        self.assertEqual(len(params.provinces), 10)
        self.assertEqual(params.demand_years, tuple(range(2018, 2025)))
        self.assertEqual(params.capacity_factor_years, (2021,))
        self.assertEqual(len(downloads(params)), 1 + 10 * 7 + 2)

    def test_json_tables_written_as_csv_and_existing_files_skipped(self):
        solar = capacity_factor_csv(HOURS_PER_YEAR)
        opener = RecordingOpener(responses(grid=[{**GRID_ROW, "extra": "keep-me"}], solar=solar))
        self._run(opener)
        grid_csv = (self.data_dir / "grid_cell_info.csv").read_text()
        self.assertIn("extra", grid_csv.splitlines()[0])
        self.assertIn("keep-me", grid_csv)
        self.assertEqual(
            (self.data_dir / "provincial_demand_ON_2024.csv").read_text().splitlines()[0],
            "local_time,province,daily_hour_ending,annual_hour_ending,demand_MWh",
        )
        self.assertEqual((self.data_dir / "solar_capacity_factor_2021.csv").read_bytes(), solar)

        opener.calls.clear()
        stdout = StringIO()
        with redirect_stdout(stdout):
            self._run(opener)
        self.assertEqual(opener.calls, [])

    def test_leap_year_demand_drops_february_29_and_renumbers_hours(self):
        self._run(RecordingOpener(responses(demand=demand_rows(2024, LEAP_YEAR_HOURS))))
        rows = read_rows(self.data_dir / "provincial_demand_ON_2024.csv")
        self.assertEqual(len(rows), HOURS_PER_YEAR)
        self.assertEqual([r["annual_hour_ending"] for r in rows], [str(h) for h in range(1, 8761)])
        self.assertFalse([r for r in rows if r["local_time"][5:10] == "02-29"])
        self.assertEqual(rows[1415]["local_time"], "2024-02-28 23:00:00")
        self.assertEqual(rows[1416]["local_time"], "2024-03-01 00:00:00")
        # demand_MWh is 100 + original zero-based hour, so it shows which source row was kept.
        self.assertEqual(rows[1416]["demand_MWh"], str(100 + 1440))
        self.assertEqual(rows[-1]["local_time"], "2024-12-31 23:00:00")

    def test_leap_year_capacity_factors_drop_hours_1417_to_1440(self):
        body = "h,1\n" + "".join(f"{h},{h / 10000}\n" for h in range(1, LEAP_YEAR_HOURS + 1))
        self._run(RecordingOpener(responses(wind=body.encode())))
        rows = read_rows(self.data_dir / "wind_capacity_factor_2021.csv")
        self.assertEqual(len(rows), HOURS_PER_YEAR)
        self.assertEqual([r["h"] for r in rows], [str(h) for h in range(1, 8761)])
        self.assertEqual(rows[1415]["1"], str(1416 / 10000))
        self.assertEqual(rows[1416]["1"], str(1441 / 10000))

    def test_unexpected_hour_count_fails_without_writing_file(self):
        with self.assertRaisesRegex(DataError, "expected 8760 or 8784 hours, got 8700"):
            self._run(RecordingOpener(responses(demand=demand_rows(2023, 8700))))
        self.assertFalse((self.data_dir / "provincial_demand_ON_2024.csv").exists())
        self.assertEqual([p.name for p in self.data_dir.glob("*.tmp*")], [])

    def test_leap_day_hours_must_be_february_29(self):
        rows = demand_rows(2024, LEAP_YEAR_HOURS)
        rows[1416]["local_time"] = "2024-03-01 00:00:00"
        with self.assertRaisesRegex(DataError, "hour 1417 should be February 29"):
            self._run(RecordingOpener(responses(demand=rows)))

    def test_leap_year_hours_must_be_sequential(self):
        rows = demand_rows(2024, LEAP_YEAR_HOURS)
        rows[10]["annual_hour_ending"] = 12
        with self.assertRaisesRegex(DataError, "expected hour 11"):
            self._run(RecordingOpener(responses(demand=rows)))

    def test_complete_cache_skips_network_and_does_not_need_api_key(self):
        self.data_dir.mkdir()
        for name in (
            "grid_cell_info.csv",
            "provincial_demand_ON_2024.csv",
            "solar_capacity_factor_2021.csv",
            "wind_capacity_factor_2021.csv",
        ):
            (self.data_dir / name).write_text("cached\n")

        def opener(request, timeout=None):
            raise AssertionError("should not fetch when files are already local")

        self._run(opener, environ={}, dotenv_path=self.dotenv)

    def test_empty_existing_file_is_redownloaded(self):
        self.data_dir.mkdir()
        (self.data_dir / "grid_cell_info.csv").write_text("")
        opener = RecordingOpener(responses())
        self._run(opener)
        self.assertGreater((self.data_dir / "grid_cell_info.csv").stat().st_size, 0)
        self.assertIn("grid_cell_info", [table for table, _ in opener.calls])

    def test_http_error_does_not_leave_partial_file_or_leak_key(self):
        def opener(request, timeout=None):
            raise HTTPError(
                f"https://api.sesit.ca/grid_cell_info?key={SECRET}",
                404,
                "Not Found",
                EmailMessage(),
                BytesIO(b'{"message": "No results found"}'),
            )

        with self.assertRaises(DataError) as caught:
            self._run(opener)
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertIn("HTTP 404", message)
        self.assertIn("No results found", message)
        self.assertFalse((self.data_dir / "grid_cell_info.csv").exists())
        self.assertFalse((self.data_dir / "grid_cell_info.csv.tmp").exists())

    def test_rejected_api_key_fails_immediately(self):
        def opener(request, timeout=None):
            raise HTTPError(
                f"https://api.sesit.ca/grid_cell_info?key={SECRET}",
                401,
                "Unauthorized",
                EmailMessage(),
                BytesIO(b""),
            )

        with self.assertRaisesRegex(DataError, "rejected the API key"):
            self._run(opener)

    def test_missing_api_key_mentions_env_var(self):
        self.dotenv.write_text("# no key here\n")
        with self.assertRaisesRegex(DataError, API_KEY_ENV):
            self._run(lambda *a, **k: None, environ={}, dotenv_path=self.dotenv)

    def test_dotenv_used_when_env_is_empty(self):
        self.dotenv.write_text(f'{API_KEY_ENV}="{SECRET}"\n')
        self._run(RecordingOpener(responses()), environ={}, dotenv_path=self.dotenv)
        self.assertTrue((self.data_dir / "grid_cell_info.csv").is_file())

    def test_env_overrides_dotenv(self):
        self.dotenv.write_text(f"{API_KEY_ENV}=from-file\n")
        self.assertEqual(_read_env_file(self.dotenv)[API_KEY_ENV], "from-file")
        # RecordingOpener fails unless the request carries SECRET from the environment.
        self._run(RecordingOpener(responses()), environ={API_KEY_ENV: SECRET})

    def test_missing_required_columns_fail(self):
        opener = RecordingOpener(responses(grid=[{"grid_cell": 1}]))
        with self.assertRaisesRegex(DataError, "missing required columns"):
            self._run(opener)

    def test_read_env_file_ignores_comments_and_export(self):
        path = self.data_dir.parent / "sample.env"
        path.write_text(
            "\n".join(
                [
                    "# comment",
                    "export OTHER=1",
                    f"{API_KEY_ENV}='abc'",
                    "not-a-pair",
                ]
            )
            + "\n"
        )
        self.assertEqual(_read_env_file(path), {"OTHER": "1", API_KEY_ENV: "abc"})


if __name__ == "__main__":
    unittest.main()
