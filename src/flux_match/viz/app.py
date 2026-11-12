"""Streamlit dashboard for a single run.

Usage: bin/viz [output/<run-folder>]

With no folder, opens the newest run in output/.
"""

import sys
from collections.abc import Sequence
from pathlib import Path

import streamlit as st

from flux_match import paths
from flux_match.errors import FluxMatchError
from flux_match.run_context import latest_run_dir, open_run, run_dir_timestamp
from flux_match.viz.charts import (
    ZOOM_OPTIONS,
    HourlyTable,
    Resolution,
    Zoom,
    aggregate_panels,
    build_figure,
    default_balancing_area,
    load_dashboard_tables,
    panels_for,
)

# Fits the longest label ("Newfoundland and Labrador.b") without spanning the chart.
BALANCING_AREA_SELECT_WIDTH = 340

SERIES_DESCRIPTIONS = {
    Resolution.HOURLY: "Hourly",
    Resolution.DAILY: "Daily mean",
    Resolution.MONTHLY: "Monthly mean",
}


@st.cache_data(show_spinner="Reading hourly files")
def _cached_tables(run_dir: str, year: int) -> tuple[HourlyTable, HourlyTable, HourlyTable]:
    ctx = open_run(Path(run_dir))
    return load_dashboard_tables(ctx, year)


def _select_run_dir(argv: Sequence[str], output_dir: Path = paths.OUTPUT_DIR) -> Path:
    if len(argv) == 1:
        run_dir = latest_run_dir(output_dir)
        stamp = run_dir_timestamp(run_dir)
        print(f"No output specified. Opening dashboard on most recent model run {stamp}.")
        return run_dir
    if len(argv) == 2:
        return Path(argv[1])
    raise FluxMatchError("Usage: bin/viz [output/<run-folder>]")


def main() -> None:
    st.set_page_config(page_title="Flux Match", layout="wide")
    try:
        run_dir = _select_run_dir(sys.argv)
        ctx = open_run(run_dir)
        ctx.require_complete("dispatch")
        years = ctx.config.section("dispatch").get_list("dispatch_years", int, unique=True)
    except FluxMatchError as e:
        st.error(str(e))
        st.stop()

    st.title(f"Flux Match: {ctx.run_dir.name}")

    year_col, balancing_area_col, resolution_col, zoom_col = st.columns([1, 2, 1.5, 1.5])
    with year_col:
        year = st.selectbox("Year", years, index=years.index(max(years)))
    try:
        demand, vre, dispatch = _cached_tables(str(ctx.run_dir), year)
    except FluxMatchError as e:
        st.error(str(e))
        st.stop()

    balancing_areas = list(demand.balancing_areas)
    with balancing_area_col:
        balancing_area = st.selectbox(
            "Balancing area",
            balancing_areas,
            index=balancing_areas.index(default_balancing_area(balancing_areas)),
            width=BALANCING_AREA_SELECT_WIDTH,
        )
    with resolution_col:
        resolution = Resolution(
            st.segmented_control(
                "Aggregation",
                list(Resolution),
                default=Resolution.HOURLY,
                required=True,
            )
        )
    with zoom_col:
        zoom_options = ZOOM_OPTIONS[resolution]
        # A key per resolution, so changing the options never leaves a stale selection.
        zoom = Zoom(
            st.segmented_control(
                "Zoom",
                list(zoom_options),
                default=Zoom.FULL_YEAR,
                required=True,
                key=f"zoom_{resolution}",
                disabled=len(zoom_options) == 1,
            )
        )

    try:
        display_times, panels = aggregate_panels(
            demand.hours, year, panels_for(balancing_area, demand, vre, dispatch), resolution
        )
    except FluxMatchError as e:
        st.error(str(e))
        st.stop()

    st.plotly_chart(
        build_figure(balancing_area, year, display_times, panels, resolution, zoom),
        width="stretch",
        config={"scrollZoom": False, "displaylogo": False},
    )
    st.caption(
        f"{SERIES_DESCRIPTIONS[resolution]} wind and solar generation for {balancing_area} from "
        "the sites selected in this run, using the single capacity-factor year in the scenario. "
        "Battery output is positive when discharging and negative when charging. "
        "Net demand is demand minus wind, solar, and battery output."
    )
    if resolution is not Resolution.HOURLY:
        st.caption(
            f"Each point is the mean of the hourly values in its {resolution.lower()} bucket, "
            "plotted at the midpoint of the bucket."
        )
    st.caption(
        f"Hour 1 is {year}-01-01 00:00. Leap years omit February 29, matching the stored "
        "8760-hour series. There is no timezone and no daylight-saving shift."
    )


if __name__ == "__main__":
    main()
