"""Hourly series loading and the stacked Plotly figure used by the dashboard.

The charts share one date axis so a hover spike is drawn through every panel.
"""

import calendar
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import plotly.graph_objects as go

from flux_match import paths
from flux_match.errors import DataError
from flux_match.io import iter_csv
from flux_match.run_context import RunContext
from flux_match.schema import FIRST_HOUR_AFTER_LEAP_DAY, HOURS_PER_YEAR


class Resolution(StrEnum):
    HOURLY = "Hourly"
    DAILY = "Daily"
    MONTHLY = "Monthly"


class Zoom(StrEnum):
    WEEK = "7 days"
    MONTH = "30 days"
    FULL_YEAR = "Full year"


ZOOM_DAYS = {Zoom.WEEK: 7, Zoom.MONTH: 30}

# A 7- or 30-day window holds at most one monthly point, so monthly charts only get "Full year".
ZOOM_OPTIONS = {
    Resolution.HOURLY: tuple(Zoom),
    Resolution.DAILY: tuple(Zoom),
    Resolution.MONTHLY: (Zoom.FULL_YEAR,),
}

HOVER_TEMPLATES = {
    Resolution.HOURLY: "%{x|%Y-%m-%d %H:%M} %{y} MW ",
    Resolution.DAILY: "%{x|%Y-%m-%d} mean %{y:,.1f} MW ",
    Resolution.MONTHLY: "%{x|%b %Y} mean %{y:,.1f} MW ",
}

DEFAULT_AUTHORITY = "Ontario.a"

PANEL_SPECS = (
    ("wind_mw", "Wind generation", True),
    ("solar_mw", "Solar generation", True),
    ("demand_mw", "Demand", True),
    ("battery_output_mw", "Battery output", True),
    ("net_demand_mw", "Net demand", True),
)

Panel = tuple[str, tuple[int | float, ...], bool]


@dataclass(frozen=True)
class HourlyTable:
    path: Path
    authorities: tuple[str, ...]
    hours: tuple[int, ...]
    series: dict[str, dict[str, tuple[int | float, ...]]]


def parse_mw_cell(value: str) -> int | float:
    """A finite MW value. Whole numbers stay ints so hover labels stay clean."""
    try:
        number = float(value)
    except ValueError:
        raise ValueError(value) from None
    if not math.isfinite(number):
        raise ValueError(value)
    as_int = int(number)
    return as_int if as_int == number else number


def hour_to_display_time(hour: int, year: int) -> datetime:
    """Map hour-of-year `hour` onto `year`, skipping February 29 in leap years.

    Stored series always have 8760 hours: leap years drop February 29 and renumber, so
    hour 1417 is March 1. The result is naive: no timezone and no daylight-saving shift.
    """
    if type(hour) is not int or hour < 1 or hour > HOURS_PER_YEAR:
        raise DataError(f"Hour {hour!r} is not an hour of the year")
    extra = 24 if calendar.isleap(year) and hour >= FIRST_HOUR_AFTER_LEAP_DAY else 0
    return datetime(year, 1, 1) + timedelta(hours=hour - 1 + extra)


def format_display_time(moment: datetime) -> str:
    """Clock time with no timezone suffix, so a Plotly date axis shows these digits."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def display_times_for(hours: Sequence[int], year: int) -> tuple[str, ...]:
    return tuple(format_display_time(hour_to_display_time(hour, year)) for hour in hours)


@dataclass(frozen=True)
class Bucket:
    start: int
    stop: int
    midpoint: datetime


def _bucket_key(moment: datetime, resolution: Resolution) -> tuple[int, ...]:
    if resolution is Resolution.DAILY:
        return (moment.month, moment.day)
    if resolution is Resolution.MONTHLY:
        return (moment.month,)
    raise DataError(f"No buckets for {resolution} resolution")


def _expected_bucket_hours(moment: datetime, resolution: Resolution) -> int:
    if resolution is Resolution.DAILY:
        return 24
    days = calendar.monthrange(moment.year, moment.month)[1]
    if moment.month == 2 and calendar.isleap(moment.year):
        days -= 1
    return 24 * days


def buckets_for(hours: Sequence[int], year: int, resolution: Resolution) -> tuple[Bucket, ...]:
    """Index ranges into `hours` for each whole day or month, and the midpoint of each.

    Hours must be consecutive and every bucket complete, so a mean never silently covers
    a partial day or month. Leap-year Februarys have 28 days, matching the stored series.
    """
    if not hours:
        raise DataError("No hours to aggregate")
    for previous, hour in zip(hours, hours[1:], strict=False):
        if hour != previous + 1:
            raise DataError(f"Hours are not consecutive: {previous} is followed by {hour}")
    moments = [hour_to_display_time(hour, year) for hour in hours]
    buckets: list[Bucket] = []
    start = 0
    for index in range(1, len(moments) + 1):
        if index < len(moments) and _bucket_key(moments[index], resolution) == _bucket_key(
            moments[start], resolution
        ):
            continue
        first = moments[start]
        size = index - start
        expected = _expected_bucket_hours(first, resolution)
        if size != expected:
            raise DataError(
                f"{resolution} bucket starting {format_display_time(first)} has {size} hours, "
                f"expected {expected}"
            )
        buckets.append(Bucket(start, index, first + timedelta(hours=size / 2)))
        start = index
    return tuple(buckets)


def aggregate_panels(
    hours: Sequence[int],
    year: int,
    panels: Sequence[Panel],
    resolution: Resolution,
) -> tuple[tuple[str, ...], tuple[Panel, ...]]:
    """Display times and panel values at `resolution`.

    Daily and monthly values are the mean of each bucket's hours, plotted at its midpoint.
    """
    if resolution is Resolution.HOURLY:
        return display_times_for(hours, year), tuple(panels)
    buckets = buckets_for(hours, year, resolution)
    for title, megawatts, _ in panels:
        if len(megawatts) != len(hours):
            raise DataError(f"{title} has {len(megawatts)} values for {len(hours)} hours")
    times = tuple(format_display_time(bucket.midpoint) for bucket in buckets)
    aggregated = tuple(
        (
            title,
            tuple(
                math.fsum(megawatts[bucket.start : bucket.stop]) / (bucket.stop - bucket.start)
                for bucket in buckets
            ),
            pin_zero,
        )
        for title, megawatts, pin_zero in panels
    )
    return times, aggregated


def load_hourly_table(
    path: Path,
    value_columns: Sequence[str],
    *,
    hours_in_year: int = HOURS_PER_YEAR,
    expected_year: int | None = None,
) -> HourlyTable:
    """Read a long-format hourly CSV (balancing_area, hour, value columns).

    Each balancing area must appear once, as one contiguous block of hours 1..hours_in_year.
    Raises DataError rather than returning a partial table.
    """
    if not value_columns:
        raise DataError(f"{path.name}: no value columns to load")
    required = ["balancing_area", "hour", *value_columns]
    if expected_year is not None:
        required = ["balancing_area", "year", "hour", *value_columns]

    name = path.name
    seen: set[str] = set()
    current: str | None = None
    previous_hour = 0
    columns: dict[str, dict[str, list[int | float]]] = {}

    for line, row in enumerate(iter_csv(path, required), start=2):
        ba = row["balancing_area"].strip()
        if not ba:
            raise DataError(f"{name}:{line}: blank balancing_area")
        if ba != current:
            _check_complete(name, current, previous_hour, hours_in_year)
            if ba in seen:
                raise DataError(f"{name}:{line}: {ba} rows are not contiguous")
            seen.add(ba)
            current, previous_hour = ba, 0
            columns[ba] = {column: [] for column in value_columns}
        if expected_year is not None:
            _check_year(row["year"], expected_year, name, line)
        hour = _parse_hour(row["hour"], name, line)
        if hour != previous_hour + 1:
            raise DataError(f"{name}:{line}: {ba}: expected hour {previous_hour + 1}, got {hour}")
        previous_hour = hour
        for column in value_columns:
            columns[ba][column].append(_parse_mw(row[column], column, name, line))

    if current is None:
        raise DataError(f"{name}: no rows")
    _check_complete(name, current, previous_hour, hours_in_year)

    series = {
        ba: {column: tuple(values) for column, values in ba_cols.items()}
        for ba, ba_cols in columns.items()
    }
    return HourlyTable(
        path=path,
        authorities=tuple(sorted(series)),
        hours=tuple(range(1, hours_in_year + 1)),
        series=series,
    )


def load_dashboard_tables(
    ctx: RunContext,
    year: int,
    data_dir: Path = paths.DATA_DIR,
    *,
    hours_in_year: int = HOURS_PER_YEAR,
) -> tuple[HourlyTable, HourlyTable, HourlyTable]:
    """Demand, wind/solar, and dispatch tables for one year, checked against each other."""
    demand_path = data_dir / paths.ba_demand_hourly_filename(year)
    demand = load_hourly_table(demand_path, ("demand_mw",), hours_in_year=hours_in_year)
    vre = load_hourly_table(
        ctx.vre_hourly_path, ("wind_mw", "solar_mw"), hours_in_year=hours_in_year
    )
    dispatch = load_hourly_table(
        ctx.dispatch_hourly_path(year),
        ("battery_output_mw", "net_demand_mw"),
        hours_in_year=hours_in_year,
        expected_year=year,
    )
    require_aligned((demand, vre, dispatch))
    return demand, vre, dispatch


def require_aligned(tables: Sequence[HourlyTable]) -> None:
    """The charts share one x-axis, so hours and balancing areas have to be identical."""
    if not tables:
        raise DataError("No hourly files to chart")
    first = tables[0]
    for table in tables[1:]:
        if table.hours != first.hours:
            raise DataError(f"{table.path.name} hours do not match {first.path.name}")
        if table.authorities != first.authorities:
            raise DataError(
                f"{table.path.name} balancing areas {list(table.authorities)} do not match "
                f"{first.path.name} {list(first.authorities)}"
            )


def values(table: HourlyTable, authority: str, column: str) -> tuple[int | float, ...]:
    found = table.series.get(authority)
    if found is None or column not in found:
        raise DataError(f"{table.path.name} has no {column!r} series for {authority!r}")
    return found[column]


def default_authority(authorities: Sequence[str]) -> str:
    if not authorities:
        raise DataError("No balancing areas to chart")
    if DEFAULT_AUTHORITY in authorities:
        return DEFAULT_AUTHORITY
    return authorities[0]


def panels_for(
    authority: str,
    demand: HourlyTable,
    vre: HourlyTable,
    dispatch: HourlyTable,
) -> tuple[Panel, ...]:
    sources = {
        "demand_mw": demand,
        "wind_mw": vre,
        "solar_mw": vre,
        "battery_output_mw": dispatch,
        "net_demand_mw": dispatch,
    }
    return tuple(
        (title, values(sources[column], authority, column), pin_zero)
        for column, title, pin_zero in PANEL_SPECS
    )


def _panel_domains(count: int) -> list[tuple[float, float]]:
    """Top-to-bottom paper domains, leaving room under the stack for the range slider."""
    slider = 0.09
    gap = 0.05
    height = (1 - slider - gap * (count - 1)) / count
    domains: list[tuple[float, float]] = []
    start = slider
    for _ in range(count):
        domains.append((start, start + height))
        start += height + gap
    domains.reverse()
    return domains


def build_figure(
    authority: str,
    year: int,
    display_times: Sequence[str],
    panels: Sequence[tuple[str, Sequence[int | float], bool]],
    resolution: Resolution = Resolution.HOURLY,
    zoom: Zoom = Zoom.FULL_YEAR,
) -> go.Figure:
    """Stacked lines on one x-axis, so a hover spike is drawn through every panel.

    `panels` is `(title, megawatts, pin_y_at_zero)` from top to bottom.
    `zoom` shows the last 7 or 30 days of the year, or all of it.
    `uirevision` resets the zoom when the authority, year, resolution, or zoom changes.
    """
    if not panels:
        raise DataError("No series to chart")
    if zoom not in ZOOM_OPTIONS[resolution]:
        raise DataError(f"{zoom} zoom is not available at {resolution} resolution")
    domains = _panel_domains(len(panels))
    figure = go.Figure()
    x_values = list(display_times)
    annotations = []
    for index, ((title, megawatts, pin_zero), domain) in enumerate(
        zip(panels, domains, strict=True), start=1
    ):
        yaxis_name = "y" if index == 1 else f"y{index}"
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=list(megawatts),
                mode="lines",
                line={"width": 1},
                name=title,
                yaxis=yaxis_name,
                hovertemplate=HOVER_TEMPLATES[resolution],
            )
        )
        yaxis: dict[str, object] = {"title": "MW", "domain": domain, "anchor": "x"}
        if pin_zero:
            yaxis["rangemode"] = "tozero"
        figure.update_layout({f"yaxis{'' if index == 1 else index}": yaxis})
        annotations.append(
            {
                "text": f"{title}, {year}",
                "x": 0.5,
                "xref": "paper",
                "y": domain[1],
                "yref": "paper",
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
            }
        )

    bottom_yaxis = "y" if len(panels) == 1 else f"y{len(panels)}"
    xaxis: dict[str, object] = {
        "type": "date",
        "anchor": bottom_yaxis,
        "domain": [0, 1],
        "rangeslider": {"visible": True, "thickness": 0.06},
        "showspikes": True,
        "spikemode": "across",
        "spikesnap": "data",
        "spikecolor": "#333333",
        "spikethickness": 1,
    }
    if zoom is not Zoom.FULL_YEAR:
        end = datetime(year + 1, 1, 1)
        start = end - timedelta(days=ZOOM_DAYS[zoom])
        xaxis["range"] = [format_display_time(start), format_display_time(end)]
    figure.update_layout(
        xaxis=xaxis,
        annotations=annotations,
        height=260 * len(panels) + 120,
        hovermode="x",
        hoversubplots="axis",
        spikedistance=-1,
        uirevision=f"{authority}|{year}|{resolution}|{zoom}",
        showlegend=False,
        margin={"l": 64, "r": 24, "t": 40, "b": 24},
    )
    return figure


def _check_complete(name: str, ba: str | None, last_hour: int, hours_in_year: int) -> None:
    if ba is not None and last_hour != hours_in_year:
        raise DataError(f"{name}: {ba} has {last_hour} hours, expected {hours_in_year}")


def _parse_hour(text: str, name: str, line: int) -> int:
    try:
        return int(text)
    except ValueError:
        raise DataError(f"{name}:{line}: hour is not an integer: {text!r}") from None


def _check_year(text: str, expected_year: int, name: str, line: int) -> None:
    try:
        year = int(text)
    except ValueError:
        raise DataError(f"{name}:{line}: year is not an integer: {text!r}") from None
    if year != expected_year:
        raise DataError(f"{name}:{line}: year is {year}, expected {expected_year}")


def _parse_mw(text: str, column: str, name: str, line: int) -> int | float:
    try:
        return parse_mw_cell(text)
    except ValueError:
        raise DataError(f"{name}:{line}: {column} is not a number of MW: {text!r}") from None
