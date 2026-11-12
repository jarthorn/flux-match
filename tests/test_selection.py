import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from flux_match import paths
from flux_match.errors import DataError
from flux_match.run_context import create_run
from flux_match.steps import select
from flux_match.steps.select.base import (
    GenerationType,
    Site,
    read_sites,
    write_sites,
)

SOLAR = GenerationType.SOLAR
WIND_ONS = GenerationType.WIND_ONSHORE
WIND_OFS = GenerationType.WIND_OFFSHORE


class SiteTest(unittest.TestCase):
    def test_rejects_non_positive_or_non_finite_capacity(self) -> None:
        for capacity in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                Site(1, SOLAR, capacity)

    def test_rejects_plain_string_generation_type(self) -> None:
        with self.assertRaises(ValueError):
            Site(1, "solar", 1.0)  # type: ignore[arg-type]


class SitesFileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "selected_sites.csv"

    def test_round_trip_is_sorted_and_exact(self) -> None:
        sites = [Site(10, WIND_ONS, 0.1 + 0.2), Site(2, WIND_OFS, 500.0), Site(2, SOLAR, 1.5)]
        write_sites(self.path, sites)
        self.assertEqual(
            self.path.read_text().splitlines(),
            [
                "grid_cell,generation_type,capacity_mw",
                "2,solar,1.5",
                "2,wind_ofs,500.0",
                "10,wind_ons,0.30000000000000004",
            ],
        )
        self.assertEqual(read_sites(self.path), sorted(sites))

    def test_empty_selection_writes_header_only(self) -> None:
        write_sites(self.path, [])
        self.assertEqual(self.path.read_text().strip(), "grid_cell,generation_type,capacity_mw")
        self.assertEqual(read_sites(self.path), [])

    def test_write_rejects_duplicate_cell_and_type(self) -> None:
        with self.assertRaisesRegex(DataError, "grid cell 3 solar"):
            write_sites(self.path, [Site(3, SOLAR, 1.0), Site(3, SOLAR, 2.0)])
        self.assertFalse(self.path.exists())

    def test_read_rejects_duplicate_cell_and_type(self) -> None:
        self.path.write_text("grid_cell,generation_type,capacity_mw\n3,solar,1\n3,solar,2\n")
        with self.assertRaisesRegex(DataError, "grid cell 3 solar"):
            read_sites(self.path)

    def test_read_reports_line_of_bad_row(self) -> None:
        for bad_row in ("x,solar,1", "1,wind,1", "1,solar,0", "1,solar,"):
            with self.subTest(row=bad_row):
                self.path.write_text(
                    f"grid_cell,generation_type,capacity_mw\n1,solar,1\n{bad_row}\n"
                )
                with self.assertRaisesRegex(DataError, r"selected_sites\.csv:3:"):
                    read_sites(self.path)

    def test_read_requires_columns(self) -> None:
        self.path.write_text("grid_cell,capacity_mw\n1,1\n")
        with self.assertRaisesRegex(DataError, "generation_type"):
            read_sites(self.path)


class RankingsPathTest(unittest.TestCase):
    """Each wind source has its own rankings file, which select needs fetch to have built."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(paths, "DATA_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, wind_source: str):
        scenario = self.dir / f"{wind_source}.toml"
        scenario.write_text(
            f'[fetch]\nwind_source = "{wind_source}"\n[selection]\nstrategy = "greedy"\n'
        )
        return create_run(scenario, self.dir / "output", datetime(2026, 11, 11, 9, 0))

    def test_uses_the_rankings_of_the_configured_wind_source(self) -> None:
        for source in ("coders", "ninja"):
            (self.dir / f"grid_cell_rankings_{source}.csv").write_text("grid_cell\n")
            self.assertEqual(
                paths.grid_cell_rankings(self.ctx(source)),
                self.dir / f"grid_cell_rankings_{source}.csv",
            )

    def test_missing_rankings_for_the_source_fail_even_if_another_source_has_them(self) -> None:
        (self.dir / "grid_cell_rankings_coders.csv").write_text("grid_cell\n")
        with self.assertRaisesRegex(DataError, r"grid_cell_rankings_ninja\.csv not found"):
            select.run(self.ctx("ninja"))


if __name__ == "__main__":
    unittest.main()
