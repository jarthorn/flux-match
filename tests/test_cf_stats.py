import csv
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flux_match.errors import DataError
from flux_match.viz.cf_stats import (
    capacity_factor_path,
    cells_by_balancing_area,
    output_path,
    population_std,
    require_matching_cells,
    write_ba_hourly_std,
)

GRID_HEADER = "grid_cell,longitude,latitude,balancing_area,province\n"


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class PopulationStdTest(unittest.TestCase):
    def test_known_values(self):
        # Mean 2; squared deviations 1+0+1; /3 = 2/3; sqrt = sqrt(2/3).
        self.assertAlmostEqual(population_std([1.0, 2.0, 3.0]), math.sqrt(2 / 3))

    def test_single_value_is_zero(self):
        self.assertEqual(population_std([0.42]), 0.0)

    def test_empty_fails(self):
        with self.assertRaisesRegex(DataError, "empty cell set"):
            population_std([])


class CellsByBalancingAreaTest(unittest.TestCase):
    def test_groups_indices_and_sorts_ba_names(self):
        by_ba = cells_by_balancing_area(["1", "2", "3"], ["Z.a", "A.a", "Z.a"])
        self.assertEqual(list(by_ba), ["A.a", "Z.a"])
        self.assertEqual(by_ba["A.a"], [1])
        self.assertEqual(by_ba["Z.a"], [0, 2])


class RequireMatchingCellsTest(TempDirTest):
    def test_matching_cells_ok(self):
        cf = self.write("cf.csv", "h,1,2\n1,0.1,0.2\n")
        require_matching_cells(cf, ["1", "2"])

    def test_missing_and_extra_fail_with_details(self):
        cf = self.write("cf.csv", "h,1,3\n1,0.1,0.2\n")
        with self.assertRaisesRegex(
            DataError,
            r"cf\.csv grid cells differ from grid_cell_info: missing \[2\].*extra \[3\]",
        ):
            require_matching_cells(cf, ["1", "2"])


class WriteBaHourlyStdTest(TempDirTest):
    def test_hourly_std_by_balancing_area(self):
        # Hour 1: A={0.0, 1.0} -> std sqrt(0.25)=0.5; B={0.5} -> 0;
        #         all={0.0, 0.5, 1.0} -> sqrt(2/3)*0.5 = sqrt(1/6).
        # Hour 2: A={0.2, 0.4} -> 0.1; B={0.9} -> 0;
        #         all={0.2, 0.9, 0.4} -> mean 0.5; sqrt((0.09+0.16+0.01)/3)=sqrt(0.26/3).
        grid = self.write(
            "grid.csv",
            GRID_HEADER + "1,0,0,A.a,A\n2,0,0,B.a,B\n3,0,0,A.a,A\n",
        )
        cf = self.write("cf.csv", "h,1,2,3\n1,0.0,0.5,1.0\n2,0.2,0.9,0.4\n")
        dest = self.dir / "out.csv"
        write_ba_hourly_std(cf, grid, dest, hours_in_year=2)

        with dest.open(newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["h", "A.a", "B.a", "all_cells"])
        self.assertEqual(rows[1][0], "1")
        self.assertAlmostEqual(float(rows[1][1]), 0.5)
        self.assertAlmostEqual(float(rows[1][2]), 0.0)
        self.assertAlmostEqual(float(rows[1][3]), math.sqrt(1 / 6))
        self.assertEqual(rows[2][0], "2")
        self.assertAlmostEqual(float(rows[2][1]), 0.1)
        self.assertAlmostEqual(float(rows[2][2]), 0.0)
        self.assertAlmostEqual(float(rows[2][3]), math.sqrt(0.26 / 3))

    def test_cell_mismatch_fails_before_writing(self):
        grid = self.write("grid.csv", GRID_HEADER + "1,0,0,A.a,A\n2,0,0,A.a,A\n")
        cf = self.write("cf.csv", "h,1\n1,0.1\n")
        dest = self.dir / "out.csv"
        with self.assertRaisesRegex(DataError, "differ from grid_cell_info"):
            write_ba_hourly_std(cf, grid, dest, hours_in_year=1)
        self.assertFalse(dest.exists())


class PathHelpersTest(unittest.TestCase):
    def test_output_path_includes_type_and_timestamp(self):
        path = output_path("solar", datetime(2026, 11, 13, 14, 59))
        self.assertEqual(path.name, "cf-stats-solar-20261113-1459.csv")

    def test_capacity_factor_path_solar_always_coders(self):
        path = capacity_factor_path("solar", 2021, wind_source="ninja")
        self.assertEqual(path.name, "solar_capacity_factor_2021.csv")
        self.assertEqual(path.parent.name, "coders")

    def test_capacity_factor_path_wind_ninja(self):
        path = capacity_factor_path("wind", 2021, wind_source="ninja")
        self.assertEqual(path.name, "wind_capacity_factor_2021.csv")
        self.assertEqual(path.parent.name, "ninja")


if __name__ == "__main__":
    unittest.main()
