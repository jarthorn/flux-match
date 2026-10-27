"""CSV reading and writing with validation of required columns."""

import csv
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from flux_match.errors import DataError


def read_csv(path: Path, required_columns: Sequence[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise DataError(f"Input file not found: {path}")
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required_columns if c not in (reader.fieldnames or ())]
        if missing:
            raise DataError(f"{path}: missing required columns {missing}")
        return list(reader)


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
