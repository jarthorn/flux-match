"""Greedy strategy: pick the sites with the highest annual output, capped per grid cell."""

from flux_match.config import Config
from flux_match.errors import ConfigError
from flux_match.run_context import RunContext
from flux_match.selection.base import Strategy


class GreedyStrategy(Strategy):
    def __init__(self, params: Config) -> None:
        self.max_capacity_mw_per_cell = params.get("max_capacity_mw_per_cell", float)
        if self.max_capacity_mw_per_cell <= 0:
            raise ConfigError(
                f"selection.greedy.max_capacity_mw_per_cell must be positive: "
                f"{self.max_capacity_mw_per_cell}"
            )

    def select(self, ctx: RunContext) -> None:
        raise NotImplementedError("greedy strategy is not implemented yet")
