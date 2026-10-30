"""Geographic K-means clustering of grid cells, a thin wrapper around scikit-learn's KMeans.

Locations are projected before clustering so that distances are roughly proportional to
kilometres: x = longitude * cos(mean latitude), y = latitude. A degree of longitude in
Canada is only 0.5-0.7 of a degree of latitude, so raw coordinates would stretch clusters
east-west. The projection is accurate enough for one province, not for the whole country.
"""

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.cluster import KMeans

from flux_match.errors import DataError


@dataclass(frozen=True)
class GeoPoint:
    id: int
    latitude: float
    longitude: float


class GeoKMeans:
    def __init__(self, k: int, seed: int, n_init: int) -> None:
        if k < 1 or n_init < 1 or seed < 0:
            raise ValueError(f"need k >= 1, n_init >= 1 and seed >= 0, got {k}, {n_init}, {seed}")
        self.k = k
        self.seed = seed
        self.n_init = n_init

    def cluster(self, points: Sequence[GeoPoint], name: str) -> list[list[int]]:
        """Group point ids into clusters, each in input order. Clusters are non-empty and may
        differ in size. There are min(k, len(points)) of them; `name` labels the warning
        printed when k is reduced."""
        if not points:
            raise ValueError(f"{name}: no points to cluster")
        _reject_duplicates(points, name)
        k = self.k
        if k > len(points):
            print(
                f"WARNING: {name}: {k} clusters requested but only {len(points)} grid cells; "
                f"using {len(points)} clusters",
                file=sys.stderr,
            )
            k = len(points)
        labels = KMeans(n_clusters=k, n_init=self.n_init, random_state=self.seed).fit_predict(
            project(points)
        )
        clusters: list[list[int]] = [[] for _ in range(k)]
        for point, label in zip(points, labels, strict=True):
            clusters[int(label)].append(point.id)
        if not all(clusters):
            # Distinct points and k <= len(points) should never leave a cluster empty.
            raise AssertionError(f"{name}: K-means produced an empty cluster")
        return clusters


def project(points: Sequence[GeoPoint]) -> np.ndarray:
    """Equirectangular (x, y) in degrees of latitude, centred on the points' mean latitude."""
    mean_latitude = sum(p.latitude for p in points) / len(points)
    scale = math.cos(math.radians(mean_latitude))
    return np.array([(p.longitude * scale, p.latitude) for p in points], dtype=float)


def _reject_duplicates(points: Sequence[GeoPoint], name: str) -> None:
    ids = [p.id for p in points]
    if len(set(ids)) != len(ids):
        raise DataError(f"{name}: duplicate grid cells")
    locations = [(p.latitude, p.longitude) for p in points]
    if len(set(locations)) != len(locations):
        raise DataError(f"{name}: two grid cells share a location")
