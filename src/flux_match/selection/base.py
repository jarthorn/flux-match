"""Interface for wind and solar site-selection strategies."""

from abc import ABC, abstractmethod

from flux_match.config import Config
from flux_match.run_context import RunContext


class Strategy(ABC):
    """Parameters come from the scenario's [selection.<name>] table. Read and validate them
    all in __init__ so that config errors surface before any work starts."""

    @abstractmethod
    def __init__(self, params: Config) -> None: ...

    @abstractmethod
    def select(self, ctx: RunContext) -> None:
        """Write the selected sites to ctx.selected_sites_path."""
