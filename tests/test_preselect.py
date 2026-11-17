import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from test_fetch import GRID_ROW, capacity_factor_csv, fetch_config, read_rows

from flux_match import paths
from flux_match.errors import DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.preselect.preselect import run


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

    def _run(self, cfg=None) -> None:
        with redirect_stdout(StringIO()):
            run(cfg or fetch_config())

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


if __name__ == "__main__":
    unittest.main()
