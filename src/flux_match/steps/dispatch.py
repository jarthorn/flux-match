"""Step 4: hourly battery dispatch and net demand for each balancing area.

Battery losses are applied entirely on charge: charging with P MW for one hour adds
P * round_trip_efficiency MWh to storage, and discharge is lossless.
"""

from dataclasses import dataclass
from typing import Self

from flux_match.config import Config
from flux_match.errors import ConfigError
from flux_match.run_context import RunContext


@dataclass(frozen=True)
class DispatchParams:
    dispatch_years: tuple[int, ...]
    initial_state_of_charge_fraction: float
    round_trip_efficiency: float

    @classmethod
    def from_config(cls, params: Config) -> Self:
        return cls(
            dispatch_years=tuple(params.get_list("dispatch_years", int, unique=True)),
            initial_state_of_charge_fraction=_fraction(
                params, "initial_state_of_charge_fraction", allow_zero=True
            ),
            round_trip_efficiency=_fraction(params, "round_trip_efficiency", allow_zero=False),
        )


def run(ctx: RunContext) -> None:
    DispatchParams.from_config(ctx.config.section("dispatch"))
    raise NotImplementedError("dispatch step is not implemented yet")


def _fraction(params: Config, key: str, allow_zero: bool) -> float:
    value = params.get(key, float)
    low_ok = value >= 0.0 if allow_zero else value > 0.0
    if not (low_ok and value <= 1.0):
        bounds = "[0, 1]" if allow_zero else "(0, 1]"
        raise ConfigError(f"dispatch.{key} must be in {bounds}: {value}")
    return value
