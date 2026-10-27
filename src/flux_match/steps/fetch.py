"""Step 1: download raw input data from CODERS into data/coders/.

Skipped for any file already present. Writes to the shared data folder rather than a run
folder, so downloads are reused across runs.
"""

from pathlib import Path

from flux_match import paths
from flux_match.config import Config


def run(config: Config, data_dir: Path = paths.CODERS_DATA_DIR) -> None:
    raise NotImplementedError("fetch step is not implemented yet")
