"""Exception types and data-error reporting."""

import sys


class FluxMatchError(Exception):
    """Base class for errors that stop a run with a message instead of a traceback."""


class ConfigError(FluxMatchError):
    """The scenario configuration is missing or invalid."""


class DataError(FluxMatchError):
    """Input data is missing or malformed."""


class RunStateError(FluxMatchError):
    """A run folder is missing or already exists, or a prerequisite step has not completed."""


def report_data_error(message: str) -> None:
    """Flag a data problem on stderr without stopping, e.g. to collect several before failing."""
    print(f"DATA ERROR: {message}", file=sys.stderr)
