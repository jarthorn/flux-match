import csv
import tempfile
import unittest
from pathlib import Path

from flux_match.errors import DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.viz.cf_stats_compare import (
    BALANCING_AREA,
    load_sample,
    parse_cf_stats_filename,
    quartile_stats,
)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class ParseFilenameTest(unittest.TestCase):
    def test_extracts_type_year_source_and_timestamp(self):
        path = Path("output/cf-stats-wind-2025-ninja-20261114-1205.csv")
        meta = parse_cf_stats_filename(path)
        self.assertEqual(meta.generation_type, "wind")
        self.assertEqual(meta.year, 2025)
        self.assertEqual(meta.source, "ninja")
        self.assertEqual(meta.timestamp, "20261114-1205")
        self.assertEqual(meta.source_label, "renewables.ninja")

    def test_coders_display_label(self):
        meta = parse_cf_stats_filename(Path("cf-stats-solar-2021-coders-20261113-1459.csv"))
        self.assertEqual(meta.generation_type, "solar")
        self.assertEqual(meta.source_label, "CODERS")

    def test_rejects_legacy_name_without_year(self):
        with self.assertRaisesRegex(DataError, "does not match"):
            parse_cf_stats_filename(Path("cf-stats-wind-coders-20261114-1128.csv"))

    def test_rejects_unrelated_name(self):
        with self.assertRaisesRegex(DataError, "does not match"):
            parse_cf_stats_filename(Path("summary.csv"))


class QuartileStatsTest(unittest.TestCase):
    def test_known_values(self):
        # Inclusive quartiles on 1..4: Q1=1.75? Let's use a clear set.
        # For [1, 2, 3, 4] with inclusive method: quantiles n=4 -> Q1, Q2, Q3.
        stats = quartile_stats([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["max"], 4.0)
        self.assertEqual(stats["median"], 2.5)
        self.assertAlmostEqual(stats["p25"], 1.75)
        self.assertAlmostEqual(stats["p75"], 3.25)

    def test_empty_fails(self):
        with self.assertRaisesRegex(DataError, "empty series"):
            quartile_stats([])


class LoadSampleTest(TempDirTest):
    def _write_cf_stats(
        self,
        name: str,
        *,
        hours: int = HOURS_PER_YEAR,
        include_ba: bool = True,
        ba_name: str = BALANCING_AREA,
    ) -> Path:
        path = self.dir / name
        fieldnames = ["h"]
        if include_ba:
            fieldnames.append(ba_name)
        fieldnames.append("all_cells")
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for hour in range(1, hours + 1):
                row = {"h": hour, "all_cells": 0.1 + hour * 1e-6}
                if include_ba:
                    row[ba_name] = 0.2 + hour * 1e-6
                writer.writerow(row)
        return path

    def test_loads_expected_length(self):
        path = self._write_cf_stats("cf-stats-wind-2025-coders-20261114-1205.csv")
        sample = load_sample(path)
        self.assertEqual(len(sample.hours), HOURS_PER_YEAR)
        self.assertEqual(sample.hours[0], 1)
        self.assertEqual(sample.hours[-1], HOURS_PER_YEAR)
        self.assertEqual(sample.meta.year, 2025)
        self.assertEqual(len(sample.balancing_area), HOURS_PER_YEAR)
        self.assertEqual(len(sample.all_cells), HOURS_PER_YEAR)

    def test_missing_balancing_area_column_fails(self):
        path = self._write_cf_stats(
            "cf-stats-wind-2025-coders-20261114-1205.csv",
            include_ba=False,
        )
        with self.assertRaisesRegex(DataError, "missing column"):
            load_sample(path)

    def test_wrong_hour_count_fails(self):
        path = self._write_cf_stats(
            "cf-stats-wind-2025-ninja-20261114-1205.csv",
            hours=10,
        )
        with self.assertRaisesRegex(DataError, f"expected {HOURS_PER_YEAR} hours"):
            load_sample(path)

    def test_missing_file_fails(self):
        path = self.dir / "cf-stats-wind-2025-coders-20261114-1205.csv"
        with self.assertRaisesRegex(DataError, "not found"):
            load_sample(path)


if __name__ == "__main__":
    unittest.main()
