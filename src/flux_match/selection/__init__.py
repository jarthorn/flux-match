"""Step 2: select wind and solar sites with the strategy named in the [selection] table."""

from flux_match.errors import ConfigError
from flux_match.run_context import RunContext
from flux_match.selection.base import Strategy, write_sites
from flux_match.selection.cluster import ClusterStrategy
from flux_match.selection.greedy import GreedyStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    "cluster": ClusterStrategy,
    "greedy": GreedyStrategy,
}


def run(ctx: RunContext) -> None:
    params = ctx.config.section("selection")
    name = params.get("strategy", str)
    if name not in STRATEGIES:
        raise ConfigError(
            f"Unknown selection strategy '{name}'. Available: {', '.join(sorted(STRATEGIES))}"
        )
    params.section(name)  # [selection.<strategy>] must exist, even if it has no keys
    sites = STRATEGIES[name](params).select(ctx)
    write_sites(ctx.selected_sites_path, sites)
