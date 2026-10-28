"""Streamlit dashboard for a single run.

Usage: bin/viz output/<run-folder>
"""

import sys
from pathlib import Path

import streamlit as st

from flux_match import paths
from flux_match.errors import FluxMatchError
from flux_match.run_context import open_run
from flux_match.viz.charts import (
    HourlyTable,
    build_figure,
    default_authority,
    display_times_for,
    load_dashboard_tables,
    panels_for,
)

# Fits the longest label ("Newfoundland and Labrador.b") without spanning the chart.
AUTHORITY_SELECT_WIDTH = 340


@st.cache_data(show_spinner="Reading hourly files")
def _cached_tables(
    run_dir: str, year: int, data_dir: str
) -> tuple[HourlyTable, HourlyTable, HourlyTable]:
    ctx = open_run(Path(run_dir))
    return load_dashboard_tables(ctx, year, Path(data_dir))


def main() -> None:
    st.set_page_config(page_title="Flux Match", layout="wide")
    if len(sys.argv) != 2:
        st.error("Usage: bin/viz output/<run-folder>")
        st.stop()
    try:
        ctx = open_run(Path(sys.argv[1]))
        ctx.require_complete("dispatch")
        years = ctx.config.section("dispatch").get_list("dispatch_years", int, unique=True)
    except FluxMatchError as e:
        st.error(str(e))
        st.stop()

    st.title(f"Flux Match: {ctx.run_dir.name}")

    year_col, authority_col = st.columns([1, 3])
    with year_col:
        year = st.selectbox("Year", years, index=years.index(max(years)))
    try:
        demand, vre, dispatch = _cached_tables(str(ctx.run_dir), year, str(paths.DATA_DIR))
    except FluxMatchError as e:
        st.error(str(e))
        st.stop()

    authorities = list(demand.authorities)
    with authority_col:
        authority = st.selectbox(
            "Balancing area",
            authorities,
            index=authorities.index(default_authority(authorities)),
            width=AUTHORITY_SELECT_WIDTH,
        )

    st.plotly_chart(
        build_figure(
            authority,
            year,
            display_times_for(demand.hours, year),
            panels_for(authority, demand, vre, dispatch),
        ),
        width="stretch",
        config={"scrollZoom": False, "displaylogo": False},
    )
    st.caption(
        f"Hourly wind and solar generation for {authority} from the sites selected in this run, "
        "using the single capacity-factor year in the scenario. "
        "Battery output is positive when discharging and negative when charging. "
        "Net demand is demand minus wind, solar, and battery output."
    )
    st.caption(
        f"Hour 1 is {year}-01-01 00:00. Leap years omit February 29, matching the stored "
        "8760-hour series. There is no timezone and no daylight-saving shift."
    )


if __name__ == "__main__":
    main()
