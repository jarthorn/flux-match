import random
import unittest
from contextlib import redirect_stderr
from io import StringIO

from flux_match.errors import DataError
from flux_match.selection.kmeans import GeoKMeans, GeoPoint


def as_sets(clusters: list[list[int]]) -> set[frozenset[int]]:
    return {frozenset(c) for c in clusters}


class GeoKMeansTest(unittest.TestCase):
    def test_recovers_well_separated_groups(self) -> None:
        west = [GeoPoint(i, 49 + i * 0.1, -120 + i * 0.1) for i in range(5)]
        east = [GeoPoint(10 + i, 46 + i * 0.1, -65 - i * 0.1) for i in range(5)]
        clusters = GeoKMeans(k=2, seed=0, n_init=5).cluster(west + east, "BA")
        self.assertEqual(
            as_sets(clusters),
            {frozenset(p.id for p in west), frozenset(p.id for p in east)},
        )

    def test_every_point_appears_exactly_once(self) -> None:
        rng = random.Random(1)
        points = [GeoPoint(i, rng.uniform(45, 60), rng.uniform(-120, -60)) for i in range(60)]
        clusters = GeoKMeans(k=7, seed=3, n_init=2).cluster(points, "BA")
        self.assertEqual(len(clusters), 7)
        self.assertTrue(all(clusters))
        self.assertEqual(sorted(i for c in clusters for i in c), list(range(60)))

    def test_same_seed_gives_same_clusters(self) -> None:
        rng = random.Random(2)
        points = [GeoPoint(i, rng.uniform(45, 60), rng.uniform(-120, -60)) for i in range(80)]
        first = GeoKMeans(k=10, seed=42, n_init=3).cluster(points, "BA")
        second = GeoKMeans(k=10, seed=42, n_init=3).cluster(points, "BA")
        self.assertEqual(first, second)

    def test_clamps_k_to_point_count_with_warning(self) -> None:
        points = [GeoPoint(i, 46 + i, -63) for i in range(3)]
        stderr = StringIO()
        with redirect_stderr(stderr):
            clusters = GeoKMeans(k=10, seed=0, n_init=1).cluster(points, "Prince Edward Island.a")
        self.assertEqual(as_sets(clusters), {frozenset({0}), frozenset({1}), frozenset({2})})
        self.assertIn("Prince Edward Island.a", stderr.getvalue())
        self.assertIn("using 3 clusters", stderr.getvalue())

    def test_projection_groups_by_distance_not_degrees(self) -> None:
        # At latitude 60, 1 degree of longitude (~56 km) is shorter than 0.7 degrees of
        # latitude (~78 km). Raw degrees would pair the points by longitude instead.
        points = [
            GeoPoint(1, 60.0, 0.0),
            GeoPoint(2, 60.0, 1.0),
            GeoPoint(3, 60.7, 0.0),
            GeoPoint(4, 60.7, 1.0),
        ]
        clusters = GeoKMeans(k=2, seed=0, n_init=10).cluster(points, "BA")
        self.assertEqual(as_sets(clusters), {frozenset({1, 2}), frozenset({3, 4})})

    def test_rejects_cells_sharing_a_location(self) -> None:
        points = [GeoPoint(1, 50.0, -100.0), GeoPoint(2, 50.0, -100.0)]
        with self.assertRaisesRegex(DataError, "share a location"):
            GeoKMeans(k=2, seed=0, n_init=1).cluster(points, "BA")

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            GeoKMeans(k=2, seed=0, n_init=1).cluster([], "BA")


if __name__ == "__main__":
    unittest.main()
