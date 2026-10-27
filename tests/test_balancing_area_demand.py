import csv
import math
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from flux_match.balancing_area_demand import (
    BA_COLUMN,
    PROVINCE_BA_POPULATION,
    PROVINCE_NAMES,
    build_ba_demand,
    find_demand_files,
    read_annual_demand,
    split_by_population,
    write_balancing_area_demand,
)
from flux_match.errors import DataError
from flux_match.schema import HOURS_PER_YEAR

DEMAND_HEADER = "local_time,province,daily_hour_ending,annual_hour_ending,demand_MWh\n"


def demand_csv(code: str, mwh_per_hour: object = 1, hours: int = HOURS_PER_YEAR) -> str:
    rows = (f"2024-01-01 00:00:00,{code},1,{h},{mwh_per_hour}\n" for h in range(1, hours + 1))
    return DEMAND_HEADER + "".join(rows)


def write_demand_files(
    data_dir: Path,
    years: list[int],
    mwh_per_hour: dict[str, float] | None = None,
    skip: tuple[str, ...] = (),
) -> None:
    """Complete provincial demand files for each year. Default is 1 MWh per hour everywhere."""
    data_dir.mkdir(exist_ok=True)
    for year in years:
        for code in PROVINCE_NAMES:
            if f"{code}_{year}" not in skip:
                value = (mwh_per_hour or {}).get(code, 1)
                (data_dir / f"provincial_demand_{code}_{year}.csv").write_text(
                    demand_csv(code, value)
                )


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data_dir = self.dir / "coders"


class PopulationTableTest(unittest.TestCase):
    def test_every_province_code_has_a_population_entry_and_vice_versa(self):
        self.assertEqual(set(PROVINCE_NAMES.values()), set(PROVINCE_BA_POPULATION))

    def test_every_province_has_positive_populations(self):
        for province, populations in PROVINCE_BA_POPULATION.items():
            with self.subTest(province=province):
                self.assertTrue(populations)
                self.assertTrue(all(p > 0 for p in populations.values()))


class SplitTest(unittest.TestCase):
    def test_proportional_to_population_and_conserves_total(self):
        shares = split_by_population(1000.0, {"a": 75, "b": 25})
        self.assertEqual(shares, {"a": 750.0, "b": 250.0})

    def test_single_ba_gets_everything(self):
        self.assertEqual(split_by_population(123.5, {"only": 7}), {"only": 123.5})


class BuildTest(unittest.TestCase):
    def totals(self, year: int, **overrides: float) -> dict[str, float]:
        return {code: overrides.get(code, 0.0) for code in PROVINCE_NAMES}

    def test_ba_spanning_two_provinces_sums_both_shares(self):
        # Ontario.a is mostly in Ontario but also serves a sliver of Quebec.
        on, qc = PROVINCE_BA_POPULATION["Ontario"], PROVINCE_BA_POPULATION["Quebec"]
        years, rows = build_ba_demand({2024: self.totals(2024, ON=1e6, QC=2e6)})
        by_ba = {r[BA_COLUMN]: r for r in rows}
        expected = 1e6 * on["Ontario.a"] / sum(on.values()) + 2e6 * qc["Ontario.a"] / sum(
            qc.values()
        )
        self.assertEqual(years, [2024])
        self.assertAlmostEqual(by_ba["Ontario.a"]["2024"], expected)
        self.assertAlmostEqual(by_ba["Ontario.b"]["2024"], 1e6 * on["Ontario.b"] / sum(on.values()))

    def test_total_demand_is_conserved_and_every_ba_has_a_row(self):
        totals = {2023: self.totals(2023, AB=5.0, NL=7.0, QC=11.0), 2024: self.totals(2024, ON=3.0)}
        years, rows = build_ba_demand(totals)
        self.assertEqual(years, [2023, 2024])
        all_bas = {ba for pops in PROVINCE_BA_POPULATION.values() for ba in pops}
        self.assertEqual({r[BA_COLUMN] for r in rows}, all_bas)
        self.assertAlmostEqual(math.fsum(r["2023"] for r in rows), 23.0)
        self.assertAlmostEqual(math.fsum(r["2024"] for r in rows), 3.0)

    def test_rows_sorted_by_balancing_area(self):
        _, rows = build_ba_demand({2024: self.totals(2024)})
        names = [r[BA_COLUMN] for r in rows]
        self.assertEqual(names, sorted(names))


class FindDemandFilesTest(TempDirTest):
    def test_finds_every_year_regardless_of_config(self):
        write_demand_files(self.data_dir, [2019, 2024])
        (self.data_dir / "grid_cell_info.csv").write_text("x\n")
        files = find_demand_files(self.data_dir)
        self.assertEqual(sorted(files), [2019, 2024])
        self.assertEqual(set(files[2024]), set(PROVINCE_NAMES))
        self.assertEqual(files[2019]["ON"].name, "provincial_demand_ON_2019.csv")

    def test_missing_province_in_any_year_fails_naming_the_file(self):
        write_demand_files(self.data_dir, [2023, 2024], skip=("QC_2023",))
        with self.assertRaisesRegex(DataError, "provincial_demand_QC_2023.csv"):
            find_demand_files(self.data_dir)

    def test_no_files_fails(self):
        self.data_dir.mkdir()
        with self.assertRaisesRegex(DataError, "No provincial demand files"):
            find_demand_files(self.data_dir)

    def test_unknown_province_fails(self):
        write_demand_files(self.data_dir, [2024])
        (self.data_dir / "provincial_demand_YT_2024.csv").write_text(demand_csv("YT"))
        with self.assertRaisesRegex(DataError, "unknown provinces.*provincial_demand_YT_2024"):
            find_demand_files(self.data_dir)

    def test_unrecognized_file_name_fails(self):
        write_demand_files(self.data_dir, [2024])
        (self.data_dir / "provincial_demand_ON.csv").write_text("x\n")
        with self.assertRaisesRegex(DataError, "Unrecognized provincial demand file name"):
            find_demand_files(self.data_dir)

    def test_temp_files_are_ignored(self):
        write_demand_files(self.data_dir, [2024])
        (self.data_dir / "provincial_demand_ON_2024.csv.tmp").write_text("partial")
        self.assertEqual(sorted(find_demand_files(self.data_dir)), [2024])


class ReadAnnualDemandTest(TempDirTest):
    def read(self, text: str, code: str = "ON") -> float:
        self.dir.mkdir(exist_ok=True)
        path = self.dir / "provincial_demand_ON_2024.csv"
        path.write_text(text)
        return read_annual_demand(path, code)

    def test_sums_all_hours(self):
        self.assertAlmostEqual(self.read(demand_csv("ON", 2.5)), 2.5 * HOURS_PER_YEAR)

    def test_wrong_hour_count_fails(self):
        with self.assertRaisesRegex(DataError, "expected 8760 hourly rows, got 8700"):
            self.read(demand_csv("ON", hours=8700))

    def test_wrong_province_fails(self):
        with self.assertRaisesRegex(DataError, "province is 'QC', expected 'ON'"):
            self.read(demand_csv("QC"))

    def test_out_of_order_hour_fails(self):
        text = demand_csv("ON").replace(",1,5,1\n", ",1,6,1\n", 1)
        with self.assertRaisesRegex(DataError, "expected hour 5 on row 5, got '6'"):
            self.read(text)

    def test_bad_demand_values_fail_and_name_the_hour(self):
        for bad in ("abc", "", "nan", "inf"):
            with self.subTest(value=bad):
                text = demand_csv("ON").replace(",1,5,1\n", f",1,5,{bad}\n", 1)
                with self.assertRaisesRegex(DataError, "hour 5: .*demand_MWh"):
                    self.read(text)

    def test_negative_demand_is_valid_and_reduces_the_total(self):
        text = demand_csv("ON").replace(",1,5,1\n", ",1,5,-21\n", 1)
        stderr = StringIO()
        with redirect_stderr(stderr):
            total = self.read(text)
        self.assertEqual(total, HOURS_PER_YEAR - 1 - 21)
        self.assertEqual(stderr.getvalue(), "")

    def test_missing_column_fails(self):
        with self.assertRaisesRegex(DataError, "missing required columns"):
            self.read("province,demand_MWh\nON,1\n")


class WriteTest(TempDirTest):
    def read_output(self, path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields = list(reader.fieldnames or [])
            return fields, {r[BA_COLUMN]: r for r in reader}

    def test_one_row_per_ba_and_one_column_per_year(self):
        # PE has one BA, so its demand lands unchanged in that BA; NL is split between two.
        write_demand_files(self.data_dir, [2024, 2022], {"PE": 2.0, "NL": 1.0})
        dest = self.dir / "balancing_area_demand.csv"
        write_balancing_area_demand(find_demand_files(self.data_dir), dest)
        fields, rows = self.read_output(dest)
        self.assertEqual(fields, [BA_COLUMN, "2022", "2024"])
        self.assertAlmostEqual(float(rows["Prince Edward Island.a"]["2024"]), 2.0 * HOURS_PER_YEAR)
        nl = PROVINCE_BA_POPULATION["Newfoundland and Labrador"]
        self.assertAlmostEqual(
            float(rows["Newfoundland and Labrador.b"]["2022"]),
            HOURS_PER_YEAR * nl["Newfoundland and Labrador.b"] / sum(nl.values()),
        )

    def test_failure_leaves_no_output_file(self):
        write_demand_files(self.data_dir, [2024])
        (self.data_dir / "provincial_demand_SK_2024.csv").write_text(demand_csv("SK", "oops"))
        dest = self.dir / "balancing_area_demand.csv"
        with self.assertRaises(DataError):
            write_balancing_area_demand(find_demand_files(self.data_dir), dest)
        self.assertEqual([p.name for p in self.dir.glob("*.csv*")], [])


if __name__ == "__main__":
    unittest.main()
