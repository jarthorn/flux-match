import math
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from test_fetch import GRID_ROW, capacity_factor_csv, fetch_config, read_rows

from flux_match import paths
from flux_match.errors import DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.fetch.fetch import FetchParams
from flux_match.steps.preselect.preselect import (
    normalize_hourly_demand,
    run,
    write_normalized_demand,
)


class PreselectTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(paths, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data_dir = paths.CODERS_DATA_DIR
        self.rankings = paths.grid_cell_rankings_path("coders")
        self.ninja_rankings = paths.grid_cell_rankings_path("ninja")

    def _write_coders(self, wind: str = "0.3", solar: str = "0.2") -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "grid_cell_info.csv").write_text(
            ",".join(GRID_ROW) + "\n" + ",".join(str(v) for v in GRID_ROW.values()) + "\n"
        )
        (self.data_dir / "solar_capacity_factor_2021.csv").write_bytes(
            capacity_factor_csv(HOURS_PER_YEAR, solar)
        )
        (self.data_dir / "wind_capacity_factor_2021.csv").write_bytes(
            capacity_factor_csv(HOURS_PER_YEAR, wind)
        )

    def _inputs(self) -> tuple[Path, ...]:
        return (
            self.data_dir / "grid_cell_info.csv",
            self.data_dir / "solar_capacity_factor_2021.csv",
            self.data_dir / "wind_capacity_factor_2021.csv",
        )

    def _write_demand(self, year: int, series: dict[str, list[float]]) -> Path:
        path = paths.ba_demand_hourly_path(year)
        lines = ["balancing_area,hour,demand_mw"]
        for ba in sorted(series):
            for hour, mw in enumerate(series[ba], start=1):
                lines.append(f"{ba},{hour},{mw}")
        path.write_text("\n".join(lines) + "\n")
        return path

    def _write_flat_demand(self, cfg) -> None:
        series = {"Ontario.a": [1.0] * HOURS_PER_YEAR}
        for year in FetchParams.from_config(cfg.section("fetch")).demand_years:
            self._write_demand(year, series)

    def _run(self, cfg=None) -> None:
        cfg = cfg or fetch_config()
        self._write_flat_demand(cfg)
        with redirect_stdout(StringIO()):
            run(cfg)

    def test_rankings_from_capacity_factor_tables(self) -> None:
        self._write_coders()
        self._run()
        rows = read_rows(self.rankings)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grid_cell"], "1")
        self.assertAlmostEqual(float(rows[0]["annual_solar_capacity_factor"]), 0.2 * 8760)
        self.assertAlmostEqual(float(rows[0]["annual_wind_capacity_factor"]), 0.3 * 8760)
        self.assertEqual(rows[0]["ba_annual_solar_rank"], "1")
        self.assertFalse(self.ninja_rankings.exists())

    def test_rankings_rebuilt_only_when_older_than_an_input(self) -> None:
        self._write_coders()
        self._run()
        self.rankings.write_text("sentinel\n")
        newest_input = max(p.stat().st_mtime for p in self._inputs())

        os.utime(self.rankings, (newest_input + 10, newest_input + 10))
        self._run()
        self.assertEqual(self.rankings.read_text(), "sentinel\n")

        os.utime(self.rankings, (newest_input - 10, newest_input - 10))
        self._run()
        self.assertEqual(read_rows(self.rankings)[0]["grid_cell"], "1")

    def test_ninja_wind_source_uses_the_ninja_table_and_coders_solar(self) -> None:
        self._write_coders()
        ninja = paths.NINJA_DATA_DIR
        ninja.mkdir()
        (ninja / "wind_capacity_factor_2021.csv").write_bytes(
            capacity_factor_csv(HOURS_PER_YEAR, "0.4")
        )
        self._run(fetch_config(wind_source="ninja"))
        rows = read_rows(self.ninja_rankings)
        self.assertAlmostEqual(float(rows[0]["annual_wind_capacity_factor"]), 0.4 * 8760)
        self.assertAlmostEqual(float(rows[0]["annual_solar_capacity_factor"]), 0.2 * 8760)
        self.assertFalse(self.rankings.exists())

    def test_missing_wind_table_fails_without_rankings(self) -> None:
        self._write_coders()
        (self.data_dir / "wind_capacity_factor_2021.csv").unlink()
        with self.assertRaisesRegex(DataError, r"wind_capacity_factor_2021\.csv not found"):
            self._run()
        self.assertFalse(self.rankings.exists())

    def test_each_year_sums_to_8760_and_the_file_averages_normalized_curves(self) -> None:
        # Hour 1 differs from the rest, and the two years are not the same shape, so the
        # mean of the normalized curves is not the normalization of the mean megawatts.
        east_2023 = _shape(1.0, 3.0)
        east_2024 = [5.0] * HOURS_PER_YEAR
        west_2023 = _shape(10.0, 0.0)
        west_2024 = _shape(2.0, 4.0)
        self._write_demand(2023, {"East.a": east_2023, "West.a": west_2023})
        self._write_demand(2024, {"West.a": west_2024, "East.a": east_2024})

        for series in (east_2023, east_2024, west_2023, west_2024):
            normalized = normalize_hourly_demand(series, where="year")
            self.assertAlmostEqual(math.fsum(normalized), HOURS_PER_YEAR)
            mean = sum(series) / HOURS_PER_YEAR
            self.assertAlmostEqual(normalized[0], series[0] / mean)
            self.assertAlmostEqual(normalized[-1], series[-1] / mean)

        expected = {
            "East.a": _mean_curves(
                normalize_hourly_demand(east_2023, where="East.a"),
                normalize_hourly_demand(east_2024, where="East.a"),
            ),
            "West.a": _mean_curves(
                normalize_hourly_demand(west_2023, where="West.a"),
                normalize_hourly_demand(west_2024, where="West.a"),
            ),
        }
        averaged_mw = _mean_curves(east_2023, east_2024)
        averaged_then_normalized = normalize_hourly_demand(averaged_mw, where="averaged")
        self.assertNotAlmostEqual(expected["East.a"][0], averaged_then_normalized[0])

        with redirect_stdout(StringIO()):
            write_normalized_demand(fetch_config(demand_years=[2023, 2024]))
        rows = read_rows(paths.normalized_demand_hourly_path())
        self.assertEqual(list(rows[0]), ["balancing_area", "hour", "normalized_demand"])
        self.assertEqual(len(rows), 2 * HOURS_PER_YEAR)
        self.assertEqual(rows[0]["balancing_area"], "East.a")
        self.assertEqual(rows[0]["hour"], "1")
        self.assertEqual(rows[HOURS_PER_YEAR - 1]["hour"], str(HOURS_PER_YEAR))
        self.assertEqual(rows[HOURS_PER_YEAR]["balancing_area"], "West.a")
        self.assertEqual(rows[HOURS_PER_YEAR]["hour"], "1")
        for ba, start in (("East.a", 0), ("West.a", HOURS_PER_YEAR)):
            block = rows[start : start + HOURS_PER_YEAR]
            values = [float(row["normalized_demand"]) for row in block]
            self.assertTrue(all(row["balancing_area"] == ba for row in block))
            self.assertEqual(
                [int(row["hour"]) for row in block], list(range(1, HOURS_PER_YEAR + 1))
            )
            for got, want in zip(values, expected[ba], strict=True):
                self.assertAlmostEqual(got, want)
            self.assertAlmostEqual(math.fsum(values), HOURS_PER_YEAR)

    def test_normalized_demand_rebuilt_only_when_older_than_an_input(self) -> None:
        source = self._write_demand(2024, {"East.a": [1.0] * HOURS_PER_YEAR})
        cfg = fetch_config()
        with redirect_stdout(StringIO()):
            write_normalized_demand(cfg)
        dest = paths.normalized_demand_hourly_path()
        dest.write_text("sentinel\n")
        newest_input = source.stat().st_mtime

        os.utime(dest, (newest_input + 10, newest_input + 10))
        with redirect_stdout(StringIO()):
            write_normalized_demand(cfg)
        self.assertEqual(dest.read_text(), "sentinel\n")

        os.utime(dest, (newest_input - 10, newest_input - 10))
        with redirect_stdout(StringIO()):
            write_normalized_demand(cfg)
        self.assertEqual(read_rows(dest)[0]["normalized_demand"], "1.0")

    def test_missing_demand_year_fails_without_output(self) -> None:
        self._write_demand(2023, {"East.a": [1.0] * HOURS_PER_YEAR})
        dest = paths.normalized_demand_hourly_path()
        with self.assertRaisesRegex(DataError, r"balancing_area_demand_hourly_2024\.csv not found"):
            write_normalized_demand(fetch_config(demand_years=[2023, 2024]))
        self.assertFalse(dest.exists())

    def test_balancing_areas_must_match_across_years(self) -> None:
        full = [1.0] * HOURS_PER_YEAR
        self._write_demand(2023, {"East.a": full})
        self._write_demand(2024, {"East.a": full, "West.a": full})
        with self.assertRaisesRegex(DataError, "balancing areas differ"):
            write_normalized_demand(fetch_config(demand_years=[2023, 2024]))
        self.assertFalse(paths.normalized_demand_hourly_path().exists())

    def test_non_positive_average_demand_fails(self) -> None:
        self._write_demand(2024, {"East.a": [0.0] * HOURS_PER_YEAR})
        with self.assertRaisesRegex(DataError, "average hourly demand is not positive"):
            write_normalized_demand(fetch_config())

    def test_negative_demand_is_reported_and_still_sums_to_8760(self) -> None:
        series = [1.0] * HOURS_PER_YEAR
        series[1] = -1.0
        self._write_demand(2024, {"East.a": series})
        err = StringIO()
        with redirect_stderr(err), redirect_stdout(StringIO()):
            write_normalized_demand(fetch_config())
        self.assertIn("demand_mw is negative: -1.0", err.getvalue())
        rows = read_rows(paths.normalized_demand_hourly_path())
        self.assertLess(float(rows[1]["normalized_demand"]), 0)
        self.assertAlmostEqual(
            math.fsum(float(row["normalized_demand"]) for row in rows), HOURS_PER_YEAR
        )

    def test_incomplete_year_fails(self) -> None:
        self._write_demand(2024, {"East.a": [1.0, 2.0, 3.0]})
        with self.assertRaisesRegex(DataError, r"East\.a has 3 hours"):
            write_normalized_demand(fetch_config())

    def test_run_writes_normalized_demand(self) -> None:
        self._write_coders()
        self._run()
        rows = read_rows(paths.normalized_demand_hourly_path())
        self.assertEqual(len(rows), HOURS_PER_YEAR)
        self.assertTrue(all(row["normalized_demand"] == "1.0" for row in rows))
        self.assertAlmostEqual(
            math.fsum(float(row["normalized_demand"]) for row in rows), HOURS_PER_YEAR
        )


def _shape(first: float, rest: float) -> list[float]:
    return [first, *([rest] * (HOURS_PER_YEAR - 1))]


def _mean_curves(*curves: list[float]) -> list[float]:
    count = len(curves)
    return [sum(curve[hour] for curve in curves) / count for hour in range(len(curves[0]))]


if __name__ == "__main__":
    unittest.main()
