import math
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.steps.select import STRATEGIES
from flux_match.steps.select.base import CapacityParams, GenerationType, Site
from flux_match.steps.select.wind_tvci import WindTvciStrategy, select_sites

SOLAR = GenerationType.SOLAR
WIND_ONS = GenerationType.WIND_ONSHORE


def wind_tvci_config(table: dict[str, object]) -> Config:
    return Config(
        {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
            "wind-tvci": table,
        },
        Path("wind-tvci.toml"),
    )


class WindTvciConfigTest(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIs(STRATEGIES["wind-tvci"], WindTvciStrategy)

    def test_wind_tvci_scenario_is_valid(self) -> None:
        config = Config.load(paths.SCENARIOS_DIR / "wind-tvci.toml").section("selection")
        self.assertEqual(config.get("strategy", str), "wind-tvci")
        self.assertEqual(WindTvciStrategy(config).candidate_multiple, 2.0)

    def test_accepts_integer_multiple(self) -> None:
        strategy = WindTvciStrategy(wind_tvci_config({"candidate_multiple": 3}))
        self.assertEqual(strategy.candidate_multiple, 3.0)

    def test_rejects_bad_multiple(self) -> None:
        for value in (0.99, 0, -2.0, math.nan, math.inf, "2"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                WindTvciStrategy(wind_tvci_config({"candidate_multiple": value}))

    def test_requires_multiple(self) -> None:
        with self.assertRaisesRegex(ConfigError, "'wind-tvci.candidate_multiple'"):
            WindTvciStrategy(wind_tvci_config({}))


def daily_wave(hours: int = HOURS_PER_YEAR) -> np.ndarray:
    return 0.5 + 0.4 * np.sin(2 * np.pi * np.arange(hours) / 24)


def write_capacity_factors(path: Path, profiles: Mapping[int, np.ndarray]) -> None:
    cells = list(profiles)
    lines = ["h," + ",".join(str(c) for c in cells)]
    for hour in range(HOURS_PER_YEAR):
        values = ",".join(repr(float(profiles[c][hour])) for c in cells)
        lines.append(f"{hour + 1},{values}")
    path.write_text("\n".join(lines) + "\n")


def ranking_row(cell: int, wind_rank: int, solar_rank: int) -> str:
    return f"{cell},{1000 - wind_rank},{1000 - solar_rank},0,0,{solar_rank},{wind_rank},1,1"


class SelectSitesTest(unittest.TestCase):
    # East.a has cells 1-5 and West.a has cell 6. Wind ranks 1 > 2 > 3 > 4 > 5 and solar
    # ranks 5 > 4 > 3 > 2 > 1. "M" mirrors the daily wave W (1 - W).
    #   wind:  1=W 2=W 3=M 4=W 5=M   solar: 1=W 2=M 3=W 4=W 5=W
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "grid_cell_info.csv").write_text(
            "grid_cell,longitude,latitude,balancing_area\n"
            "1,-73.5,45.5,East.a\n"
            "2,-73.6,45.6,East.a\n"
            "3,-63.6,44.6,East.a\n"
            "4,-63.5,44.7,East.a\n"
            "5,-63.4,44.8,East.a\n"
            "6,-123.1,49.3,West.a\n"
        )
        (self.dir / "grid_cell_rankings.csv").write_text(
            "grid_cell,annual_wind_capacity_factor,annual_solar_capacity_factor,"
            "ba_median_daily_solar_capacity,ba_median_daily_wind_capacity,"
            "ba_annual_solar_rank,ba_annual_wind_rank,ba_median_solar_rank,ba_median_wind_rank\n"
            + "\n".join(
                ranking_row(cell, wind_rank, solar_rank)
                for cell, wind_rank, solar_rank in (
                    (1, 1, 5),
                    (2, 2, 4),
                    (3, 3, 3),
                    (4, 4, 2),
                    (5, 5, 1),
                )
            )
            + "\n6,500,500,0,0,1,1,1,1\n"
        )
        self.set_demand(east_mw=2.0)
        w = daily_wave()
        m = 1.0 - w
        write_capacity_factors(self.dir / "wind.csv", {1: w, 2: w, 3: m, 4: w, 5: m, 6: w})
        write_capacity_factors(self.dir / "solar.csv", {1: w, 2: m, 3: w, 4: w, 5: w, 6: w})

    def set_demand(self, east_mw: float) -> None:
        (self.dir / "balancing_area_demand.csv").write_text(
            f"balancing_area,2024\nEast.a,{east_mw * HOURS_PER_YEAR}\nWest.a,{HOURS_PER_YEAR}\n"
        )

    def select(
        self,
        candidate_multiple: float,
        capacity_factor_paths: Mapping[GenerationType, Path] | None = None,
        **capacity: float,
    ) -> list[Site]:
        values = {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
            **capacity,
        }
        return select_sites(
            demand_path=self.dir / "balancing_area_demand.csv",
            rankings_path=self.dir / "grid_cell_rankings.csv",
            grid_cell_info=self.dir / "grid_cell_info.csv",
            capacity_factor_paths=capacity_factor_paths
            or {WIND_ONS: self.dir / "wind.csv", SOLAR: self.dir / "solar.csv"},
            capacity=CapacityParams(**values),
            candidate_multiple=candidate_multiple,
        )

    def east(self, sites: Sequence[Site], generation_type: GenerationType) -> list[Site]:
        return sorted(s for s in sites if s.generation_type is generation_type and s.grid_cell != 6)

    def test_solar_is_greedy_annual_and_wind_complements_solar(self) -> None:
        # Solar greedy by annual rank: 5 then 4. Their CF sum is 2W, so wind's first pick
        # from pool 1-4 is the mirror at 3 (classic TVCI would force best-ranked 1 first).
        sites = self.select(2.0)
        self.assertEqual(self.east(sites, SOLAR), [Site(4, SOLAR, 1.0), Site(5, SOLAR, 1.0)])
        self.assertEqual(
            self.east(sites, WIND_ONS), [Site(1, WIND_ONS, 1.0), Site(3, WIND_ONS, 1.0)]
        )
        self.assertIn(Site(6, WIND_ONS, 1.0), sites)
        self.assertIn(Site(6, SOLAR, 1.0), sites)

    def test_no_solar_falls_back_to_classic_tvci_wind(self) -> None:
        # Without solar, wind forces the best-ranked cell first, then its mirror.
        sites = self.select(2.0, solar_capacity_ratio=0.0)
        self.assertEqual([s for s in sites if s.generation_type is SOLAR], [])
        self.assertEqual(
            self.east(sites, WIND_ONS), [Site(1, WIND_ONS, 1.0), Site(3, WIND_ONS, 1.0)]
        )

    def test_pick_order_prefers_solar_mirror_first(self) -> None:
        # Same sites as classic TVCI wind (1 and 3), but solar seeding picks 3 before 1.
        # Partial capacity on the last pick reveals order: 3 full, then 1 with remainder.
        self.set_demand(east_mw=1.5)
        sites = self.select(2.0, solar_capacity_ratio=1.0)
        self.assertEqual(
            self.east(sites, WIND_ONS),
            [Site(1, WIND_ONS, 0.5), Site(3, WIND_ONS, 1.0)],
        )


if __name__ == "__main__":
    unittest.main()
