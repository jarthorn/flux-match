"""Animated map of one year of hourly capacity factors, as one self-contained HTML file.

    uv run --group viz python -m flux_match.viz.cf_animation --type wind|solar [--year Y]

Each grid cell is drawn as a rectangle on a Plotly map of Canada, coloured green (low) to red
(capacity factor 1.0). All 8760 hours are embedded once as base64 `uint8` values (capacity
factor * 255, rounded), and a small inline JS player restyles the map's single choropleth
trace for the current hour. See docs/cf-animation.md.
"""

import argparse
import base64
import json
import math
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from flux_match.errors import DataError, FluxMatchError
from flux_match.grid_cell_rankings import (
    iter_hourly_capacity_factors,
    read_grid_cells,
    read_locations,
)
from flux_match.paths import CODERS_DATA_DIR, OUTPUT_DIR, TIMESTAMP_FORMAT
from flux_match.schema import HOURS_PER_YEAR

HOURS_PER_DAY = 24
GENERATION_TYPES = ("wind", "solar")

# CODERS grid spacing, in degrees.
LON_STEP = 0.625
LAT_STEP = 0.5
LATTICE_TOLERANCE = 1e-6  # in grid steps

# The 8760-hour year has no Feb 29.
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
assert sum(DAYS_IN_MONTH) * HOURS_PER_DAY == HOURS_PER_YEAR

COLORSCALE = [[0.0, "#1a9641"], [0.5, "#ffffbf"], [1.0, "#d7191c"]]
# Colours are stretched by this factor so that types with low capacity factors (solar rarely
# exceeds 0.5) use more of the palette. Zero stays at the bottom; values above 1 / boost are
# drawn as the top colour. Hover text always shows the actual capacity factor.
COLOR_BOOST = {"wind": 1.0, "solar": 1.2}
MAP_HEIGHT_PX = 720


def month_start_days() -> list[int]:
    """Zero-based day of the year on which each month starts."""
    starts = [0]
    for days in DAYS_IN_MONTH[:-1]:
        starts.append(starts[-1] + days)
    return starts


def hour_label(hour: int) -> str:
    """The UTC interval covered by `hour` (1-8760, hour-ending), e.g. `Jan 1 00:00-01:00 UTC`."""
    if not 1 <= hour <= HOURS_PER_YEAR:
        raise ValueError(f"hour must be in 1..{HOURS_PER_YEAR}, got {hour}")
    day, hour_of_day = divmod(hour - 1, HOURS_PER_DAY)
    starts = month_start_days()
    month = max(m for m, start in enumerate(starts) if start <= day)
    return (
        f"{MONTH_NAMES[month]} {day - starts[month] + 1} "
        f"{hour_of_day:02d}:00-{hour_of_day + 1:02d}:00 UTC"
    )


def color_scale_max(generation_type: str) -> float:
    """The capacity factor drawn in the top colour."""
    return 1.0 / COLOR_BOOST[generation_type]


def quantize(capacity_factors: np.ndarray) -> np.ndarray:
    """Map capacity factors in [0, 1] to uint8 0-255, rounding halves up."""
    values = np.asarray(capacity_factors, dtype=np.float64)
    if values.size and not ((values >= 0.0) & (values <= 1.0)).all():  # also rejects NaN
        raise DataError("capacity factors to quantize must be in 0-1")
    return np.floor(values * 255.0 + 0.5).astype(np.uint8)


def cell_polygons(
    cells: Sequence[str],
    longitudes: Sequence[float],
    latitudes: Sequence[float],
    lon_step: float = LON_STEP,
    lat_step: float = LAT_STEP,
) -> dict[str, Any]:
    """GeoJSON FeatureCollection with one rectangle per cell, centred on its location.

    Every cell must sit on the lattice of `lon_step` x `lat_step` anchored at the first cell;
    anything else is a hard failure, since the rectangles would overlap or leave gaps.
    """
    if not cells:
        raise DataError("no grid cells to draw")
    lon0, lat0 = longitudes[0], latitudes[0]
    features = []
    for cell, lon, lat in zip(cells, longitudes, latitudes, strict=True):
        for name, offset, step in (
            ("longitude", lon - lon0, lon_step),
            ("latitude", lat - lat0, lat_step),
        ):
            steps = offset / step
            if abs(steps - round(steps)) > LATTICE_TOLERANCE:
                raise DataError(
                    f"grid cell {cell}: {name} is not on the {step} degree lattice of cell "
                    f"{cells[0]} (longitude {lon}, latitude {lat})"
                )
        west, east = lon - lon_step / 2, lon + lon_step / 2
        south, north = lat - lat_step / 2, lat + lat_step / 2
        # Clockwise exterior ring, as expected by d3-geo (which Plotly uses).
        ring = [[west, south], [west, north], [east, north], [east, south], [west, south]]
        features.append(
            {
                "type": "Feature",
                "id": cell,
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def infer_spacing(values: Sequence[float]) -> float:
    """Smallest gap between distinct coordinate values."""
    ordered = sorted(set(values))
    if len(ordered) < 2:
        raise DataError("cannot infer grid spacing from fewer than two distinct coordinates")
    return min(b - a for a, b in zip(ordered, ordered[1:], strict=False))


def check_spacing(longitudes: Sequence[float], latitudes: Sequence[float]) -> None:
    """Hard fail unless the grid cells are spaced as CODERS documents."""
    for name, inferred, expected in (
        ("longitude", infer_spacing(longitudes), LON_STEP),
        ("latitude", infer_spacing(latitudes), LAT_STEP),
    ):
        if not math.isclose(inferred, expected, abs_tol=1e-6):
            raise DataError(
                f"grid_cell_info.csv: {name} spacing is {inferred}, expected {expected} degrees"
            )


def capacity_factor_path(generation_type: str, year: int) -> Path:
    return CODERS_DATA_DIR / f"{generation_type}_capacity_factor_{year}.csv"


def default_year(generation_type: str) -> int:
    """The only capacity-factor year available for `generation_type` in data/coders/."""
    paths = sorted(CODERS_DATA_DIR.glob(f"{generation_type}_capacity_factor_*.csv"))
    years = []
    for path in paths:
        suffix = path.stem.rsplit("_", 1)[-1]
        if not suffix.isdigit():
            raise DataError(f"Cannot read the year from {path.name}")
        years.append(int(suffix))
    if len(years) != 1:
        raise DataError(
            f"Expected exactly one {generation_type}_capacity_factor_<year>.csv in "
            f"{CODERS_DATA_DIR}, found {years or 'none'}; choose one with --year"
        )
    return years[0]


def load_frames(path: Path, cells: Sequence[str]) -> np.ndarray:
    """uint8 array of shape (8760, len(cells)): quantized capacity factors per hour."""
    frames = np.empty((HOURS_PER_YEAR, len(cells)), dtype=np.uint8)
    for hour_index, values in enumerate(iter_hourly_capacity_factors(path, cells)):
        frames[hour_index] = quantize(np.array(values))
    return frames


def build_figure(
    geojson: dict[str, Any],
    cells: Sequence[str],
    areas: Sequence[str],
    first_hour: np.ndarray,
    title: str,
    color_max: float = 1.0,
) -> Any:
    """Choropleth of `first_hour` (uint8) with the colour scale spanning 0 to `color_max`.

    The plotted values, and so the hover text, stay the actual capacity factors.
    """
    import plotly.graph_objects as go

    fig = go.Figure(
        go.Choropleth(
            geojson=geojson,
            locations=list(cells),
            z=(first_hour / 255.0).tolist(),
            text=list(areas),
            zmin=0,
            zmax=color_max,
            colorscale=COLORSCALE,
            marker_line_width=0,
            colorbar={"title": "Capacity factor"},
            hovertemplate="Cell %{location}<br>%{text}<br>CF %{z:.3f}<extra></extra>",
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
    fig.update_layout(title=title, height=MAP_HEIGHT_PX, margin={"l": 0, "r": 0, "t": 40, "b": 0})
    return fig


PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body { font-family: sans-serif; margin: 0 1em; }
  #controls { display: flex; gap: 0.5em; align-items: center; flex-wrap: wrap; margin: 0.5em 0; }
  #controls button { min-width: 4em; }
  #label { font-variant-numeric: tabular-nums; margin-left: 1em; }
  #slider-box { position: relative; padding: 0 8px; }
  #slider { width: 100%; margin: 0; }
  #months { position: relative; height: 1.5em; font-size: 0.8em; color: #555; }
  #months span { position: absolute; transform: translateX(-50%); }
</style>
</head>
<body>
__FIGURE__
<div id="controls">
  <button id="play">Play</button>
  <button id="back-day" title="Back 1 day">-1 d</button>
  <button id="back-hour" title="Back 1 hour (Left arrow)">-1 h</button>
  <button id="fwd-hour" title="Forward 1 hour (Right arrow)">+1 h</button>
  <button id="fwd-day" title="Forward 1 day">+1 d</button>
  <label>Speed <select id="speed"></select> hours/s</label>
  <span id="label"></span>
</div>
<div id="slider-box">
  <input id="slider" type="range" min="1" max="__HOURS__" step="1" value="1">
  <div id="months"></div>
</div>
<script id="cf-config" type="application/json">__CONFIG__</script>
<script id="cf-data" type="text/plain">__DATA__</script>
<script>
__PLAYER_JS__
</script>
</body>
</html>
"""

PLAYER_JS = """
(function () {
  const config = JSON.parse(document.getElementById("cf-config").textContent);
  const N = config.hours, cells = config.cells, speeds = config.speeds;
  const binary = atob(document.getElementById("cf-data").textContent);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  if (bytes.length !== N * cells) {
    throw new Error("cf-data has " + bytes.length + " bytes, expected " + N * cells);
  }

  const gd = document.getElementById("cfmap");
  const $ = (id) => document.getElementById(id);
  const slider = $("slider"), playButton = $("play"), speedSelect = $("speed");
  let hour = 1, playing = false, generation = 0;

  for (const s of speeds) {
    const isDefault = s === config.default_speed;
    speedSelect.add(new Option(String(s), String(s), isDefault, isDefault));
  }
  config.month_start_days.forEach((day, m) => {
    const span = document.createElement("span");
    span.textContent = config.month_names[m];
    span.style.left = (day * 24 / (N - 1) * 100) + "%";
    $("months").appendChild(span);
  });

  function labelFor(h) {
    const day = Math.floor((h - 1) / 24), hod = (h - 1) % 24;
    let month = 0;
    while (month + 1 < 12 && config.month_start_days[month + 1] <= day) month++;
    const pad = (n) => String(n).padStart(2, "0");
    return "Hour " + h + " - " + config.month_names[month] + " " +
      (day - config.month_start_days[month] + 1) + " " + pad(hod) + ":00-" + pad(hod + 1) +
      ":00 UTC";
  }

  // Rendering: at most one Plotly.restyle in flight, and at most one per animation frame.
  let rendering = false, idleWaiters = [];
  function requestRender() {
    if (rendering) return;
    rendering = true;
    requestAnimationFrame(renderLatest);
  }
  function renderLatest() {
    const shown = hour;
    const offset = (shown - 1) * cells;
    const z = new Array(cells);
    for (let i = 0; i < cells; i++) z[i] = bytes[offset + i] / 255;
    Plotly.restyle(gd, { z: [z] }).then(() => {
      if (hour !== shown) {
        requestAnimationFrame(renderLatest);
      } else {
        rendering = false;
        const waiters = idleWaiters;
        idleWaiters = [];
        waiters.forEach((resolve) => resolve());
      }
    });
  }
  function whenIdle() {
    return rendering ? new Promise((resolve) => idleWaiters.push(resolve)) : Promise.resolve();
  }

  function setHour(h) {
    hour = Math.min(N, Math.max(1, h));
    slider.value = hour;
    $("label").textContent = labelFor(hour);
    requestRender();
  }

  function setPlaying(value) {
    playing = value;
    playButton.textContent = playing ? "Pause" : "Play";
  }
  async function play() {
    if (playing) return;
    if (hour >= N) setHour(1);
    setPlaying(true);
    const mine = ++generation;
    while (playing && mine === generation && hour < N) {
      const started = performance.now();
      setHour(hour + 1);
      await whenIdle();
      // The next frame is scheduled only after this one has rendered, so a slow render lowers
      // the frame rate instead of queueing frames. Render time counts towards the interval.
      const wait = Math.max(0, 1000 / Number(speedSelect.value) - (performance.now() - started));
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
    if (mine === generation) setPlaying(false);
  }
  function pause() {
    generation++;
    setPlaying(false);
  }

  playButton.onclick = () => (playing ? pause() : play());
  $("back-day").onclick = () => setHour(hour - 24);
  $("back-hour").onclick = () => setHour(hour - 1);
  $("fwd-hour").onclick = () => setHour(hour + 1);
  $("fwd-day").onclick = () => setHour(hour + 24);
  slider.oninput = () => setHour(Number(slider.value));
  document.addEventListener("keydown", (e) => {
    const tag = e.target.tagName;
    if (e.key === " " && tag !== "BUTTON" && tag !== "SELECT") {
      e.preventDefault();
      playing ? pause() : play();
    } else if (
      (e.key === "ArrowLeft" || e.key === "ArrowRight") && tag !== "INPUT" && tag !== "SELECT"
    ) {
      e.preventDefault();
      setHour(hour + (e.key === "ArrowRight" ? 1 : -1));
    }
  });

  $("label").textContent = labelFor(hour);
})();
"""

SPEEDS = (0.5, 1, 2, 4, 8, 16)
DEFAULT_SPEED = 1


def render_page(fig: Any, frames: np.ndarray, title: str) -> str:
    """The full HTML page: the figure, controls, base64 frame data and the player script."""
    n_hours, n_cells = frames.shape
    config = {
        "hours": n_hours,
        "cells": n_cells,
        "speeds": list(SPEEDS),
        "default_speed": DEFAULT_SPEED,
        "month_names": list(MONTH_NAMES),
        "month_start_days": month_start_days(),
    }
    figure_html = fig.to_html(include_plotlyjs=True, full_html=False, div_id="cfmap")
    data = base64.b64encode(np.ascontiguousarray(frames).tobytes()).decode("ascii")
    values = {
        "TITLE": title,
        "HOURS": str(n_hours),
        "CONFIG": json.dumps(config),
        "PLAYER_JS": PLAYER_JS,
        "FIGURE": figure_html,
        "DATA": data,
    }
    # One pass, so substituted text (e.g. plotly.js) is never searched for placeholders.
    return re.sub(r"__([A-Z_]+)__", lambda m: values[m.group(1)], PAGE_TEMPLATE)


def output_path(generation_type: str, now: datetime) -> Path:
    return OUTPUT_DIR / f"cf-animation-{generation_type}-{now.strftime(TIMESTAMP_FORMAT)}.html"


def build_animation(generation_type: str, year: int, dest: Path) -> None:
    cells, areas = read_grid_cells(CODERS_DATA_DIR / "grid_cell_info.csv")
    locations = read_locations(CODERS_DATA_DIR / "grid_cell_info.csv")
    latitudes = [locations[c][0] for c in cells]
    longitudes = [locations[c][1] for c in cells]
    check_spacing(longitudes, latitudes)
    geojson = cell_polygons(cells, longitudes, latitudes)

    print(f"Reading {generation_type} capacity factors for {year}...")
    frames = load_frames(capacity_factor_path(generation_type, year), cells)

    title = f"Hourly {generation_type} capacity factor, {year}"
    fig = build_figure(geojson, cells, areas, frames[0], title, color_scale_max(generation_type))
    page = render_page(fig, frames, title)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    print(f"Wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", dest="generation_type", required=True, choices=GENERATION_TYPES)
    parser.add_argument(
        "--year", type=int, help="capacity-factor year (default: the only one in data/coders/)"
    )
    parser.add_argument("--out", type=Path, help="output HTML file (default: under output/)")
    args = parser.parse_args(argv)
    try:
        year = args.year if args.year is not None else default_year(args.generation_type)
        dest = args.out or output_path(args.generation_type, datetime.now())
        build_animation(args.generation_type, year, dest)
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
