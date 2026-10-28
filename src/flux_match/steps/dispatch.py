"""Step 4: hourly battery dispatch and net demand for each balancing area.

For each dispatch year, streams data/balancing_area_demand_hourly_<year>.csv (from fetch) and the
run's vre_by_ba_hourly.csv (from preprocess) in lockstep. Both are sorted by balancing area, then
hour. Each balancing area in a file must have hours 1-8760 in order, and the two files must have
the same balancing area and hour on every row; anything else is a hard failure. The single
capacity-factor year in vre_by_ba_hourly.csv is reused for every dispatch year.

Each balancing area has one battery, sized from the newest year in balancing_area_demand.csv:

    capacity_mwh = bess_capacity_ratio * average daily demand (annual_demand_mwh / 365)
    power_mw = capacity_mwh / bess_duration

Every balancing area and year starts at bess_initial_soc * capacity_mwh; state of charge is not
carried between years. Each hour, a surplus of variable generation over demand charges the battery
and a shortfall discharges it, both limited by power_mw. Battery losses are applied entirely on
charge: charging with P MW for one hour adds P * bess_rte MWh to storage, and discharge is
lossless. Surplus that cannot be stored shows up as negative net demand.

Writes dispatch_hourly_<year>.csv with DISPATCH_COLUMNS, where battery output is positive for
discharge and negative for charge, and net demand = demand - variable generation - battery output.
"""

import itertools
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Self

from flux_match import paths
from flux_match.balancing_area_demand import read_ba_demand
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.grid_cell_rankings import HOURS_PER_DAY
from flux_match.io import iter_csv, write_csv_atomic
from flux_match.run_context import RunContext
from flux_match.schema import DISPATCH_COLUMNS, HOURS_PER_YEAR


@dataclass(frozen=True)
class DispatchParams:
    dispatch_years: tuple[int, ...]
    bess_initial_soc: float
    bess_rte: float
    bess_capacity_ratio: float
    bess_duration: float

    @classmethod
    def from_config(cls, params: Config) -> Self:
        return cls(
            dispatch_years=tuple(params.get_list("dispatch_years", int, unique=True)),
            bess_initial_soc=_bounded(params, "bess_initial_soc", allow_zero=True, maximum=1.0),
            bess_rte=_bounded(params, "bess_rte", allow_zero=False, maximum=1.0),
            bess_capacity_ratio=_bounded(params, "bess_capacity_ratio", allow_zero=True),
            bess_duration=_bounded(params, "bess_duration", allow_zero=False),
        )


@dataclass(frozen=True)
class Battery:
    capacity_mwh: float
    power_mw: float


class HourlyValue(NamedTuple):
    balancing_area: str
    hour: int
    value: float


class PairedHour(NamedTuple):
    balancing_area: str
    hour: int
    demand_mw: float
    variable_generation_mw: float


def run(
    ctx: RunContext,
    data_dir: Path = paths.DATA_DIR,
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> None:
    params = DispatchParams.from_config(ctx.config.section("dispatch"))
    _, annual_demand = read_ba_demand(data_dir / paths.BA_DEMAND_FILENAME)
    batteries = size_batteries(annual_demand, params.bess_capacity_ratio, params.bess_duration)
    for year in params.dispatch_years:
        demand_path = data_dir / paths.ba_demand_hourly_filename(year)
        if not demand_path.is_file():
            raise DataError(
                f"Hourly demand for {year} not found: {demand_path}. "
                f"Add {year} to fetch.demand_years and run fetch."
            )
        dest = ctx.dispatch_hourly_path(year)
        print(f"writing {dest.name}", flush=True)
        hours = paired_hours(demand_path, ctx.vre_hourly_path, hours_in_year=hours_in_year)
        write_csv_atomic(dest, DISPATCH_COLUMNS, dispatch_rows(year, hours, batteries, params))


def size_batteries(
    annual_demand_mwh: Mapping[str, float], capacity_ratio: float, duration_h: float
) -> dict[str, Battery]:
    """One battery per balancing area, holding `capacity_ratio` days of average demand."""
    batteries = {}
    for ba, demand in annual_demand_mwh.items():
        if demand < 0:
            raise DataError(f"{ba}: annual demand is negative: {demand}")
        capacity = capacity_ratio * demand / HOURS_PER_YEAR * HOURS_PER_DAY
        batteries[ba] = Battery(capacity_mwh=capacity, power_mw=capacity / duration_h)
    return batteries


def dispatch_hour(
    shortfall_mw: float, soc_mwh: float, battery: Battery, rte: float
) -> tuple[float, float]:
    """Battery output (positive = discharge) and the new state of charge after one hour.

    `shortfall_mw` is demand minus variable generation; negative means surplus to store.
    """
    if shortfall_mw > 0:
        output = min(shortfall_mw, battery.power_mw, soc_mwh)
        return output, soc_mwh - output
    drawn = min(-shortfall_mw, battery.power_mw, (battery.capacity_mwh - soc_mwh) / rte)
    # 0.0 - x rather than -x, so an idle battery writes 0.0 rather than -0.0.
    return 0.0 - drawn, min(battery.capacity_mwh, soc_mwh + drawn * rte)


def dispatch_rows(
    year: int,
    hours: Iterable[PairedHour],
    batteries: Mapping[str, Battery],
    params: DispatchParams,
) -> Iterator[dict[str, object]]:
    """DISPATCH_COLUMNS rows, resetting the battery at the start of each balancing area."""
    for ba, ba_hours in itertools.groupby(hours, key=lambda h: h.balancing_area):
        battery = batteries.get(ba)
        if battery is None:
            raise DataError(f"{ba}: no annual demand in {paths.BA_DEMAND_FILENAME}")
        soc = params.bess_initial_soc * battery.capacity_mwh
        for h in ba_hours:
            shortfall = h.demand_mw - h.variable_generation_mw
            output, soc = dispatch_hour(shortfall, soc, battery, params.bess_rte)
            yield {
                "balancing_area": ba,
                "year": year,
                "hour": h.hour,
                "variable_generation_mw": h.variable_generation_mw,
                "battery_output_mw": output,
                "net_demand_mw": shortfall - output,
            }


def paired_hours(
    demand_path: Path, vre_path: Path, *, hours_in_year: int = HOURS_PER_YEAR
) -> Iterator[PairedHour]:
    """Stream both hourly files together, failing on the first row where they disagree."""
    demand = iter_hourly_values(demand_path, "demand_mw", hours_in_year=hours_in_year)
    vre = iter_hourly_values(vre_path, "variable_generation_mw", hours_in_year=hours_in_year)
    for row, (d, v) in enumerate(itertools.zip_longest(demand, vre), start=2):
        if d is None or v is None:
            ended = demand_path if d is None else vre_path
            raise DataError(
                f"{demand_path.name} and {vre_path.name} have different balancing areas: "
                f"{ended.name} ends before row {row}"
            )
        if (d.balancing_area, d.hour) != (v.balancing_area, v.hour):
            raise DataError(
                f"row {row}: {demand_path.name} has {d.balancing_area} hour {d.hour}, "
                f"but {vre_path.name} has {v.balancing_area} hour {v.hour}"
            )
        yield PairedHour(d.balancing_area, d.hour, d.value, v.value)


def iter_hourly_values(
    path: Path, column: str, *, hours_in_year: int = HOURS_PER_YEAR
) -> Iterator[HourlyValue]:
    """Stream `column` from a file sorted by balancing area, then hour.

    Each balancing area must appear once, as one contiguous block of hours 1..hours_in_year.
    """
    name = path.name
    seen: set[str] = set()
    current: str | None = None
    previous_hour = 0
    for line, row in enumerate(iter_csv(path, ("balancing_area", "hour", column)), start=2):
        ba = row["balancing_area"].strip()
        if not ba:
            raise DataError(f"{name}:{line}: blank balancing_area")
        if ba != current:
            _check_complete(name, current, previous_hour, hours_in_year)
            if ba in seen:
                raise DataError(f"{name}:{line}: {ba} rows are not contiguous")
            seen.add(ba)
            current, previous_hour = ba, 0
        hour = _parse_hour(row["hour"], name, line)
        if hour != previous_hour + 1:
            raise DataError(f"{name}:{line}: {ba}: expected hour {previous_hour + 1}, got {hour}")
        previous_hour = hour
        yield HourlyValue(ba, hour, _parse_value(row[column], column, name, line))
    if current is None:
        raise DataError(f"{name}: no rows")
    _check_complete(name, current, previous_hour, hours_in_year)


def _check_complete(name: str, ba: str | None, last_hour: int, hours_in_year: int) -> None:
    if ba is not None and last_hour != hours_in_year:
        raise DataError(f"{name}: {ba} has {last_hour} hours, expected {hours_in_year}")


def _parse_hour(text: str, name: str, line: int) -> int:
    try:
        return int(text)
    except ValueError:
        raise DataError(f"{name}:{line}: hour is not an integer: {text!r}") from None


def _parse_value(text: str, column: str, name: str, line: int) -> float:
    try:
        value = float(text)
    except ValueError:
        raise DataError(f"{name}:{line}: {column} is not a number: {text!r}") from None
    if not math.isfinite(value):
        raise DataError(f"{name}:{line}: invalid {column} {text!r}")
    return value


def _bounded(params: Config, key: str, *, allow_zero: bool, maximum: float = math.inf) -> float:
    value = params.get(key, float)
    low_ok = value >= 0.0 if allow_zero else value > 0.0
    if not (low_ok and value <= maximum and math.isfinite(value)):
        low = "[0" if allow_zero else "(0"
        high = f"{maximum:g}]" if math.isfinite(maximum) else "inf)"
        raise ConfigError(f"dispatch.{key} must be in {low}, {high}: {value}")
    return value
