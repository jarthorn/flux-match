"""Balancing-area map and demand overview, as one HTML file.

    uv run --group viz python -m flux_match.viz.ba_overview [--out PATH]

The page links ``article.css`` in the same directory as the HTML file (``output/`` has a
symlink for that) and loads plotly.js from the CDN. A select box switches the map and both
charts among balancing areas; the default is Ontario.a.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import plotly.graph_objects as go

from flux_match import paths
from flux_match.errors import DataError, FluxMatchError
from flux_match.grid_cell_rankings import read_grid_cells, read_locations
from flux_match.io import iter_csv, read_csv
from flux_match.schema import HOURS_PER_YEAR, NORMALIZED_DEMAND_COLUMNS
from flux_match.viz.cf_animation import cell_polygons, check_spacing, hour_label
from flux_match.viz.cf_stats_compare import (
    MONTH_NAMES,
    hours_to_day_of_year,
    month_start_day_of_year,
)

DEFAULT_BA = "Ontario.a"
SELECTED_COLOR = "#0b3a6b"
OTHER_COLOR = "#d7e7f6"
CSS_HREF = "article.css"

MAP_DIV_ID = "ba-map"
ANNUAL_DIV_ID = "ba-annual"
NORMALIZED_DIV_ID = "ba-normalized"

BA_COLUMN, HOUR_COLUMN, NORMALIZED_COLUMN = NORMALIZED_DEMAND_COLUMNS

TITLE = "Balancing area overview"
LEDE = (
    "Balancing areas are the regions of the Canadian electricity grid used by this model. "
    "Most provinces are a single balancing area; Newfoundland, Ontario, and Quebec are "
    "split into two. Choose an area to see where its grid cells are and how its demand varies."
)
MAP_TEXT = (
    "Each rectangle is one "
    '<a href="https://coders.cme-emh.ca/">CODERS</a> '
    "map grid cell. Cells in the selected balancing area are dark blue."
)
ANNUAL_TEXT = (
    "Total electricity demand in the selected balancing area for each year, in GWh."
)
NORMALIZED_TEXT = (
    "A normalized demand curve is generated for each year by dividing the demand in "
    "each hour by the average hourly demand for that year. Demand curves for each year "
    "are then averaged to produce a multi-year normalized demand. This demand curve can "
    "be used when selecting sites for renewable generation in that balancing area. "
    "This allows tailoring generation to the general demand curve, without overfitting "
    "to the demand for a particular year. The average hourly value in this graph will "
    "always be 1.0, and typically has a range between 0 and 2.0"
)

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <link rel="stylesheet" href="__CSS_HREF__">
  <style>
    .ba-select {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin: 0 0 1.5rem;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 1rem;
    }
    .ba-select select {
      font: inherit;
      color: var(--ink);
      background: var(--panel);
      border: 1px solid var(--rule);
      border-radius: 4px;
      padding: 0.35rem 0.6rem;
    }
    #map-box { width: 100%; aspect-ratio: 1.8; }
  </style>
</head>
<body>
  <main>
    <h1>__TITLE__</h1>
    <p class="lede">__LEDE__</p>
    <label class="ba-select">Balancing area
      <select id="ba-select">
__OPTIONS__
      </select>
    </label>

    <h2>Map overview</h2>
    <p>__MAP_TEXT__</p>
    <div class="chart"><div id="map-box">__MAP__</div></div>

    <h2>Demand overview</h2>
    <h3>Annual demand</h3>
    <p>__ANNUAL_TEXT__</p>
    <div class="chart">__ANNUAL__</div>

    <h3>Normalized demand</h3>
    <p>__NORMALIZED_TEXT__</p>
    <div class="chart">__NORMALIZED__</div>
  </main>
  <script id="ba-data" type="application/json">__DATA__</script>
  <script>
(function () {
  const payload = JSON.parse(document.getElementById("ba-data").textContent);
  const select = document.getElementById("ba-select");

  function show(ba) {
    const z = payload.cellAreas.map((area) => (area === ba ? 1 : 0));
    Plotly.restyle("__MAP_ID__", {z: [z]});
    const annual = payload.annual[ba];
    const normalized = payload.normalized[ba];
    Plotly.restyle("__ANNUAL_ID__", {y: [annual], name: ba}).then(() => {
      Plotly.relayout("__ANNUAL_ID__", {"yaxis.autorange": true});
    });
    Plotly.restyle("__NORMALIZED_ID__", {y: [normalized], name: ba}).then(() => {
      Plotly.relayout("__NORMALIZED_ID__", {"yaxis.autorange": true});
    });
  }

  select.addEventListener("change", () => show(select.value));
})();
  </script>
</body>
</html>
"""


def mwh_to_gwh(mwh: float) -> int:
    """Nearest integer GWh. Halves round away from zero."""
    gwh = mwh / 1000.0
    if gwh >= 0:
        return math.floor(gwh + 0.5)
    return math.ceil(gwh - 0.5)


def membership_z(areas: Sequence[str], selected: str) -> list[int]:
    """1 for cells in `selected`, 0 for every other cell."""
    return [1 if area == selected else 0 for area in areas]


def _parse_number(text: str, *, where: str) -> float:
    try:
        value = float(text)
    except TypeError, ValueError:
        raise DataError(f"{where}: not a number ({text!r})") from None
    if not math.isfinite(value):
        raise DataError(f"{where}: non-finite value ({text!r})")
    return value


def _year_columns(name: str, row: Mapping[str, str]) -> list[int]:
    years = []
    for column in row:
        if column == BA_COLUMN:
            continue
        try:
            years.append(int(column))
        except ValueError:
            raise DataError(f"{name}: demand column {column!r} is not a year") from None
    if not years:
        raise DataError(f"{name}: no demand year columns")
    if years != sorted(years):
        raise DataError(f"{name}: demand years are not in ascending order: {years}")
    return years


def load_annual_demand(path: Path) -> tuple[list[int], dict[str, list[int]]]:
    """Year columns, and annual demand in GWh (rounded) for each balancing area."""
    rows = read_csv(path, (BA_COLUMN,))
    if not rows:
        raise DataError(f"{path.name}: no balancing areas")
    years = _year_columns(path.name, rows[0])
    demand: dict[str, list[int]] = {}
    for line, row in enumerate(rows, start=2):
        ba = row[BA_COLUMN].strip()
        if not ba:
            raise DataError(f"{path.name}:{line}: blank balancing_area")
        if ba in demand:
            raise DataError(f"{path.name}:{line}: duplicate balancing area {ba}")
        gwh = []
        for year in years:
            where = f"{path.name}:{line}: {year} demand"
            gwh.append(mwh_to_gwh(_parse_number(row.get(str(year), ""), where=where)))
        demand[ba] = gwh
    return years, demand


def load_normalized_demand(path: Path) -> dict[str, list[float]]:
    """Normalized demand for hours 1..8760, in that order, per balancing area."""
    series: dict[str, list[float]] = {}
    for line, row in enumerate(iter_csv(path, NORMALIZED_DEMAND_COLUMNS), start=2):
        ba = row[BA_COLUMN].strip()
        if not ba:
            raise DataError(f"{path.name}:{line}: blank balancing_area")
        where = f"{path.name}:{line}: {ba}"
        try:
            hour = int(row[HOUR_COLUMN])
        except TypeError, ValueError:
            raise DataError(f"{where}: invalid hour {row[HOUR_COLUMN]!r}") from None
        expected = len(series.get(ba, ())) + 1
        if hour != expected:
            raise DataError(f"{where}: expected hour {expected}, got {hour}")
        if expected > HOURS_PER_YEAR:
            raise DataError(f"{where}: more than {HOURS_PER_YEAR} hours")
        value = _parse_number(row[NORMALIZED_COLUMN], where=f"{where}: normalized demand")
        series.setdefault(ba, []).append(value)
    if not series:
        raise DataError(f"{path.name}: no balancing areas")
    short = sorted(ba for ba, values in series.items() if len(values) != HOURS_PER_YEAR)
    if short:
        raise DataError(
            f"{path.name}: expected {HOURS_PER_YEAR} hours for {', '.join(short)}, "
            f"got {', '.join(str(len(series[ba])) for ba in short)}"
        )
    return series


def require_same_areas(
    grid_areas: set[str],
    annual_areas: set[str],
    normalized_areas: set[str],
) -> list[str]:
    """Balancing areas present in every input, sorted. The default area must be one of them."""
    labelled = (
        ("grid_cell_info.csv", grid_areas),
        ("balancing_area_demand.csv", annual_areas),
        ("normalized_demand_hourly.csv", normalized_areas),
    )
    union = set().union(*(areas for _, areas in labelled))
    missing = [
        f"{name} is missing {sorted(union - areas)}" for name, areas in labelled if areas != union
    ]
    if missing:
        raise DataError("balancing areas differ across inputs: " + "; ".join(missing))
    if DEFAULT_BA not in union:
        raise DataError(f"{DEFAULT_BA} is not in the inputs")
    return sorted(union)


def load_grid(path: Path) -> tuple[list[str], list[str], dict[str, Any]]:
    """Cell ids, balancing areas, and one rectangle per cell, in file order."""
    cells, areas = read_grid_cells(path)
    locations = read_locations(path)
    missing = [cell for cell in cells if cell not in locations]
    if missing:
        raise DataError(f"{path.name}: no location for grid cells {missing[:10]}")
    latitudes = [locations[cell][0] for cell in cells]
    longitudes = [locations[cell][1] for cell in cells]
    check_spacing(longitudes, latitudes)
    return cells, areas, cell_polygons(cells, longitudes, latitudes)


def _require_default(
    annual: Mapping[str, Sequence[int]],
    normalized: Mapping[str, Sequence[float]],
    balancing_areas: Sequence[str],
) -> None:
    if (
        DEFAULT_BA not in balancing_areas
        or DEFAULT_BA not in annual
        or DEFAULT_BA not in normalized
    ):
        raise DataError(f"{DEFAULT_BA} is not in the inputs")
    missing = [ba for ba in balancing_areas if ba not in annual or ba not in normalized]
    if missing:
        raise DataError(f"missing demand series for {', '.join(missing)}")


def make_map_figure(
    geojson: dict[str, Any],
    cells: Sequence[str],
    areas: Sequence[str],
    selected: str,
) -> go.Figure:
    """Choropleth of grid cells. `selected` is dark blue; every other cell is light blue."""
    fig = go.Figure(
        go.Choropleth(
            geojson=geojson,
            locations=list(cells),
            z=membership_z(areas, selected),
            zmin=0,
            zmax=1,
            colorscale=[[0.0, OTHER_COLOR], [1.0, SELECTED_COLOR]],
            marker_line_width=0,
            showscale=False,
            hoverinfo="skip",
        )
    )
    fig.update_geos(
        scope="north america",
        showcountries=True,
        showsubunits=True,
        showlakes=True,
        projection_type="conic conformal",
        lataxis_range=[41, 70],
        lonaxis_range=[-142, -52],
    )
    fig.update_layout(
        autosize=True,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        paper_bgcolor="#ffffff",
        geo_bgcolor="#ffffff",
    )
    return fig


def make_annual_figure(years: Sequence[int], gwh: Sequence[int], name: str) -> go.Figure:
    """Annual demand in GWh for one balancing area."""
    fig = go.Figure(
        go.Scatter(
            x=list(years),
            y=list(gwh),
            mode="lines+markers",
            name=name,
            line={"color": SELECTED_COLOR, "width": 2},
            marker={"color": SELECTED_COLOR, "size": 8},
            hovertemplate="Year %{x}<br>%{y:,.0f} GWh<extra></extra>",
        )
    )
    fig.update_layout(
        margin={"l": 72, "r": 24, "t": 16, "b": 48},
        showlegend=False,
        xaxis={
            "title": "Year",
            "tickmode": "linear",
            "dtick": 1,
            "tickformat": "d",
            "showgrid": True,
            "gridcolor": "#e6ebef",
        },
        yaxis={
            "title": "Annual demand (GWh)",
            "showgrid": True,
            "gridcolor": "#e6ebef",
            "zeroline": False,
            "tickformat": ",",
        },
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        hovermode="x unified",
    )
    return fig


def make_normalized_figure(values: Sequence[float], name: str) -> go.Figure:
    """Normalized demand across the hours of one non-leap year."""
    hours = list(range(1, len(values) + 1))
    fig = go.Figure(
        go.Scatter(
            x=hours_to_day_of_year(hours),
            y=list(values),
            customdata=[hour_label(hour) for hour in hours],
            mode="lines",
            name=name,
            line={"color": SELECTED_COLOR, "width": 1.2},
            hovertemplate="%{customdata}<br>%{y:.3f}<extra></extra>",
        )
    )
    fig.update_layout(
        margin={"l": 60, "r": 24, "t": 16, "b": 48},
        showlegend=False,
        xaxis={
            "title": None,
            "tickmode": "array",
            "tickvals": month_start_day_of_year(),
            "ticktext": list(MONTH_NAMES),
            "range": [1, 365],
            "showgrid": True,
            "gridcolor": "#e6ebef",
        },
        yaxis={
            "title": "Normalized demand",
            "showgrid": True,
            "gridcolor": "#e6ebef",
            "zeroline": False,
        },
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        hovermode="x unified",
    )
    return fig


def _options_html(balancing_areas: Sequence[str]) -> str:
    lines = []
    for area in balancing_areas:
        selected = " selected" if area == DEFAULT_BA else ""
        escaped = html.escape(area, quote=True)
        lines.append(f'        <option value="{escaped}"{selected}>{escaped}</option>')
    return "\n".join(lines)


def _figure_html(fig: go.Figure, *, div_id: str, include_plotlyjs: str | bool, height: str) -> str:
    return fig.to_html(
        include_plotlyjs=include_plotlyjs,
        full_html=False,
        div_id=div_id,
        default_width="100%",
        default_height=height,
        config={"responsive": True},
    )


def render_page(
    *,
    cells: Sequence[str],
    areas: Sequence[str],
    geojson: dict[str, Any],
    years: Sequence[int],
    annual: Mapping[str, Sequence[int]],
    normalized: Mapping[str, Sequence[float]],
    balancing_areas: Sequence[str],
) -> str:
    """The full HTML page for `balancing_areas`, drawn first for Ontario.a."""
    _require_default(annual, normalized, balancing_areas)
    if len(cells) != len(areas):
        raise DataError(f"grid has {len(cells)} cells but {len(areas)} balancing areas")
    payload = {
        "cellAreas": list(areas),
        "annual": {ba: list(annual[ba]) for ba in balancing_areas},
        "normalized": {ba: list(normalized[ba]) for ba in balancing_areas},
    }
    values = {
        "TITLE": TITLE,
        "CSS_HREF": CSS_HREF,
        "LEDE": LEDE,
        "OPTIONS": _options_html(balancing_areas),
        "MAP_TEXT": MAP_TEXT,
        "ANNUAL_TEXT": ANNUAL_TEXT,
        "NORMALIZED_TEXT": NORMALIZED_TEXT,
        "MAP_ID": MAP_DIV_ID,
        "ANNUAL_ID": ANNUAL_DIV_ID,
        "NORMALIZED_ID": NORMALIZED_DIV_ID,
        "MAP": _figure_html(
            make_map_figure(geojson, cells, areas, DEFAULT_BA),
            div_id=MAP_DIV_ID,
            include_plotlyjs="cdn",
            height="100%",
        ),
        "ANNUAL": _figure_html(
            make_annual_figure(years, annual[DEFAULT_BA], DEFAULT_BA),
            div_id=ANNUAL_DIV_ID,
            include_plotlyjs=False,
            height="420px",
        ),
        "NORMALIZED": _figure_html(
            make_normalized_figure(normalized[DEFAULT_BA], DEFAULT_BA),
            div_id=NORMALIZED_DIV_ID,
            include_plotlyjs=False,
            height="420px",
        ),
        "DATA": json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c"),
    }
    return re.sub(r"__([A-Z_]+)__", lambda match: values[match.group(1)], PAGE_TEMPLATE)


def output_path(now: datetime) -> Path:
    return paths.OUTPUT_DIR / f"ba-overview-{now.strftime(paths.TIMESTAMP_FORMAT)}.html"


def build(grid_path: Path, annual_path: Path, normalized_path: Path, dest: Path) -> None:
    cells, areas, geojson = load_grid(grid_path)
    years, annual = load_annual_demand(annual_path)
    normalized = load_normalized_demand(normalized_path)
    balancing_areas = require_same_areas(set(areas), set(annual), set(normalized))
    page = render_page(
        cells=cells,
        areas=areas,
        geojson=geojson,
        years=years,
        annual=annual,
        normalized=normalized,
        balancing_areas=balancing_areas,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    print(f"Wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="output HTML file (default: under output/)")
    args = parser.parse_args(argv)
    try:
        dest = args.out or output_path(datetime.now())
        build(
            paths.CODERS_DATA_DIR / "grid_cell_info.csv",
            paths.ba_demand_path(),
            paths.normalized_demand_hourly_path(),
            dest,
        )
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
