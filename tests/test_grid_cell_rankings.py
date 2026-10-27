import csv
import tempfile
import unittest
from pathlib import Path

from flux_match.errors import DataError
from flux_match.grid_cell_rankings import (
    RANKINGS_COLUMNS,
    CapacitySummary,
    build_rankings,
    rank_within_groups,
    read_grid_cells,
    summarize_capacity_factors,
    write_grid_cell_rankings,
)
from flux_match.schema import HOURS_PER_YEAR

GRID_HEADER = "grid_cell,longitude,latitude,balancing_area,province\n"


def hourly_csv(columns: dict[str, list[float]], hours: int = HOURS_PER_YEAR) -> str:
    """Capacity-factor table text; each column is a function of 0-based hour via list index."""
    header = "h," + ",".join(columns)
    rows = [f"{h + 1}," + ",".join(str(col[h]) for col in columns.values()) for h in range(hours)]
    return "\n".join([header, *rows]) + "\n"


def day_pattern(daily_values: list[float]) -> list[float]:
    """Hourly values giving the listed day sums: each day has 24 equal hours."""
    return [v / 24 for v in daily_values for _ in range(24)]


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class RankWithinGroupsTest(unittest.TestCase):
    def test_highest_value_is_rank_one_per_group(self):
        ranks = rank_within_groups([1.0, 5.0, 3.0, 2.0, 9.0], ["a", "a", "b", "a", "b"])
        self.assertEqual(ranks, [3, 1, 2, 2, 1])

    def test_ties_broken_by_position_so_ranks_are_unique(self):
        ranks = rank_within_groups([0.0, 0.0, 0.0, 1.0], ["a", "a", "a", "a"])
        self.assertEqual(ranks, [2, 3, 4, 1])


class SummarizeTest(TempDirTest):
    def test_annual_sum_and_median_of_daily_sums(self):
        # Cell 1: day sums rise evenly from 0 to 24, so the median is 12 and the annual sum is
        # 365 * 12. A day boundary misaligned by even one hour would shift the median.
        # Cell 2 is constant at 0.25 per hour.
        path = self.write(
            "wind.csv",
            hourly_csv(
                {
                    "1": day_pattern([24 * d / 364 for d in range(365)]),
                    "2": [0.25] * HOURS_PER_YEAR,
                }
            ),
        )
        summary = summarize_capacity_factors(path)
        self.assertEqual(summary.cells, ("1", "2"))
        self.assertAlmostEqual(summary.annual[0], 4380.0)
        self.assertAlmostEqual(summary.annual[1], 2190.0)
        self.assertAlmostEqual(summary.median_daily[0], 12.0)
        self.assertAlmostEqual(summary.median_daily[1], 6.0)

    def test_median_differs_from_mean_for_skewed_days(self):
        # One huge day among otherwise empty days: annual sum is large, median is zero.
        days = [0.0] * 365
        days[100] = 24.0
        path = self.write("solar.csv", hourly_csv({"7": day_pattern(days)}))
        summary = summarize_capacity_factors(path)
        self.assertAlmostEqual(summary.annual[0], 24.0)
        self.assertEqual(summary.median_daily[0], 0.0)

    def test_wrong_hour_count_fails(self):
        path = self.write("w.csv", hourly_csv({"1": [0.1] * 8700}, hours=8700))
        with self.assertRaisesRegex(DataError, "expected 8760 hourly rows, got 8700"):
            summarize_capacity_factors(path)

    def test_too_many_hours_fails(self):
        path = self.write("w.csv", hourly_csv({"1": [0.1] * 8761}, hours=8761))
        with self.assertRaisesRegex(DataError, "more than 8760 hourly rows"):
            summarize_capacity_factors(path)

    def test_out_of_order_hour_fails(self):
        text = hourly_csv({"1": [0.1] * HOURS_PER_YEAR}).replace("\n5,", "\n6,", 1)
        with self.assertRaisesRegex(DataError, "expected hour 5 on row 5, got '6'"):
            summarize_capacity_factors(self.write("w.csv", text))

    def test_bad_values_fail_and_name_hour_and_cell(self):
        for bad, message in [
            ("abc", r"hour 3, grid cell 2: not a number: 'abc'"),
            ("", r"hour 3, grid cell 2: not a number: ''"),
            ("nan", r"hour 3, grid cell 2: capacity factor 'nan' is outside 0-1"),
            ("-0.1", r"hour 3, grid cell 2: capacity factor '-0.1' is outside 0-1"),
            ("1.5", r"hour 3, grid cell 2: capacity factor '1.5' is outside 0-1"),
        ]:
            with self.subTest(bad=bad):
                column = [0.1] * HOURS_PER_YEAR
                text = hourly_csv({"1": column, "2": column}).splitlines()
                text[3] = "3,0.1," + bad
                with self.assertRaisesRegex(DataError, message):
                    summarize_capacity_factors(self.write("w.csv", "\n".join(text) + "\n"))

    def test_short_row_fails(self):
        text = hourly_csv({"1": [0.1] * HOURS_PER_YEAR, "2": [0.1] * HOURS_PER_YEAR}).splitlines()
        text[10] = "10,0.1"
        with self.assertRaisesRegex(DataError, "hour 10 has 2 fields, expected 3"):
            summarize_capacity_factors(self.write("w.csv", "\n".join(text) + "\n"))

    def test_duplicate_cell_columns_fail(self):
        text = "h,1,1\n" + "".join(f"{h},0.1,0.1\n" for h in range(1, HOURS_PER_YEAR + 1))
        with self.assertRaisesRegex(DataError, r"duplicate grid cell columns \['1'\]"):
            summarize_capacity_factors(self.write("w.csv", text))

    def test_missing_hour_column_fails(self):
        with self.assertRaisesRegex(DataError, "first column must be 'h'"):
            summarize_capacity_factors(self.write("w.csv", "hour,1\n1,0.1\n"))


class ReadGridCellsTest(TempDirTest):
    def test_returns_cells_and_areas_in_file_order(self):
        path = self.write("g.csv", GRID_HEADER + "5,0,0,A.a,A\n2,0,0,B.a,B\n")
        self.assertEqual(read_grid_cells(path), (["5", "2"], ["A.a", "B.a"]))

    def test_duplicate_cell_fails(self):
        path = self.write("g.csv", GRID_HEADER + "1,0,0,A.a,A\n1,0,0,A.a,A\n")
        with self.assertRaisesRegex(DataError, r"duplicate grid cells \[1\]"):
            read_grid_cells(path)

    def test_blank_balancing_area_fails(self):
        path = self.write("g.csv", GRID_HEADER + "1,0,0,,A\n")
        with self.assertRaisesRegex(DataError, r"blank balancing_area for grid cells \[1\]"):
            read_grid_cells(path)


class BuildRankingsTest(unittest.TestCase):
    def summary(self, cells, annual, median):
        return CapacitySummary(tuple(cells), tuple(annual), tuple(median))

    def test_ranks_are_per_balancing_area_and_use_the_matching_metric(self):
        # Solar and wind give opposite orders, and the annual and median metrics disagree.
        cells = ["1", "2", "3", "4"]
        areas = ["A", "A", "A", "B"]
        solar = self.summary(cells, [10, 30, 20, 5], [3, 1, 2, 7])
        wind = self.summary(cells, [3, 2, 1, 9], [1, 2, 3, 4])
        rows = build_rankings(cells, areas, solar, wind)
        by_cell = {r["grid_cell"]: r for r in rows}
        self.assertEqual([r["ba_annual_solar_rank"] for r in rows], [3, 1, 2, 1])
        self.assertEqual([r["ba_annual_wind_rank"] for r in rows], [1, 2, 3, 1])
        self.assertEqual([r["ba_median_solar_rank"] for r in rows], [1, 3, 2, 1])
        self.assertEqual([r["ba_median_wind_rank"] for r in rows], [3, 2, 1, 1])
        self.assertEqual(by_cell["2"]["annual_solar_capacity_factor"], 30)
        self.assertEqual(by_cell["2"]["annual_wind_capacity_factor"], 2)
        self.assertEqual(by_cell["2"]["ba_median_daily_solar_capacity"], 1)
        self.assertEqual(by_cell["2"]["ba_median_daily_wind_capacity"], 2)
        self.assertEqual(tuple(rows[0]), RANKINGS_COLUMNS)

    def test_capacity_tables_in_different_column_order_are_aligned_by_cell(self):
        cells = ["1", "2"]
        solar = self.summary(["2", "1"], [20, 10], [2, 1])
        wind = self.summary(["1", "2"], [1, 2], [1, 2])
        rows = build_rankings(cells, ["A", "A"], solar, wind)
        self.assertEqual([r["annual_solar_capacity_factor"] for r in rows], [10, 20])
        self.assertEqual([r["ba_annual_solar_rank"] for r in rows], [2, 1])

    def test_cell_mismatch_fails(self):
        ok = self.summary(["1", "2"], [1, 2], [1, 2])
        short = self.summary(["1"], [1], [1])
        with self.assertRaisesRegex(DataError, r"solar_capacity_factor .* missing \[2\]"):
            build_rankings(["1", "2"], ["A", "A"], short, ok)
        extra = self.summary(["1", "2", "3"], [1, 2, 3], [1, 2, 3])
        with self.assertRaisesRegex(DataError, r"wind_capacity_factor .* extra \[3\]"):
            build_rankings(["1", "2"], ["A", "A"], ok, extra)


class WriteGridCellRankingsTest(TempDirTest):
    def test_writes_csv_with_one_row_per_grid_cell(self):
        grid = self.write("g.csv", GRID_HEADER + "1,0,0,A.a,A\n2,0,0,A.a,A\n3,0,0,B.a,B\n")
        solar = self.write(
            "solar.csv",
            hourly_csv({"1": [0.1] * 8760, "2": [0.3] * 8760, "3": [0.0] * 8760}),
        )
        wind = self.write(
            "wind.csv",
            hourly_csv({"1": [0.5] * 8760, "2": [0.2] * 8760, "3": [0.0] * 8760}),
        )
        dest = self.dir / "grid_cell_rankings.csv"
        write_grid_cell_rankings(grid, solar, wind, dest)
        with dest.open(newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(tuple(reader.fieldnames or ()), RANKINGS_COLUMNS)
            rows = list(reader)
        self.assertEqual([r["grid_cell"] for r in rows], ["1", "2", "3"])
        self.assertEqual([r["ba_annual_solar_rank"] for r in rows], ["2", "1", "1"])
        self.assertEqual([r["ba_annual_wind_rank"] for r in rows], ["1", "2", "1"])
        self.assertAlmostEqual(float(rows[0]["annual_wind_capacity_factor"]), 4380.0)
        self.assertAlmostEqual(float(rows[1]["ba_median_daily_solar_capacity"]), 7.2)

    def test_failure_leaves_no_output_file(self):
        grid = self.write("g.csv", GRID_HEADER + "1,0,0,A.a,A\n")
        solar = self.write("solar.csv", hourly_csv({"1": [0.1] * 8760}))
        wind = self.write("wind.csv", hourly_csv({"2": [0.1] * 8760}))
        dest = self.dir / "grid_cell_rankings.csv"
        with self.assertRaisesRegex(DataError, "wind_capacity_factor grid cells differ"):
            write_grid_cell_rankings(grid, solar, wind, dest)
        self.assertEqual([p.name for p in self.dir.glob("grid_cell_rankings*")], [])


if __name__ == "__main__":
    unittest.main()
