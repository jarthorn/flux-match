"""CSV reading and writing with validation of required columns."""

import csv
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path

from flux_match.errors import DataError


def read_csv(path: Path, required_columns: Sequence[str]) -> list[dict[str, str]]:
    return list(iter_csv(path, required_columns))


def iter_csv(path: Path, required_columns: Sequence[str]) -> Iterator[dict[str, str]]:
    """Like read_csv, but yields one row at a time, for files too large to hold in memory."""
    if not path.is_file():
        raise DataError(f"Input file not found: {path}")
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required_columns if c not in (reader.fieldnames or ())]
        if missing:
            raise DataError(f"{path}: missing required columns {missing}")
        yield from reader


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def write_csv_atomic(
    path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]
) -> None:
    """Like write_csv, but `path` is only replaced once the whole file is written."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        write_csv(tmp, columns, rows)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
