import tempfile
import unittest
from pathlib import Path

from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.selection.base import GenerationType, Site
from flux_match.selection.greedy import (
    GreedyStrategy,
    allocate,
    select_sites,
    target_capacity_mw,
)

SOLAR = GenerationType.SOLAR
WIND_ONS = GenerationType.WIND_ONSHORE
WIND_OFS = GenerationType.WIND_OFFSHORE


class AllocateTest(unittest.TestCase):
    def test_fills_best_cells_first_and_partial_on_last(self) -> None:
        sites = allocate([20, 10, 30], 2.5, 1.0, WIND_ONS, "East.a")
        self.assertEqual(
            sites,
            [Site(20, WIND_ONS, 1.0), Site(10, WIND_ONS, 1.0), Site(30, WIND_ONS, 0.5)],
        )

    def test_zero_target_selects_nothing(self) -> None:
        self.assertEqual(allocate([1, 2], 0.0, 1.0, SOLAR, "East.a"), [])

    def test_fails_when_cells_cannot_meet_target(self) -> None:
        with self.assertRaisesRegex(DataError, r"East\.a.*wind_ons"):
            allocate([1, 2], 5.0, 2.0, WIND_ONS, "East.a")


class GreedyConfigTest(unittest.TestCase):
    def test_accepts_integer_caps_from_toml(self) -> None:
        strategy = GreedyStrategy(
            Config(
                {
                    "solar_capacity_ratio": 1,
                    "wind_capacity_ratio": 0,
                    "max_wind_capacity_mw_per_cell": 1000,
                    "max_solar_capacity_mw_per_cell": 500,
                },
                Path("default.toml"),
            )
        )
        self.assertEqual(strategy.max_wind_capacity_mw_per_cell, 1000.0)
        self.assertEqual(strategy.max_solar_capacity_mw_per_cell, 500.0)
        self.assertEqual(strategy.wind_capacity_ratio, 0.0)

    def test_rejects_negative_ratio_and_non_positive_cap(self) -> None:
        base = {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
        }
        cases = (
            ("solar_capacity_ratio", -0.1),
            ("max_wind_capacity_mw_per_cell", 0.0),
            ("max_solar_capacity_mw_per_cell", float("nan")),
        )
        for key, value in cases:
            with self.subTest(key=key), self.assertRaises(ConfigError):
                GreedyStrategy(Config({**base, key: value}, Path("default.toml")))


class SelectSitesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # East.a: cell 20 is best wind, cell 10 is best solar. West.a has one cell.
        (self.dir / "grid_cell_info.csv").write_text(
            "grid_cell,balancing_area\n10,East.a\n20,East.a\n30,West.a\n"
        )
        (self.dir / "grid_cell_rankings.csv").write_text(
            "grid_cell,annual_wind_capacity_factor,annual_solar_capacity_factor,"
            "ba_median_daily_solar_capacity,ba_median_daily_wind_capacity,"
            "ba_annual_solar_rank,ba_annual_wind_rank,ba_median_solar_rank,ba_median_wind_rank\n"
            "10,1,3,0,0,1,2,1,2\n"
            "20,2,1,0,0,2,1,2,1\n"
            "30,1,1,0,0,1,1,1,1\n"
        )
        # 2022 values are large so a year mix-up would change the MW totals.
        (self.dir / "balancing_area_demand.csv").write_text(
            "balancing_area,2022,2024\n"
            f"East.a,999999,{3 * HOURS_PER_YEAR}\n"
            f"West.a,999999,{HOURS_PER_YEAR}\n"
        )

    def select(self, **overrides: float) -> list[Site]:
        params = {
            "demand_path": self.dir / "balancing_area_demand.csv",
            "rankings_path": self.dir / "grid_cell_rankings.csv",
            "grid_cell_info": self.dir / "grid_cell_info.csv",
            "wind_capacity_ratio": 1.0,
            "solar_capacity_ratio": 1.0,
            "max_wind_mw": 2.0,
            "max_solar_mw": 2.0,
            **overrides,
        }
        return select_sites(**params)

    def test_target_is_ratio_times_average_load(self) -> None:
        self.assertEqual(target_capacity_mw(3 * HOURS_PER_YEAR, 1.0), 3.0)
        self.assertEqual(target_capacity_mw(HOURS_PER_YEAR, 0.5), 0.5)

    def test_allocates_by_rank_with_partial_last_cell_using_newest_year(self) -> None:
        # East average load 3 MW: wind 20@2 + 10@1; solar 10@2 + 20@1. West: 30@1 each.
        self.assertEqual(
            sorted(self.select()),
            [
                Site(10, SOLAR, 2.0),
                Site(10, WIND_ONS, 1.0),
                Site(20, SOLAR, 1.0),
                Site(20, WIND_ONS, 2.0),
                Site(30, SOLAR, 1.0),
                Site(30, WIND_ONS, 1.0),
            ],
        )

    def test_zero_ratio_omits_that_technology(self) -> None:
        sites = self.select(wind_capacity_ratio=0.0)
        self.assertTrue(all(s.generation_type is SOLAR for s in sites))
        self.assertEqual(len(sites), 3)

    def test_fails_when_a_ba_has_too_few_cells(self) -> None:
        with self.assertRaisesRegex(DataError, r"East\.a"):
            self.select(max_wind_mw=1.0)

    def test_never_selects_offshore_wind(self) -> None:
        self.assertTrue(all(s.generation_type is not WIND_OFS for s in self.select()))


if __name__ == "__main__":
    unittest.main()
