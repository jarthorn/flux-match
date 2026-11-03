import math
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import numpy as np

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.selection import STRATEGIES
from flux_match.selection.base import CapacityParams, GenerationType, Site
from flux_match.selection.total_variation import total_variation_complementarity
from flux_match.selection.tvci import (
    TvciStrategy,
    candidate_pool_size,
    select_sites,
    tvci_order,
)

SOLAR = GenerationType.SOLAR
WIND_ONS = GenerationType.WIND_ONSHORE


def reference_order(capacity_factors: np.ndarray, count: int) -> list[int]:
    """Loop-based transcription of docs/complementarity-approach.md, step 3, including its
    running-average update, to check tvci_order against."""
    remaining = list(range(1, len(capacity_factors)))
    picked = [0]
    total = capacity_factors[0].copy()
    while len(picked) < count:
        scores = [
            float(total_variation_complementarity(total, capacity_factors[[i]])[0])
            for i in remaining
        ]
        best = remaining.pop(scores.index(max(scores)))
        picked.append(best)
        x = len(picked)
        total += capacity_factors[best]
    return picked


class TvciOrderTest(unittest.TestCase):
    def test_matches_reference_on_random_series(self) -> None:
        rng = np.random.default_rng(4)
        for count in (1, 2, 5, 12, 20):
            capacity_factors = rng.random((20, 300))
            cells = list(range(100, 120))
            with self.subTest(count=count):
                self.assertEqual(
                    tvci_order(cells, capacity_factors, count),
                    [cells[i] for i in reference_order(capacity_factors, count)],
                )

    def test_best_ranked_first_then_most_complementary(self) -> None:
        wave = np.array([0.0, 1.0, 0.0, 1.0, 0.0])
        noise = np.array([0.2, 0.5, 0.1, 0.9, 0.4])
        capacity_factors = np.stack([wave, wave, noise, 1.0 - wave])
        self.assertEqual(tvci_order([7, 8, 9, 10], capacity_factors, 2), [7, 10])

    def test_scores_against_mean_of_picks_not_latest_pick(self) -> None:
        # After 1 and 2 the mean is flat, so 3 (mirroring 2) and 4 (copying 1) both score 0
        # and rank breaks the tie. Scoring against pick 2 alone would choose 4.
        wave = np.array([0.0, 1.0, 0.0, 1.0])
        capacity_factors = np.stack([wave, 1.0 - wave, 1.0 - wave, wave])
        self.assertEqual(tvci_order([1, 2, 3, 4], capacity_factors, 3), [1, 2, 3])

    def test_ties_go_to_better_ranked_cell(self) -> None:
        wave = np.array([0.0, 1.0, 0.0, 1.0])
        capacity_factors = np.stack([wave, 1.0 - wave, 1.0 - wave])
        self.assertEqual(tvci_order([5, 3, 9], capacity_factors, 2), [5, 3])

    def test_count_zero_and_all(self) -> None:
        capacity_factors = np.random.default_rng(5).random((3, 10))
        self.assertEqual(tvci_order([1, 2, 3], capacity_factors, 0), [])
        self.assertEqual(sorted(tvci_order([1, 2, 3], capacity_factors, 3)), [1, 2, 3])
        self.assertEqual(tvci_order([], np.empty((0, 10)), 0), [])

    def test_constant_mean_and_candidate_names_the_cells(self) -> None:
        capacity_factors = np.array([[0.3] * 4, [0.0, 1.0, 0.0, 1.0], [0.5] * 4])
        with self.assertRaisesRegex(DataError, r"grid cells \[10\].*grid cells \[30\]"):
            tvci_order([10, 20, 30], capacity_factors, 2)

    def test_rejects_bad_count_or_shape(self) -> None:
        capacity_factors = np.zeros((2, 4))
        for count in (-1, 3):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "count"):
                tvci_order([1, 2], capacity_factors, count)
        with self.assertRaisesRegex(ValueError, "one row per cell"):
            tvci_order([1, 2, 3], capacity_factors, 1)


class CandidatePoolSizeTest(unittest.TestCase):
    def test_rounds_up_and_caps_at_cells_available(self) -> None:
        self.assertEqual(candidate_pool_size(2, 10, 2.0), 4)
        self.assertEqual(candidate_pool_size(3, 10, 1.5), 5)
        self.assertEqual(candidate_pool_size(2, 3, 2.0), 3)
        self.assertEqual(candidate_pool_size(0, 10, 2.0), 0)


def tvci_config(table: dict[str, object]) -> Config:
    return Config(
        {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
            "tvci": table,
        },
        Path("tvci.toml"),
    )


class TvciConfigTest(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIs(STRATEGIES["tvci"], TvciStrategy)

    def test_tvci_scenario_is_valid(self) -> None:
        config = Config.load(paths.SCENARIOS_DIR / "tvci.toml").section("selection")
        self.assertEqual(config.get("strategy", str), "tvci")
        self.assertEqual(TvciStrategy(config).candidate_multiple, 2.0)

    def test_accepts_integer_multiple(self) -> None:
        strategy = TvciStrategy(tvci_config({"candidate_multiple": 3}))
        self.assertEqual(strategy.candidate_multiple, 3.0)

    def test_rejects_bad_multiple(self) -> None:
        for value in (0.99, 0, -2.0, math.nan, math.inf, "2"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                TvciStrategy(tvci_config({"candidate_multiple": value}))

    def test_requires_multiple(self) -> None:
        with self.assertRaisesRegex(ConfigError, "'tvci.candidate_multiple'"):
            TvciStrategy(tvci_config({}))


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
    # ranks 5 > 4 > 3 > 2 > 1. "M" mirrors the daily wave W (1 - W), so it is perfectly
    # complementary to it; copies of W are not complementary to each other at all.
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

    def test_picks_complementary_cell_from_pool(self) -> None:
        # East needs 2 sites, so 4 candidates. Wind pool 1-4: 1, then its mirror 3.
        # Solar pool 5, 4, 3, 2: 5, then its mirror 2. Greedy would pick 1, 2 and 5, 4.
        sites = self.select(2.0)
        self.assertEqual(
            self.east(sites, WIND_ONS), [Site(1, WIND_ONS, 1.0), Site(3, WIND_ONS, 1.0)]
        )
        self.assertEqual(self.east(sites, SOLAR), [Site(2, SOLAR, 1.0), Site(5, SOLAR, 1.0)])
        self.assertIn(Site(6, WIND_ONS, 1.0), sites)
        self.assertIn(Site(6, SOLAR, 1.0), sites)

    def test_multiple_of_one_uses_greedy_cells(self) -> None:
        sites = self.select(1.0)
        self.assertEqual(
            self.east(sites, WIND_ONS), [Site(1, WIND_ONS, 1.0), Site(2, WIND_ONS, 1.0)]
        )
        self.assertEqual(self.east(sites, SOLAR), [Site(4, SOLAR, 1.0), Site(5, SOLAR, 1.0)])

    def test_pool_size_rounds_up(self) -> None:
        # ceil(1.5 * 2) = 3 wind candidates, which reaches the mirror in cell 3.
        sites = self.select(1.5)
        self.assertEqual(
            self.east(sites, WIND_ONS), [Site(1, WIND_ONS, 1.0), Site(3, WIND_ONS, 1.0)]
        )

    def test_last_pick_takes_partial_capacity(self) -> None:
        # 2.5 MW needs 3 sites from a pool of 3: 1, its mirror 3, then 2 with the remainder.
        self.set_demand(east_mw=2.5)
        sites = self.select(1.0, solar_capacity_ratio=0.0)
        self.assertEqual(
            self.east(sites, WIND_ONS),
            [Site(1, WIND_ONS, 1.0), Site(2, WIND_ONS, 0.5), Site(3, WIND_ONS, 1.0)],
        )

    def test_uses_every_cell_and_raises_cap_when_target_exceeds_them(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            sites = self.select(2.0, wind_capacity_ratio=5.0, solar_capacity_ratio=0.0)
        self.assertEqual(self.east(sites, WIND_ONS), [Site(c, WIND_ONS, 2.0) for c in range(1, 6)])
        self.assertIn("Raising the maximum to 2", stdout.getvalue())

    def test_skips_table_with_no_target(self) -> None:
        missing = self.dir / "missing.csv"
        sites = self.select(
            2.0,
            capacity_factor_paths={WIND_ONS: self.dir / "wind.csv", SOLAR: missing},
            solar_capacity_ratio=0.0,
        )
        self.assertEqual([s for s in sites if s.generation_type is SOLAR], [])

    def test_missing_candidate_column_fails(self) -> None:
        w = daily_wave()
        write_capacity_factors(self.dir / "wind.csv", {1: w, 2: w, 4: w, 5: w, 6: w})
        with self.assertRaisesRegex(DataError, r"missing grid cell columns \[3\]"):
            self.select(2.0)

    def test_undefined_complementarity_names_area_and_type(self) -> None:
        flat = np.full(HOURS_PER_YEAR, 0.2)
        w = daily_wave()
        write_capacity_factors(self.dir / "wind.csv", {1: flat, 2: flat, 3: w, 4: w, 5: w, 6: w})
        with self.assertRaisesRegex(DataError, r"East\.a wind_ons: .*grid cells \[2\]"):
            self.select(2.0)


if __name__ == "__main__":
    unittest.main()
