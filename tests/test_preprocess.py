import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flux_match.errors import DataError
from flux_match.io import read_csv
from flux_match.run_context import create_run
from flux_match.schema import VRE_HOURLY_COLUMNS
from flux_match.selection.base import GenerationType, Site, write_sites
from flux_match.steps.preprocess import (
    LocatedSite,
    hourly_generation_by_ba,
    locate_sites,
    run,
    write_vre_hourly,
)

SOLAR = GenerationType.SOLAR
WIND_ONS = GenerationType.WIND_ONSHORE
WIND_OFS = GenerationType.WIND_OFFSHORE
HOURS = 3

GRID_HEADER = "grid_cell,balancing_area\n"


def hourly_csv(columns: dict[str, list[float]]) -> str:
    hours = len(next(iter(columns.values())))
    header = "h," + ",".join(columns)
    rows = [f"{h + 1}," + ",".join(str(col[h]) for col in columns.values()) for h in range(hours)]
    return "\n".join([header, *rows]) + "\n"


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class LocateSitesTest(unittest.TestCase):
    def test_splits_wind_and_solar_and_tags_balancing_area(self) -> None:
        sites = [
            Site(1, WIND_ONS, 100.0),
            Site(2, SOLAR, 50.0),
            Site(1, WIND_OFS, 25.0),
        ]
        wind, solar = locate_sites(
            sites, {"1": "East.a", "2": "West.a"}, Path("selected_sites.csv")
        )
        self.assertEqual(
            wind,
            [LocatedSite(1, "East.a", 100.0), LocatedSite(1, "East.a", 25.0)],
        )
        self.assertEqual(solar, [LocatedSite(2, "West.a", 50.0)])

    def test_missing_grid_cells_fail(self) -> None:
        sites = [Site(1, SOLAR, 1.0), Site(9, WIND_ONS, 1.0), Site(8, SOLAR, 1.0)]
        with self.assertRaisesRegex(DataError, r"selected_sites\.csv:.*\[8, 9\]"):
            locate_sites(sites, {"1": "East.a"}, Path("selected_sites.csv"))


class HourlyGenerationTest(TempDirTest):
    def test_multiplies_nameplate_by_factor_and_sums_by_balancing_area(self) -> None:
        path = self.write(
            "wind.csv",
            hourly_csv({"1": [0.5, 0.0, 1.0], "2": [0.2, 0.4, 0.0], "99": [1.0, 1.0, 1.0]}),
        )
        sites = [
            LocatedSite(1, "East.a", 100.0),
            LocatedSite(2, "East.a", 50.0),
            LocatedSite(1, "West.a", 10.0),
        ]
        got = hourly_generation_by_ba(path, sites, hours_in_year=HOURS)
        self.assertEqual(got["East.a"], [60.0, 20.0, 100.0])
        self.assertEqual(got["West.a"], [5.0, 0.0, 10.0])

    def test_empty_sites_does_not_need_the_file(self) -> None:
        self.assertEqual(
            hourly_generation_by_ba(self.dir / "missing.csv", [], hours_in_year=HOURS), {}
        )

    def test_missing_capacity_factor_column_fails(self) -> None:
        path = self.write("wind.csv", hourly_csv({"1": [0.1, 0.1, 0.1]}))
        with self.assertRaisesRegex(DataError, r"missing grid cell columns \[2\]"):
            hourly_generation_by_ba(path, [LocatedSite(2, "East.a", 1.0)], hours_in_year=HOURS)


class WriteVreHourlyTest(TempDirTest):
    def _inputs(
        self,
        sites: list[Site],
        wind: dict[str, list[float]],
        solar: dict[str, list[float]],
        grid: str = GRID_HEADER + "1,East.a\n2,West.a\n3,East.a\n",
    ) -> tuple[Path, Path, Path, Path, Path]:
        sites_path = self.dir / "selected_sites.csv"
        write_sites(sites_path, sites)
        return (
            sites_path,
            self.write("grid_cell_info.csv", grid),
            self.write("wind.csv", hourly_csv(wind)),
            self.write("solar.csv", hourly_csv(solar)),
            self.dir / "vre_by_ba_hourly.csv",
        )

    def _write(self, *args: Path) -> list[dict[str, str]]:
        write_vre_hourly(*args, hours_in_year=HOURS)
        rows = read_csv(args[-1], VRE_HOURLY_COLUMNS)
        self.assertEqual(tuple(rows[0]), VRE_HOURLY_COLUMNS)
        return rows

    def test_combines_wind_and_solar_and_includes_areas_with_no_sites(self) -> None:
        rows = self._write(
            *self._inputs(
                [Site(1, WIND_ONS, 100.0), Site(1, SOLAR, 40.0), Site(3, WIND_OFS, 20.0)],
                {"1": [0.5, 0.0, 0.25], "3": [0.5, 1.0, 0.0]},
                {"1": [0.25, 0.5, 0.0], "2": [1.0, 1.0, 1.0]},
            )
        )
        self.assertEqual(len(rows), 6)
        by_key = {(r["balancing_area"], r["hour"]): r for r in rows}
        east1 = by_key[("East.a", "1")]
        self.assertEqual(float(east1["wind_mw"]), 60.0)  # 100*0.5 + 20*0.5
        self.assertEqual(float(east1["solar_mw"]), 10.0)  # 40*0.25
        self.assertEqual(float(east1["variable_generation_mw"]), 70.0)
        east2 = by_key[("East.a", "2")]
        self.assertEqual(float(east2["wind_mw"]), 20.0)
        self.assertEqual(float(east2["solar_mw"]), 20.0)
        west = by_key[("West.a", "1")]
        self.assertEqual(float(west["wind_mw"]), 0.0)
        self.assertEqual(float(west["solar_mw"]), 0.0)
        self.assertEqual(float(west["variable_generation_mw"]), 0.0)
        self.assertEqual([r["balancing_area"] for r in rows], ["East.a"] * 3 + ["West.a"] * 3)
        self.assertEqual([r["hour"] for r in rows[:3]], ["1", "2", "3"])

    def test_empty_selection_writes_zeros_for_every_area(self) -> None:
        rows = self._write(
            *self._inputs(
                [],
                {"1": [0.1, 0.1, 0.1]},
                {"1": [0.2, 0.2, 0.2]},
            )
        )
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(float(r["variable_generation_mw"]) == 0.0 for r in rows))

    def test_output_rounded_to_whole_mw_and_total_is_sum_of_rounded_values(self) -> None:
        rows = self._write(
            *self._inputs(
                [Site(1, WIND_ONS, 566.9952054794521), Site(1, SOLAR, 10.0)],
                {"1": [1.0, 0.0, 0.0], "3": [0.0, 0.0, 0.0]},
                {"1": [0.04, 0.0, 0.0]},
            )
        )
        self.assertEqual(rows[0]["wind_mw"], "567")
        self.assertEqual(rows[0]["solar_mw"], "0")
        self.assertEqual(rows[0]["variable_generation_mw"], "567")

    def test_selected_cell_missing_from_grid_info_fails_without_output(self) -> None:
        args = self._inputs(
            [Site(9, SOLAR, 1.0)],
            {"1": [0.1, 0.1, 0.1]},
            {"1": [0.1, 0.1, 0.1]},
        )
        with self.assertRaisesRegex(DataError, r"selected grid cells not in grid_cell_info: \[9\]"):
            write_vre_hourly(*args, hours_in_year=HOURS)
        self.assertFalse(args[-1].exists())
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])

    def test_bad_capacity_factor_fails_without_output(self) -> None:
        args = self._inputs(
            [Site(1, WIND_ONS, 1.0)],
            {"1": [0.1, 1.5, 0.1]},
            {"1": [0.1, 0.1, 0.1]},
        )
        with self.assertRaisesRegex(DataError, r"hour 2, grid cell 1: capacity factor '1.5'"):
            write_vre_hourly(*args, hours_in_year=HOURS)
        self.assertFalse(args[-1].exists())


class PreprocessRunTest(TempDirTest):
    def test_run_uses_configured_capacity_factor_year(self) -> None:
        data_dir = self.dir / "coders"
        data_dir.mkdir()
        (data_dir / "grid_cell_info.csv").write_text(GRID_HEADER + "1,East.a\n")
        (data_dir / "wind_capacity_factor_2021.csv").write_text(hourly_csv({"1": [0.5, 0.0, 0.0]}))
        (data_dir / "solar_capacity_factor_2021.csv").write_text(
            hourly_csv({"1": [0.25, 0.0, 0.0]})
        )
        scenario = self.write(
            "default.toml",
            "\n".join(
                [
                    "[fetch]",
                    'provinces = ["ON"]',
                    "demand_years = [2024]",
                    "capacity_factor_years = [2021]",
                ]
            )
            + "\n",
        )
        ctx = create_run(scenario, self.dir / "output", datetime(2026, 10, 27, 19, 51))
        write_sites(ctx.selected_sites_path, [Site(1, WIND_ONS, 10.0), Site(1, SOLAR, 8.0)])
        run(ctx, data_dir=data_dir, hours_in_year=HOURS)
        rows = read_csv(ctx.vre_hourly_path, VRE_HOURLY_COLUMNS)
        self.assertEqual(len(rows), HOURS)
        self.assertEqual(float(rows[0]["wind_mw"]), 5.0)
        self.assertEqual(float(rows[0]["solar_mw"]), 2.0)
        self.assertEqual(float(rows[0]["variable_generation_mw"]), 7.0)


if __name__ == "__main__":
    unittest.main()
