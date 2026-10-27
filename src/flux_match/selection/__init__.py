"""Step 2: select wind and solar sites with the strategy named in the [selection] table."""

from flux_match.errors import ConfigError
from flux_match.run_context import RunContext
from flux_match.selection.base import Strategy
from flux_match.selection.greedy import GreedyStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    "greedy": GreedyStrategy,
}


def run(ctx: RunContext) -> None:
    params = ctx.config.section("selection")
    name = params.get("strategy", str)
    if name not in STRATEGIES:
        raise ConfigError(
            f"Unknown selection strategy '{name}'. Available: {', '.join(sorted(STRATEGIES))}"
        )
    STRATEGIES[name](params.section(name)).select(ctx)
