import csv
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.selection import STRATEGIES, greedy
from flux_match.selection.base import CapacityParams, GenerationType, Site
from flux_match.selection.cluster import ClusterStrategy, round_robin_order, select_sites
from flux_match.selection.kmeans import GeoKMeans

SOLAR = GenerationType.SOLAR
WIND_ONS = GenerationType.WIND_ONSHORE


class RoundRobinOrderTest(unittest.TestCase):
    def test_one_cell_per_cluster_per_round_best_first(self) -> None:
        cluster_of = {1: 1, 2: 1, 3: 2, 4: 3, 5: 2}
        self.assertEqual(round_robin_order([1, 2, 3, 4, 5], cluster_of), [1, 3, 4, 2, 5])

    def test_later_round_is_ordered_by_rank_not_cluster(self) -> None:
        # Cluster 1's second cell (5) ranks below cluster 2's second cell (3).
        cluster_of = {1: 1, 2: 2, 3: 2, 5: 1}
        self.assertEqual(round_robin_order([1, 2, 3, 5], cluster_of), [1, 2, 3, 5])

    def test_single_cluster_keeps_rank_order(self) -> None:
        self.assertEqual(round_robin_order([4, 2, 9], dict.fromkeys([4, 2, 9], 1)), [4, 2, 9])


CLUSTER_TABLE = {"cluster_count": 10, "seed": 0, "n_init": 1}


def cluster_config(cluster: dict[str, object]) -> Config:
    return Config(
        {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
            "cluster": cluster,
        },
        Path("cluster.toml"),
    )


class ClusterConfigTest(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIs(STRATEGIES["cluster"], ClusterStrategy)

    def test_cluster_scenario_is_valid(self) -> None:
        config = Config.load(paths.SCENARIOS_DIR / "cluster.toml").section("selection")
        self.assertEqual(config.get("strategy", str), "cluster")
        strategy = ClusterStrategy(config)
        self.assertEqual(strategy.kmeans.k, 10)

    def test_rejects_bad_cluster_params(self) -> None:
        cases = (
            {"cluster_count": 0},
            {"cluster_count": 2.5},
            {"n_init": 0},
            {"seed": -1},
            {"seed": 2**32},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ConfigError):
                ClusterStrategy(cluster_config({**CLUSTER_TABLE, **case}))

    def test_requires_cluster_table_keys(self) -> None:
        for key in CLUSTER_TABLE:
            table = {k: v for k, v in CLUSTER_TABLE.items() if k != key}
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, f"'cluster.{key}'"):
                ClusterStrategy(cluster_config(table))


class SelectSitesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # East.a has two geographic groups: {1, 2} near Montreal and {3, 4} near Halifax.
        # Wind ranks 1 > 2 > 3 > 4; solar ranks 3 > 4 > 1 > 2. West.a has one cell.
        (self.dir / "grid_cell_info.csv").write_text(
            "grid_cell,balancing_area,latitude,longitude\n"
            "1,East.a,45.5,-73.5\n"
            "2,East.a,45.6,-73.6\n"
            "3,East.a,44.6,-63.6\n"
            "4,East.a,44.7,-63.5\n"
            "5,West.a,49.3,-123.1\n"
        )
        (self.dir / "grid_cell_rankings.csv").write_text(
            "grid_cell,annual_wind_capacity_factor,annual_solar_capacity_factor,"
            "ba_median_daily_solar_capacity,ba_median_daily_wind_capacity,"
            "ba_annual_solar_rank,ba_annual_wind_rank,ba_median_solar_rank,ba_median_wind_rank\n"
            "1,4000,1200,0,0,3,1,1,1\n"
            "2,3000,1100,0,0,4,2,2,2\n"
            "3,2000,1400,0,0,1,3,3,3\n"
            "4,1000,1300,0,0,2,4,4,4\n"
            "5,2500,1000,0,0,1,1,1,1\n"
        )
        (self.dir / "balancing_area_demand.csv").write_text(
            f"balancing_area,2024\nEast.a,{2.5 * HOURS_PER_YEAR}\nWest.a,{HOURS_PER_YEAR}\n"
        )

    def capacity(self, **overrides: float) -> CapacityParams:
        values = {
            "solar_capacity_ratio": 1.0,
            "wind_capacity_ratio": 1.0,
            "max_wind_capacity_mw_per_cell": 1.0,
            "max_solar_capacity_mw_per_cell": 1.0,
            **overrides,
        }
        return CapacityParams(**values)

    def select(self, **capacity: float) -> list[Site]:
        return select_sites(
            demand_path=self.dir / "balancing_area_demand.csv",
            rankings_path=self.dir / "grid_cell_rankings.csv",
            grid_cell_info=self.dir / "grid_cell_info.csv",
            clusters_path=self.dir / "clusters.csv",
            capacity=self.capacity(**capacity),
            kmeans=GeoKMeans(k=2, seed=0, n_init=5),
        )

    def test_spreads_sites_across_clusters_then_starts_a_new_round(self) -> None:
        # East targets 2.5 MW at 1 MW per cell, so three sites: the best cell, the best in
        # the other cluster, then round two's best. Greedy would pick 1, 2, 3 for wind.
        self.assertEqual(
            sorted(self.select()),
            [
                Site(1, SOLAR, 1.0),
                Site(1, WIND_ONS, 1.0),
                Site(2, WIND_ONS, 0.5),
                Site(3, SOLAR, 1.0),
                Site(3, WIND_ONS, 1.0),
                Site(4, SOLAR, 0.5),
                Site(5, SOLAR, 1.0),
                Site(5, WIND_ONS, 1.0),
            ],
        )

    def test_one_site_matches_greedy(self) -> None:
        capacity = {"max_wind_capacity_mw_per_cell": 5.0, "max_solar_capacity_mw_per_cell": 5.0}
        expected = greedy.select_sites(
            demand_path=self.dir / "balancing_area_demand.csv",
            rankings_path=self.dir / "grid_cell_rankings.csv",
            grid_cell_info=self.dir / "grid_cell_info.csv",
            capacity=self.capacity(**capacity),
            ranking_metric="annual_capacity",
        )
        self.assertEqual(sorted(self.select(**capacity)), sorted(expected))

    def test_uses_every_cell_and_raises_cap_when_target_exceeds_them(self) -> None:
        # East needs 10 MW of wind from 4 cells capped at 1 MW: every cell, at 2.5 MW each.
        stdout = StringIO()
        with redirect_stdout(stdout):
            sites = self.select(wind_capacity_ratio=4.0)
        east_wind = sorted(s for s in sites if s.generation_type is WIND_ONS and s.grid_cell < 5)
        self.assertEqual(east_wind, [Site(c, WIND_ONS, 2.5) for c in (1, 2, 3, 4)])
        self.assertIn("Raising the maximum to 2.5", stdout.getvalue())

    def test_writes_clusters_file(self) -> None:
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            self.select()
        with (self.dir / "clusters.csv").open(newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["grid_cell"] for r in rows], ["1", "2", "3", "4", "5"])
        east = {r["grid_cell"]: r["cluster_id"] for r in rows if r["balancing_area"] == "East.a"}
        self.assertEqual(east["1"], east["2"])
        self.assertEqual(east["3"], east["4"])
        self.assertNotEqual(east["1"], east["3"])
        self.assertEqual({east["1"], east["3"]}, {"1", "2"})
        self.assertEqual(rows[4]["cluster_id"], "1")
        self.assertEqual(rows[0]["annual_wind_capacity_factor"], "4000.0")
        self.assertEqual(rows[0]["latitude"], "45.5")
        # West.a has one cell, fewer than K=2.
        self.assertIn("West.a", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
