"""Run folders: creation, the config snapshot, and tracking which steps have completed."""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import RunStateError

CONFIG_NAME = "config.toml"
MANIFEST_NAME = "run.json"

RUN_STEPS = ("select", "preprocess", "dispatch")


@dataclass(frozen=True)
class RunContext:
    run_dir: Path
    config: Config

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
    scenario_file: Path, output_dir: Path = paths.OUTPUT_DIR, started: datetime | None = None
) -> RunContext:
    Config.load(scenario_file)  # fail on a bad scenario before creating the run folder
    started = started or datetime.now()
    run_dir = output_dir / paths.run_dir_name(scenario_file.stem, started)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise RunStateError(f"Run folder already exists: {run_dir}") from None
    shutil.copyfile(scenario_file, run_dir / CONFIG_NAME)
    ctx = RunContext(run_dir, Config.load(run_dir / CONFIG_NAME))
    ctx._write_manifest(
        {
            "scenario": scenario_file.stem,
            "started": started.isoformat(timespec="seconds"),
            "git": _git_info(),
            "completed_steps": [],
        }
    )
    return ctx


def open_run(run_dir: Path) -> RunContext:
    for name in (CONFIG_NAME, MANIFEST_NAME):
        if not (run_dir / name).is_file():
            raise RunStateError(f"Not a run folder (missing {name}): {run_dir}")
    return RunContext(run_dir, Config.load(run_dir / CONFIG_NAME))


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
