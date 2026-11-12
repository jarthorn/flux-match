"""Step 2: select wind and solar sites with the strategy named in the [selection] table."""

from flux_match import paths
from flux_match.errors import ConfigError
from flux_match.run_context import RunContext
from flux_match.steps.select.base import Strategy, write_sites
from flux_match.steps.select.cluster import ClusterStrategy
from flux_match.steps.select.greedy import GreedyStrategy
from flux_match.steps.select.tvci import TvciStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    "cluster": ClusterStrategy,
    "greedy": GreedyStrategy,
    "tvci": TvciStrategy,
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
    write_sites(paths.selected_sites_path(ctx), sites)
