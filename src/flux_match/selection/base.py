"""Interface for wind and solar site-selection strategies, and the selected-sites file format.

Every strategy returns a list of Site records; the step writes them with write_sites, so all
strategies produce the same file. The file is a sparse CSV with one row per grid cell and
generation type that has capacity:

    grid_cell,generation_type,capacity_mw
    1,solar,250.0
    1,wind_ons,1000.0

grid_cell is a grid_cell id from data/coders/grid_cell_info.csv. generation_type is one of
the GenerationType values. capacity_mw is positive and finite. A (grid_cell, generation_type)
pair appears at most once. Rows are sorted by grid_cell, then generation_type.
"""

import math
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from pathlib import Path

from flux_match.config import Config
from flux_match.errors import DataError
from flux_match.io import read_csv, write_csv
from flux_match.run_context import RunContext


class GenerationType(StrEnum):
    WIND_ONSHORE = "wind_ons"
    WIND_OFFSHORE = "wind_ofs"
    SOLAR = "solar"


@dataclass(frozen=True, order=True)
class Site:
    grid_cell: int
    generation_type: GenerationType
    capacity_mw: float

    def __post_init__(self) -> None:
        if not isinstance(self.generation_type, GenerationType):
            raise ValueError(f"generation_type must be a GenerationType: {self.generation_type!r}")
        if not math.isfinite(self.capacity_mw) or self.capacity_mw <= 0:
            raise ValueError(
                f"capacity_mw must be positive and finite for grid cell {self.grid_cell} "
                f"{self.generation_type}: {self.capacity_mw}"
            )


SITE_COLUMNS = tuple(f.name for f in fields(Site))


def _reject_duplicates(sites: list[Site], source: object) -> None:
    counts = Counter((s.grid_cell, s.generation_type) for s in sites)
    duplicates = sorted(k for k, n in counts.items() if n > 1)
    if duplicates:
        listed = ", ".join(f"grid cell {cell} {kind}" for cell, kind in duplicates)
        raise DataError(f"{source}: duplicate selected sites: {listed}")


def write_sites(path: Path, sites: Iterable[Site]) -> None:
    ordered = sorted(sites)
    _reject_duplicates(ordered, path)
    write_csv(path, SITE_COLUMNS, (asdict(s) for s in ordered))


def read_sites(path: Path) -> list[Site]:
    sites = []
    # Line 1 is the header.
    for line, row in enumerate(read_csv(path, SITE_COLUMNS), start=2):
        try:
            sites.append(
                Site(
                    grid_cell=int(row["grid_cell"]),
                    generation_type=GenerationType(row["generation_type"]),
                    capacity_mw=float(row["capacity_mw"]),
                )
            )
        except ValueError as e:
            raise DataError(f"{path}:{line}: {e}") from e
    _reject_duplicates(sites, path)
    return sites


class Strategy(ABC):
    """Parameters come from the scenario's [selection.<name>] table. Read and validate them
    all in __init__ so that config errors surface before any work starts."""

    @abstractmethod
    def __init__(self, params: Config) -> None: ...

    @abstractmethod
    def select(self, ctx: RunContext) -> list[Site]:
        """Return the selected sites. Omit cells and generation types with no capacity."""
