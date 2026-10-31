import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.selection.base import (
    CapacityParams,
    GenerationType,
    Site,
    allocate,
    target_capacity_mw,
)
from flux_match.selection.greedy import GreedyStrategy, select_sites

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

    def test_raises_per_cell_cap_when_too_low(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            sites = allocate([1, 2], 5.0, 2.0, WIND_ONS, "East.a")
        self.assertEqual(
            sites,
            [Site(1, WIND_ONS, 2.5), Site(2, WIND_ONS, 2.5)],
        )
        self.assertIn(
            "The value of max_wind_capacity_mw_per_cell is too low to meet "
            "requested capacity in East.a. "
            "Raising the maximum to 2.5 so that capacity can be supplied",
            stdout.getvalue(),
        )

    def test_raises_solar_cap_when_too_low(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            sites = allocate([7], 10.0, 3.0, SOLAR, "West.a")
        self.assertEqual(sites, [Site(7, SOLAR, 10.0)])
        self.assertIn("max_solar_capacity_mw_per_cell", stdout.getvalue())
        self.assertIn("Raising the maximum to 10", stdout.getvalue())

    def test_fails_when_there_are_no_cells(self) -> None:
        with self.assertRaisesRegex(DataError, r"East\.a.*wind_ons"):
            allocate([], 5.0, 2.0, WIND_ONS, "East.a")

    def test_raised_cap_covers_target_despite_float_rounding(self) -> None:
        # 22701 / 489 is a float that looks exact as n * (target/n) but leaves a
        # leftover when capacity is subtracted cell by cell.
        n = 489
        target = 22701.0
        stdout = StringIO()
        with redirect_stdout(stdout):
            sites = allocate(list(range(n)), target, 10.0, WIND_ONS, "British Columbia.a")
        self.assertEqual(len(sites), n)
        self.assertAlmostEqual(sum(s.capacity_mw for s in sites), target, places=9)
        self.assertIn("Raising the maximum to", stdout.getvalue())


def selection_config(**overrides: object) -> Config:
    greedy = overrides.pop("greedy", {"ranking_metric": "annual_capacity"})
    return Config(
        {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
            **overrides,
            "greedy": greedy,
        },
        Path("default.toml"),
    )


class GreedyConfigTest(unittest.TestCase):
    def test_accepts_integer_caps_from_toml(self) -> None:
        strategy = GreedyStrategy(
            selection_config(
                solar_capacity_ratio=1,
                wind_capacity_ratio=0,
                max_wind_capacity_mw_per_cell=1000,
                max_solar_capacity_mw_per_cell=500,
                greedy={"ranking_metric": "median_daily_capacity"},
            )
        )
        self.assertEqual(strategy.capacity.max_wind_capacity_mw_per_cell, 1000.0)
        self.assertEqual(strategy.capacity.max_solar_capacity_mw_per_cell, 500.0)
        self.assertEqual(strategy.capacity.wind_capacity_ratio, 0.0)
        self.assertEqual(strategy.ranking_metric, "median_daily_capacity")

    def test_rejects_negative_ratio_and_non_positive_cap(self) -> None:
        cases = (
            ("solar_capacity_ratio", -0.1),
            ("max_wind_capacity_mw_per_cell", 0.0),
            ("max_solar_capacity_mw_per_cell", float("nan")),
        )
        for key, value in cases:
            with self.subTest(key=key), self.assertRaises(ConfigError):
                GreedyStrategy(selection_config(**{key: value}))

    def test_rejects_unknown_ranking_metric(self) -> None:
        with self.assertRaisesRegex(ConfigError, "ranking_metric"):
            GreedyStrategy(selection_config(greedy={"ranking_metric": "peak_hour"}))


class SelectSitesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # East.a annual: 20 best wind, 10 best solar. Median: 10 best wind, 20 best solar.
        (self.dir / "grid_cell_info.csv").write_text(
            "grid_cell,balancing_area,latitude,longitude\n"
            "10,East.a,45,-70\n20,East.a,46,-71\n30,West.a,50,-120\n"
        )
        (self.dir / "grid_cell_rankings.csv").write_text(
            "grid_cell,annual_wind_capacity_factor,annual_solar_capacity_factor,"
            "ba_median_daily_solar_capacity,ba_median_daily_wind_capacity,"
            "ba_annual_solar_rank,ba_annual_wind_rank,ba_median_solar_rank,ba_median_wind_rank\n"
            "10,1,3,0,0,1,2,2,1\n"
            "20,2,1,0,0,2,1,1,2\n"
            "30,1,1,0,0,1,1,1,1\n"
        )
        # 2022 values are large so a year mix-up would change the MW totals.
        (self.dir / "balancing_area_demand.csv").write_text(
            "balancing_area,2022,2024\n"
            f"East.a,999999,{3 * HOURS_PER_YEAR}\n"
            f"West.a,999999,{HOURS_PER_YEAR}\n"
        )

    def select(self, ranking_metric: str = "annual_capacity", **capacity: float) -> list[Site]:
        return select_sites(
            demand_path=self.dir / "balancing_area_demand.csv",
            rankings_path=self.dir / "grid_cell_rankings.csv",
            grid_cell_info=self.dir / "grid_cell_info.csv",
            capacity=CapacityParams(
                **{
                    "wind_capacity_ratio": 1.0,
                    "solar_capacity_ratio": 1.0,
                    "max_wind_capacity_mw_per_cell": 2.0,
                    "max_solar_capacity_mw_per_cell": 2.0,
                    **capacity,
                }
            ),
            ranking_metric=ranking_metric,
        )

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

    def test_median_daily_capacity_uses_median_ranks(self) -> None:
        # East median: wind 10@2 + 20@1; solar 20@2 + 10@1. West: 30@1 each.
        self.assertEqual(
            sorted(self.select(ranking_metric="median_daily_capacity")),
            [
                Site(10, SOLAR, 1.0),
                Site(10, WIND_ONS, 2.0),
                Site(20, SOLAR, 2.0),
                Site(20, WIND_ONS, 1.0),
                Site(30, SOLAR, 1.0),
                Site(30, WIND_ONS, 1.0),
            ],
        )

    def test_zero_ratio_omits_that_technology(self) -> None:
        sites = self.select(wind_capacity_ratio=0.0)
        self.assertTrue(all(s.generation_type is SOLAR for s in sites))
        self.assertEqual(len(sites), 3)

    def test_raises_cap_when_a_ba_has_too_few_cells(self) -> None:
        # East average load 3 MW, two cells, max_wind 1 MW: raise wind cap to 1.5.
        stdout = StringIO()
        with redirect_stdout(stdout):
            sites = self.select(max_wind_capacity_mw_per_cell=1.0)
        self.assertEqual(
            sorted(sites),
            [
                Site(10, SOLAR, 2.0),
                Site(10, WIND_ONS, 1.5),
                Site(20, SOLAR, 1.0),
                Site(20, WIND_ONS, 1.5),
                Site(30, SOLAR, 1.0),
                Site(30, WIND_ONS, 1.0),
            ],
        )
        self.assertIn("max_wind_capacity_mw_per_cell", stdout.getvalue())
        self.assertIn("Raising the maximum to 1.5", stdout.getvalue())

    def test_never_selects_offshore_wind(self) -> None:
        self.assertTrue(all(s.generation_type is not WIND_OFS for s in self.select()))


if __name__ == "__main__":
    unittest.main()
