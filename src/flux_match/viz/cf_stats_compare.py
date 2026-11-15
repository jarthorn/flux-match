"""Compare two cf-stats CSV outputs and write a standalone HTML report.

    uv run --group viz python -m flux_match.viz.cf_stats_compare SAMPLE_A.csv SAMPLE_B.csv

Reads hourly population std columns for one balancing area plus ``all_cells`` from each file,
writes a quartile summary table and two Plotly time-series charts (CDN plotly.js). Filenames must
match ``cf-stats-{type}-{year}-{source}-{YYYYMMDD-HHMM}.csv``.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go

from flux_match import paths
from flux_match.errors import DataError, FluxMatchError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.viz.cf_stats import ALL_CELLS_COLUMN, GENERATION_TYPES, HOUR_COLUMN, WIND_SOURCES

# Focus columns for this report; change here to analyse a different balancing area.
BALANCING_AREA = "British Columbia.a"

HOURS_PER_DAY = 24
SOURCE_DISPLAY = {"coders": "CODERS", "ninja": "renewables.ninja"}
SAMPLE_COLORS = {"A": "#1f6f8b", "B": "#c45c26"}

# Non-leap year (cf-stats years are 8760 hours with Feb 29 dropped).
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
assert sum(DAYS_IN_MONTH) * HOURS_PER_DAY == HOURS_PER_YEAR

FILENAME_RE = re.compile(
    r"^cf-stats-(?P<generation_type>wind|solar)-(?P<year>\d{4})-"
    r"(?P<source>coders|ninja)-(?P<timestamp>\d{8}-\d{4})$"
)

INTRO_TEMPLATE = (
    "This report compares capacity factor data across two datasets. Each source dataset contains "
    "the hourly capacity factor for a single variable renewable energy source (wind or solar) "
    "for each MERRA-2 map cell across the Canadian provinces. We examine the standard "
    "deviation across a region's map cells for each hour. For both wind and solar power, we should "
    "expect to see periods of high variation as insolation and wind patterns move across the area. "
    "We examine the standard deviation across both a single province (British Columbia), and "
    "across all Canadian provinces."
)

CONCLUSION_TEMPLATE = (
    "Summarize the main differences between Sample A and Sample B here. Note patterns in "
    "the summary box plot and the time-series charts for {balancing_area} and all cells."
)

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --bg: #f7f6f2;
      --ink: #1a1f24;
      --muted: #4a5560;
      --rule: #d5d1c8;
      --panel: #ffffff;
      --accent-a: #1f6f8b;
      --accent-b: #c45c26;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
      color: var(--ink);
      background:
        radial-gradient(ellipse at top left, #ebe6dc 0%, transparent 50%),
        linear-gradient(180deg, #faf8f4 0%, var(--bg) 40%, #efece5 100%);
      line-height: 1.55;
    }
    main {
      max-width: 960px;
      margin: 0 auto;
      padding: 2.5rem 1.5rem 4rem;
    }
    h1 {
      font-size: 2rem;
      font-weight: 600;
      letter-spacing: -0.02em;
      margin: 0 0 0.75rem;
    }
    h2 {
      font-size: 1.25rem;
      font-weight: 600;
      margin: 2.25rem 0 0.75rem;
      padding-bottom: 0.35rem;
      border-bottom: 1px solid var(--rule);
    }
    h3 {
      font-size: 1.05rem;
      font-weight: 600;
      margin: 1.5rem 0 0.5rem;
    }
    .lede {
      font-size: 1.1rem;
      color: var(--muted);
      max-width: 42rem;
      margin: 0 0 1.5rem;
    }
    .section-body {
      color: var(--muted);
      max-width: 42rem;
      margin: 0 0 1rem;
    }
    .samples {
      display: grid;
      gap: 0.85rem;
      margin: 0 0 1.5rem;
    }
    .sample {
      background: var(--panel);
      border: 1px solid var(--rule);
      border-left-width: 4px;
      border-radius: 4px;
      padding: 0.85rem 1rem;
      box-shadow: 0 1px 0 rgba(26, 31, 36, 0.04);
    }
    .sample.a { border-left-color: var(--accent-a); }
    .sample.b { border-left-color: var(--accent-b); }
    .sample strong { display: block; margin-bottom: 0.45rem; }
    .sample .meta {
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.85rem;
      color: var(--muted);
      margin: 0;
    }
    .sample .meta dt {
      display: inline;
      font-weight: 600;
      color: var(--ink);
    }
    .sample .meta dd {
      display: inline;
      margin: 0 1.25rem 0 0.35rem;
    }
    .chart {
      background: var(--panel);
      border: 1px solid var(--rule);
      border-radius: 4px;
      padding: 0.5rem 0.5rem 0.25rem;
      margin: 0 0 2rem;
      box-shadow: 0 1px 0 rgba(26, 31, 36, 0.04);
    }
  </style>
</head>
<body>
  <main>
    <h1>__TITLE__</h1>
    <h2>Introduction</h2>
    <p class="section-body">__INTRO__</p>
    <h2>Datasets</h2>
    <p>
       The following datasets are examined in this report:
    </p>
    <div class="samples">
      <div class="sample a">
        <strong>Sample A</strong>
        __SAMPLE_A__
      </div>
      <div class="sample b">
        <strong>Sample B</strong>
        __SAMPLE_B__
      </div>
    </div>

    <h2>Descriptive Statistics Summary</h2>
    <p class="section-body">
       The following box plot compares the distribution of hourly standard deviations for each
       sample, for __BA_LABEL__ and across all cells.
    </p>
    <div class="chart">__CHART_SUMMARY__</div>

    <h2>Graphs</h2>
    <h3>Capacity factor standard deviation in __BA_LABEL__</h3>
    <p>
       The following chart shows the standard deviation of the capacity factor across cells in a
       single balancing area, per hour of the year. British Columbia is chosen here because it is
       a large geographical area where we should expect variation in wind patterns across the
       province in any given hour, from the windy coast to the interior where there is
       much less wind.
       For solar capacity factor, we should see variation during daylight hours as the sun passes
       over the province, but zero variation overnight.
    </p>
    <div class="chart">__CHART_BA__</div>
    <h3>Capacity factor standard deviation across Canada</h3>
    <p>
       As a reference, this chart shows the standard deviation of the capacity factor across all of
       Canada for each hour of the year. We should see high variation in most hours for wind
       capacity factor, and high variation during daylight hours for solar capacity factor.
    </p>
    <div class="chart">__CHART_ALL__</div>
    <h2>Conclusion</h2>
    <p class="section-body">__CONCLUSION__</p>
  </main>
</body>
</html>
"""


@dataclass(frozen=True)
class SampleMeta:
    path: Path
    generation_type: str
    year: int
    source: str
    timestamp: str

    @property
    def source_label(self) -> str:
        return SOURCE_DISPLAY[self.source]

    def meta_list_html(self) -> str:
        return (
            '<dl class="meta">'
            f"<dt>Generation type:</dt><dd>{self.generation_type}</dd>"
            f"<dt>Year:</dt><dd>{self.year}</dd>"
            f"<dt>Data source:</dt><dd>{self.source_label}</dd>"
            "</dl>"
        )


@dataclass(frozen=True)
class SampleSeries:
    meta: SampleMeta
    hours: tuple[int, ...]
    balancing_area: tuple[float, ...]
    all_cells: tuple[float, ...]


def month_start_day_of_year() -> list[int]:
    """1-based day of year on which each month starts."""
    starts = [1]
    day = 1
    for days in DAYS_IN_MONTH[:-1]:
        day += days
        starts.append(day)
    return starts


def parse_cf_stats_filename(path: Path) -> SampleMeta:
    """Parse metadata from a cf-stats output filename; hard-fail on mismatch."""
    match = FILENAME_RE.match(path.stem)
    if not match:
        raise DataError(
            f"filename does not match cf-stats-{{type}}-{{year}}-{{source}}-{{YYYYMMDD-HHMM}}: "
            f"{path.name}"
        )
    generation_type = match.group("generation_type")
    source = match.group("source")
    if generation_type not in GENERATION_TYPES:
        raise DataError(f"unknown generation type in filename: {generation_type!r}")
    if source not in WIND_SOURCES:
        raise DataError(f"unknown data source in filename: {source!r}")
    return SampleMeta(
        path=path,
        generation_type=generation_type,
        year=int(match.group("year")),
        source=source,
        timestamp=match.group("timestamp"),
    )


def _parse_float(value: str, *, path: Path, hour: int, column: str) -> float:
    try:
        number = float(value)
    except ValueError as e:
        raise DataError(
            f"{path.name}: hour {hour} column {column!r}: not a number ({value!r})"
        ) from e
    if not math.isfinite(number):
        raise DataError(f"{path.name}: hour {hour} column {column!r}: non-finite value ({value!r})")
    return number


def load_sample(path: Path, balancing_area: str = BALANCING_AREA) -> SampleSeries:
    """Load hour, balancing-area, and all_cells series from a cf-stats CSV."""
    if not path.is_file():
        raise DataError(f"Input file not found: {path}")
    meta = parse_cf_stats_filename(path)
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise DataError(f"{path.name}: missing header row")
        required = (HOUR_COLUMN, balancing_area, ALL_CELLS_COLUMN)
        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            raise DataError(f"{path.name}: missing column(s): {', '.join(missing)}")

        hours: list[int] = []
        ba_values: list[float] = []
        all_values: list[float] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                hour = int(row[HOUR_COLUMN])
            except (KeyError, TypeError, ValueError) as e:
                raise DataError(f"{path.name}: line {row_number}: invalid hour") from e
            expected = len(hours) + 1
            if hour != expected:
                raise DataError(
                    f"{path.name}: expected hour {expected}, got {hour} at line {row_number}"
                )
            hours.append(hour)
            ba_values.append(
                _parse_float(row[balancing_area], path=path, hour=hour, column=balancing_area)
            )
            all_values.append(
                _parse_float(row[ALL_CELLS_COLUMN], path=path, hour=hour, column=ALL_CELLS_COLUMN)
            )

    if len(hours) != HOURS_PER_YEAR:
        raise DataError(f"{path.name}: expected {HOURS_PER_YEAR} hours, got {len(hours)}")
    return SampleSeries(
        meta=meta,
        hours=tuple(hours),
        balancing_area=tuple(ba_values),
        all_cells=tuple(all_values),
    )


def quartile_stats(values: Sequence[float]) -> dict[str, float]:
    """Min, 25th, median, 75th, max for a non-empty series."""
    if not values:
        raise DataError("cannot compute quartiles of an empty series")
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {
        "min": min(values),
        "p25": q1,
        "median": q2,
        "p75": q3,
        "max": max(values),
    }


def hours_to_day_of_year(hours: Sequence[int]) -> list[float]:
    """Map hour 1..8760 to a continuous day-of-year coordinate (hour 1 -> 1.0)."""
    return [(h - 1) / HOURS_PER_DAY + 1.0 for h in hours]


def make_summary_box_figure(
    sample_a: SampleSeries,
    sample_b: SampleSeries,
    balancing_area: str = BALANCING_AREA,
) -> go.Figure:
    """Horizontal box plots of hourly std for both samples and both scopes."""
    # First trace is drawn at the bottom; reverse so Sample A · BA is on top.
    series = [
        (f"Sample A · {balancing_area}", sample_a.balancing_area, SAMPLE_COLORS["A"]),
        (f"Sample B · {balancing_area}", sample_b.balancing_area, SAMPLE_COLORS["B"]),
        ("Sample A · All Cells", sample_a.all_cells, SAMPLE_COLORS["A"]),
        ("Sample B · All Cells", sample_b.all_cells, SAMPLE_COLORS["B"]),
    ]
    fig = go.Figure()
    for label, values, color in reversed(series):
        fig.add_trace(
            go.Box(
                x=list(values),
                name=label,
                orientation="h",
                marker_color=color,
                line={"color": color},
                boxpoints=False,
                showlegend=False,
                hoverinfo="x",
            )
        )
    fig.update_layout(
        margin={"l": 160, "r": 24, "t": 16, "b": 40},
        xaxis={
            "title": "Population std (capacity factor)",
            "showgrid": True,
            "gridcolor": "#e6ebef",
            "zeroline": False,
        },
        yaxis={"automargin": True},
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
    )
    return fig


def _series_trace(
    *,
    days: Sequence[float],
    values: Sequence[float],
    name: str,
    color: str,
) -> go.Scatter:
    return go.Scatter(
        x=list(days),
        y=list(values),
        mode="lines",
        name=name,
        line={"color": color, "width": 1.4},
    )


def make_comparison_figure(
    sample_a: SampleSeries,
    sample_b: SampleSeries,
    *,
    series_attr: str,
    title: str,
    y_title: str = "Population std (capacity factor)",
) -> go.Figure:
    days_a = hours_to_day_of_year(sample_a.hours)
    days_b = hours_to_day_of_year(sample_b.hours)
    values_a = getattr(sample_a, series_attr)
    values_b = getattr(sample_b, series_attr)
    fig = go.Figure(
        data=[
            _series_trace(
                days=days_a,
                values=values_a,
                name="Sample A",
                color=SAMPLE_COLORS["A"],
            ),
            _series_trace(
                days=days_b,
                values=values_b,
                name="Sample B",
                color=SAMPLE_COLORS["B"],
            ),
        ]
    )
    tickvals = month_start_day_of_year()
    fig.update_layout(
        title={"text": title, "x": 0.01, "xanchor": "left"},
        margin={"l": 60, "r": 24, "t": 48, "b": 72},
        legend={"orientation": "h", "yanchor": "top", "y": -0.18, "x": 0},
        xaxis={
            "title": None,
            "tickmode": "array",
            "tickvals": tickvals,
            "ticktext": list(MONTH_NAMES),
            "range": [1, 365],
            "showgrid": True,
            "gridcolor": "#e6ebef",
        },
        yaxis={"title": y_title, "showgrid": True, "gridcolor": "#e6ebef", "zeroline": False},
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        hovermode="x unified",
    )
    return fig


def render_report(
    sample_a: SampleSeries,
    sample_b: SampleSeries,
    balancing_area: str = BALANCING_AREA,
) -> str:
    """Build the full HTML page string."""
    fig_summary = make_summary_box_figure(sample_a, sample_b, balancing_area)
    fig_ba = make_comparison_figure(
        sample_a,
        sample_b,
        series_attr="balancing_area",
        title=f"{balancing_area} — hourly standard deviation",
    )
    fig_all = make_comparison_figure(
        sample_a,
        sample_b,
        series_attr="all_cells",
        title="Canada — hourly standard deviation",
    )
    chart_summary = fig_summary.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        div_id="chart-summary",
        default_width="100%",
        default_height="320px",
        config={"responsive": True},
    )
    chart_ba = fig_ba.to_html(
        include_plotlyjs=False,
        full_html=False,
        div_id="chart-ba",
        default_width="100%",
        default_height="420px",
        config={"responsive": True},
    )
    chart_all = fig_all.to_html(
        include_plotlyjs=False,
        full_html=False,
        div_id="chart-all",
        default_width="100%",
        default_height="420px",
        config={"responsive": True},
    )
    values = {
        "TITLE": "Capacity Factor Statistics Comparison",
        "SAMPLE_A": sample_a.meta.meta_list_html(),
        "SAMPLE_B": sample_b.meta.meta_list_html(),
        "INTRO": INTRO_TEMPLATE.format(balancing_area=balancing_area),
        "BA_LABEL": balancing_area,
        "CHART_SUMMARY": chart_summary,
        "CHART_BA": chart_ba,
        "CHART_ALL": chart_all,
        "CONCLUSION": CONCLUSION_TEMPLATE.format(balancing_area=balancing_area),
    }
    return re.sub(r"__([A-Z_]+)__", lambda m: values[m.group(1)], PAGE_TEMPLATE)


def output_path(now: datetime) -> Path:
    return paths.OUTPUT_DIR / f"cf-stats-compare-{now.strftime(paths.TIMESTAMP_FORMAT)}.html"


def build(path_a: Path, path_b: Path, dest: Path, balancing_area: str = BALANCING_AREA) -> None:
    sample_a = load_sample(path_a, balancing_area)
    sample_b = load_sample(path_b, balancing_area)
    html = render_report(sample_a, sample_b, balancing_area)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html)
    print(f"Wrote {dest}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample_a", type=Path, help="first cf-stats CSV (Sample A)")
    parser.add_argument("sample_b", type=Path, help="second cf-stats CSV (Sample B)")
    parser.add_argument("--out", type=Path, help="output HTML file (default: under output/)")
    args = parser.parse_args(argv)
    try:
        dest = args.out or output_path(datetime.now())
        build(args.sample_a, args.sample_b, dest)
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
