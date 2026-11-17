"""Collate the config and dispatch statistics of every run in a batch, for offline analysis.

    uv run python -m flux_match.viz.batch_summary --batch NAME

Reads every subfolder of output/NAME (one per batch run) and writes these files to that folder:
    summary.csv       one row per run, sorted by batch_index: batch_index, started, every
                      config.toml value that differs between runs (or is missing from some),
                      then summary dispatch statistics from run.json
    invariants.toml   the config.toml values that are identical in every run
    net_demand.csv, curtailment.csv, sat_demand_hours.csv
                      one row per run: batch_index, started, each balancing area (sorted),
                      then total; values from the matching dispatch_stats percentage
Each config.toml value is in exactly one of summary.csv and invariants.toml. Config columns
are dotted keys (`selection.wind_capacity_ratio`); arrays and booleans are written as TOML
literals. Any incomplete or malformed run fails the whole summary, after reporting every
bad run.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux_match import paths
from flux_match.config import dump_toml, dump_value, read_toml
from flux_match.errors import DataError, FluxMatchError, RunStateError, report_data_error
from flux_match.io import write_csv_atomic
from flux_match.run_context import check_batch_name
from flux_match.steps.dispatch.dispatch import STATS_TOTAL_KEY

SUMMARY_FILENAME = "summary.csv"
INVARIANTS_FILENAME = "invariants.toml"

# (dispatch_stats key, output filename, treat null as 0.0)
AREA_STAT_FILES: tuple[tuple[str, str, bool], ...] = (
    ("net_demand_pct", "net_demand.csv", False),
    ("curtailment_pct", "curtailment.csv", True),
    ("satisfied_demand_hours_pct", "sat_demand_hours.csv", False),
)

RUN_COLUMNS = ("batch_index", "started")
STAT_COLUMNS = (
    "net_demand_pct",
    "curtailment_pct",
    "satisfied_demand_hours_pct",
    "peak_net_demand_mw",
    "peak_net_demand_ba",
)


@dataclass(frozen=True)
class BatchRun:
    batch_index: int
    started: str
    config: dict[str, Any]
    """config.toml flattened to dotted keys."""
    stats: dict[str, object]
    """STAT_COLUMNS values."""
    area_stats: dict[str, dict[str, float]]
    """Per-metric balancing-area values (including total); null curtailment is 0.0."""


def flatten_config(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested TOML tables to `{"table.key": value}`, in file order."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if "." in key:
            raise DataError(f"Config key {key!r} contains '.', so cannot be written as a column")
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten_config(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def unflatten_config(flat: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for dotted, value in flat.items():
        *parents, leaf = dotted.split(".")
        table = nested
        for part in parents:
            table = table.setdefault(part, {})
        table[leaf] = value
    return nested


def read_run(run_dir: Path) -> BatchRun:
    """One batch run folder; raises DataError (naming the folder) if it is incomplete."""
    try:
        config = flatten_config(read_toml(paths.config_path(run_dir)))
        for key, value in config.items():
            try:
                dump_value(value)
            except TypeError as e:
                raise DataError(f"config.toml '{key}': {e}") from None
        manifest = _read_manifest(run_dir)
        batch_index = manifest.get("batch_index")
        if type(batch_index) is not int or paths.batch_run_dir_name(batch_index) != run_dir.name:
            raise DataError(f"batch_index {batch_index!r} does not match the folder name")
        started = manifest.get("started")
        if not isinstance(started, str):
            raise DataError(f"'started' must be a string, got {started!r}")
        if "dispatch" not in manifest.get("completed_steps", []):
            raise DataError("the dispatch step has not completed")
        stats = manifest.get("dispatch_stats")
        if not isinstance(stats, dict):
            raise DataError("run.json has no dispatch_stats")
        area_stats = {
            name: _stat_by_area(stats, name, allow_null=allow_null)
            for name, _, allow_null in AREA_STAT_FILES
        }
        return BatchRun(batch_index, started, config, summary_stats(stats), area_stats)
    except FluxMatchError as e:
        raise DataError(f"{run_dir}: {e}") from None


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    path = paths.manifest_path(run_dir)
    if not path.is_file():
        raise DataError(f"missing {paths.MANIFEST_NAME}")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise DataError(f"invalid {paths.MANIFEST_NAME}: {e}") from None
    if not isinstance(manifest, dict):
        raise DataError(f"{paths.MANIFEST_NAME} must be a JSON object")
    return manifest


def summary_stats(stats: dict[str, Any]) -> dict[str, object]:
    """The STAT_COLUMNS values from a run's dispatch_stats."""
    peaks = stats.get("peak_net_demand_mw")
    if not isinstance(peaks, dict) or not peaks:
        raise DataError("dispatch_stats.peak_net_demand_mw is missing or empty")
    for ba, mw in peaks.items():
        _check_number(f"dispatch_stats.peak_net_demand_mw.{ba}", mw)
    # max() keeps the first of tied areas, in run.json order.
    peak_ba = max(peaks, key=peaks.__getitem__)
    return {
        "net_demand_pct": _stat_by_area(stats, "net_demand_pct")[STATS_TOTAL_KEY],
        "curtailment_pct": _stat_by_area(stats, "curtailment_pct", allow_null=True)[
            STATS_TOTAL_KEY
        ],
        "satisfied_demand_hours_pct": _stat_by_area(stats, "satisfied_demand_hours_pct")[
            STATS_TOTAL_KEY
        ],
        "peak_net_demand_mw": peaks[peak_ba],
        "peak_net_demand_ba": peak_ba,
    }


def _stat_by_area(stats: dict[str, Any], name: str, allow_null: bool = False) -> dict[str, float]:
    """Balancing-area values for one dispatch_stats metric, including total.

    Null values become 0.0 when allow_null is true (curtailment with no variable generation).
    """
    by_area = stats.get(name)
    if not isinstance(by_area, dict) or STATS_TOTAL_KEY not in by_area:
        raise DataError(f"dispatch_stats.{name}.{STATS_TOTAL_KEY} is missing")
    result: dict[str, float] = {}
    for ba, value in by_area.items():
        label = f"dispatch_stats.{name}.{ba}"
        if value is None and allow_null:
            result[ba] = 0.0
        else:
            result[ba] = _check_number(label, value)
    return result


def _check_number(label: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"{label} must be a number, got {value!r}")
    return value


def read_batch(batch_dir: Path) -> list[BatchRun]:
    """Every run in the batch, sorted by batch_index. Reports each bad run before failing."""
    run_dirs = sorted(path for path in batch_dir.iterdir() if path.is_dir())
    if not run_dirs:
        raise DataError(f"No run folders in {batch_dir}")
    runs: list[BatchRun] = []
    errors = 0
    for run_dir in run_dirs:
        try:
            runs.append(read_run(run_dir))
        except DataError as e:
            report_data_error(str(e))
            errors += 1
    if errors:
        raise DataError(f"{errors} of {len(run_dirs)} run folders in {batch_dir} are invalid")
    return sorted(runs, key=lambda run: run.batch_index)


def split_config(runs: Sequence[BatchRun]) -> tuple[dict[str, Any], list[str]]:
    """(values identical in every run, keys of the values that differ or are sometimes missing).

    `4` and `4.0` are identical (the invariant keeps the first run's value), but `1` and `true`
    are not.
    """
    keys = dict.fromkeys(key for run in runs for key in run.config)
    invariants: dict[str, Any] = {}
    varying: list[str] = []
    for key in keys:
        values = {_comparable(run.config[key]) if key in run.config else None for run in runs}
        if len(values) == 1 and None not in values:
            invariants[key] = runs[0].config[key]
        else:
            varying.append(key)
    return invariants, varying


def _comparable(value: Any) -> object:
    """A hashable form of a TOML value in which equal numbers match, but bools and numbers
    (equal in Python, `True == 1`) and strings and numbers do not."""
    if isinstance(value, list):
        return ("list", tuple(_comparable(v) for v in value))
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    return (type(value).__name__, value)


def summary_columns(varying: Sequence[str]) -> list[str]:
    fixed = (*RUN_COLUMNS, *STAT_COLUMNS)
    clashes = sorted(set(varying) & set(fixed))
    if clashes:
        raise DataError(f"Config keys clash with summary.csv columns: {clashes}")
    return [*RUN_COLUMNS, *varying, *STAT_COLUMNS]


def summary_rows(runs: Sequence[BatchRun], varying: Sequence[str]) -> list[dict[str, object]]:
    return [
        {
            "batch_index": run.batch_index,
            "started": run.started,
            **{key: _csv_value(run.config.get(key)) for key in varying},
            **run.stats,
        }
        for run in runs
    ]


def _csv_value(value: Any) -> object:
    """Strings as-is, missing values blank, anything else as a TOML literal."""
    if value is None or isinstance(value, str):
        return value
    return dump_value(value)


def balancing_areas(runs: Sequence[BatchRun], metric: str) -> list[str]:
    """Sorted BA names for a metric; every run must have every area (and total already checked)."""
    expected = sorted(
        {ba for run in runs for ba in run.area_stats[metric] if ba != STATS_TOTAL_KEY}
    )
    if not expected:
        raise DataError(f"dispatch_stats.{metric} has no balancing areas")
    errors = 0
    for run in runs:
        present = {ba for ba in run.area_stats[metric] if ba != STATS_TOTAL_KEY}
        missing = [ba for ba in expected if ba not in present]
        if missing:
            report_data_error(
                f"{paths.batch_run_dir_name(run.batch_index)}: "
                f"dispatch_stats.{metric} missing balancing areas: {missing}"
            )
            errors += 1
    if errors:
        raise DataError(
            f"{errors} of {len(runs)} runs are missing balancing areas in dispatch_stats.{metric}"
        )
    return expected


def area_stat_columns(areas: Sequence[str]) -> list[str]:
    return [*RUN_COLUMNS, *areas, STATS_TOTAL_KEY]


def area_stat_rows(
    runs: Sequence[BatchRun], metric: str, areas: Sequence[str]
) -> list[dict[str, object]]:
    return [
        {
            "batch_index": run.batch_index,
            "started": run.started,
            **{ba: run.area_stats[metric][ba] for ba in areas},
            STATS_TOTAL_KEY: run.area_stats[metric][STATS_TOTAL_KEY],
        }
        for run in runs
    ]


def write_area_stat_csvs(
    batch_dir: Path, runs: Sequence[BatchRun], areas_by_metric: dict[str, list[str]]
) -> None:
    """Write the three per-balancing-area CSVs using already-validated BA lists."""
    for metric, filename, _ in AREA_STAT_FILES:
        areas = areas_by_metric[metric]
        write_csv_atomic(
            batch_dir / filename, area_stat_columns(areas), area_stat_rows(runs, metric, areas)
        )


def write_batch_summary(batch_dir: Path) -> tuple[int, dict[str, Any], list[str]]:
    """Write summary.csv, invariants.toml, and the three area CSVs. Returns
    (runs, invariants, varying)."""
    runs = read_batch(batch_dir)
    invariants, varying = split_config(runs)
    # Validate BA sets before writing any outputs.
    areas_by_metric = {metric: balancing_areas(runs, metric) for metric, _, _ in AREA_STAT_FILES}
    write_csv_atomic(
        batch_dir / SUMMARY_FILENAME, summary_columns(varying), summary_rows(runs, varying)
    )
    (batch_dir / INVARIANTS_FILENAME).write_text(dump_toml(unflatten_config(invariants)))
    write_area_stat_csvs(batch_dir, runs, areas_by_metric)
    return len(runs), invariants, varying


def batch_dir_for(name: str, output_dir: Path = paths.OUTPUT_DIR) -> Path:
    check_batch_name(output_dir, name)
    batch_dir = output_dir / name
    if not batch_dir.is_dir():
        raise RunStateError(f"Batch folder not found: {batch_dir}")
    return batch_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--batch", required=True, help="batch name, a folder in output/")
    args = parser.parse_args(argv)
    try:
        batch_dir = batch_dir_for(args.batch)
        n_runs, invariants, varying = write_batch_summary(batch_dir)
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(
        f"Wrote {batch_dir / SUMMARY_FILENAME} ({n_runs} runs, {len(varying)} "
        "varying config values)"
    )
    print(f"Wrote {batch_dir / INVARIANTS_FILENAME} ({len(invariants)} invariant config values)")
    names = ", ".join(filename for _, filename, _ in AREA_STAT_FILES)
    print(f"Wrote {names} (per-balancing-area stats)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
