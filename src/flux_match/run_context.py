"""Run folders: creation, the config snapshot, and tracking which steps have completed."""

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, RunStateError

CONFIG_NAME = "config.toml"
MANIFEST_NAME = "run.json"

RUN_STEPS = ("select", "preprocess", "dispatch")

MAX_BATCH_INDEX = 9999
_BATCH_INDEX_PATTERN = re.compile(r"[0-9]{4}")


@dataclass(frozen=True)
class RunContext:
    """A run folder and its config.

    In batch mode, a run leaves only config.toml, run.json and selected_sites.csv: steps skip
    optional outputs (clusters.csv, dispatch_hourly_<year>.csv), and the intermediate
    vre_by_ba_hourly.csv is removed by `remove_intermediate_files` once the run is over.
    """

    run_dir: Path
    config: Config
    batch: bool = False

    @property
    def selected_sites_path(self) -> Path:
        return self.run_dir / "selected_sites.csv"

    @property
    def clusters_path(self) -> Path:
        return self.run_dir / "clusters.csv"

    @property
    def vre_hourly_path(self) -> Path:
        return self.run_dir / "vre_by_ba_hourly.csv"

    def dispatch_hourly_path(self, year: int) -> Path:
        return self.run_dir / f"dispatch_hourly_{year}.csv"

    def remove_intermediate_files(self) -> None:
        """Delete files that later steps need but that are not results (batch mode cleanup)."""
        self.vre_hourly_path.unlink(missing_ok=True)

    def require_complete(self, step: str) -> None:
        if step not in self._read_manifest()["completed_steps"]:
            raise RunStateError(f"Step '{step}' has not completed in {self.run_dir}")

    def begin_step(self, step: str) -> None:
        """Check the prerequisite step, and un-mark this and later steps, whose outputs
        are about to be overwritten or become stale."""
        index = RUN_STEPS.index(step)
        if index > 0:
            self.require_complete(RUN_STEPS[index - 1])
        manifest = self._read_manifest()
        manifest["completed_steps"] = [
            s for s in manifest["completed_steps"] if RUN_STEPS.index(s) < index
        ]
        for stale in RUN_STEPS[index:]:
            manifest.pop(_stats_key(stale), None)
        self._write_manifest(manifest)

    def complete_step(self, step: str) -> None:
        manifest = self._read_manifest()
        manifest["completed_steps"].append(step)
        self._write_manifest(manifest)

    def record_stats(self, step: str, stats: dict[str, Any]) -> None:
        """Store summary statistics for `step` in the manifest, as "<step>_stats"."""
        if step not in RUN_STEPS:
            raise ValueError(f"Unknown run step: {step}")
        manifest = self._read_manifest()
        manifest[_stats_key(step)] = stats
        self._write_manifest(manifest)

    def read_stats(self, step: str) -> dict[str, Any] | None:
        return self._read_manifest().get(_stats_key(step))

    def _read_manifest(self) -> dict[str, Any]:
        return json.loads((self.run_dir / MANIFEST_NAME).read_text())

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        (self.run_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")


def _stats_key(step: str) -> str:
    return f"{step}_stats"


def create_run(
    scenario_file: Path,
    output_dir: Path = paths.OUTPUT_DIR,
    started: datetime | None = None,
    *,
    batch: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> RunContext:
    """Create a new run folder: output_dir/YYYYMMDD-HHMM-<scenario>, or for a batch run,
    output_dir/<batch>/NNNN, numbered one past the batch's highest existing index."""
    # Validate everything before creating the folder so a bad input leaves no residue.
    if batch is not None:
        _check_batch_name(output_dir, batch)
    config = Config.load(scenario_file, overrides=overrides)
    started = started or datetime.now()
    if batch is None:
        run_dir, batch_index = _allocate_run_dir(output_dir, scenario_file.stem, started), None
    else:
        run_dir, batch_index = _allocate_batch_run_dir(output_dir / batch)
    # Snapshot the resolved config so re-runs do not pick up later changes to defaults.toml.
    (run_dir / CONFIG_NAME).write_text(config.to_toml())
    ctx = RunContext(
        run_dir,
        Config.load(run_dir / CONFIG_NAME, inherit_defaults=False),
        batch=batch is not None,
    )
    ctx._write_manifest(
        {
            "scenario": scenario_file.stem,
            "batch_name": batch,
            "batch_index": batch_index,
            "config_overrides": overrides or {},
            "started": started.isoformat(timespec="seconds"),
            "git": _git_info(),
            "completed_steps": [],
        }
    )
    return ctx


def _check_batch_name(output_dir: Path, name: str) -> None:
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise ConfigError(f"Batch name must be a plain folder name: {name!r}")
    if (output_dir / name / CONFIG_NAME).is_file():
        raise RunStateError(f"Batch name is an existing run folder: {output_dir / name}")


def _allocate_run_dir(output_dir: Path, scenario_name: str, started: datetime) -> Path:
    run_dir = output_dir / paths.run_dir_name(scenario_name, started)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise RunStateError(f"Run folder already exists: {run_dir}") from None
    return run_dir


def _allocate_batch_run_dir(batch_dir: Path) -> tuple[Path, int]:
    batch_dir.mkdir(parents=True, exist_ok=True)
    existing = [
        int(path.name)
        for path in batch_dir.iterdir()
        if path.is_dir() and _BATCH_INDEX_PATTERN.fullmatch(path.name)
    ]
    for batch_index in range(max(existing, default=0) + 1, MAX_BATCH_INDEX + 1):
        run_dir = batch_dir / paths.batch_run_dir_name(batch_index)
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue  # claimed by a concurrent run in the same batch
        return run_dir, batch_index
    raise RunStateError(f"No free batch index in {batch_dir} (maximum {MAX_BATCH_INDEX})")


def open_run(run_dir: Path) -> RunContext:
    for name in (CONFIG_NAME, MANIFEST_NAME):
        if not (run_dir / name).is_file():
            raise RunStateError(f"Not a run folder (missing {name}): {run_dir}")
    return RunContext(run_dir, Config.load(run_dir / CONFIG_NAME, inherit_defaults=False))


def latest_run_dir(output_dir: Path = paths.OUTPUT_DIR) -> Path:
    """The run folder whose name sorts last (YYYYMMDD-HHMM prefix, so that is the newest)."""
    if not output_dir.is_dir():
        raise RunStateError(f"Output folder not found: {output_dir}")
    runs = [
        path
        for path in output_dir.iterdir()
        if path.is_dir() and (path / CONFIG_NAME).is_file() and (path / MANIFEST_NAME).is_file()
    ]
    if not runs:
        raise RunStateError(f"No run folders found in {output_dir}")
    return max(runs, key=lambda path: path.name)


def run_dir_timestamp(run_dir: Path) -> str:
    """The YYYYMMDD-HHMM prefix of a run folder name, or the whole name if it does not match."""
    prefix = run_dir.name[:13]
    try:
        datetime.strptime(prefix, paths.TIMESTAMP_FORMAT)
    except ValueError:
        return run_dir.name
    return prefix


def _git_info() -> dict[str, str | bool] | None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=paths.PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"WARNING: could not record git commit for this run: {e}", file=sys.stderr)
        return None
