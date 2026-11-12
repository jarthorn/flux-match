import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path

from flux_match import paths
from flux_match.errors import DataError, FluxMatchError
from flux_match.io import write_csv
from flux_match.run_context import create_run
from flux_match.schema import FIRST_HOUR_AFTER_LEAP_DAY, HOURS_PER_YEAR
from flux_match.viz.app import _select_run_dir
from flux_match.viz.charts import (
    DEFAULT_BALANCING_AREA,
    ZOOM_OPTIONS,
    Resolution,
    Zoom,
    aggregate_panels,
    buckets_for,
    build_figure,
    default_balancing_area,
    display_times_for,
    hour_to_display_time,
    load_dashboard_tables,
    load_hourly_table,
    panels_for,
    parse_mw_cell,
    require_aligned,
    values,
)

HOURS = 3
SCENARIO = """\
[fetch]
provinces = ["ON"]
demand_years = [2024]
capacity_factor_years = [2021]
[selection]
strategy = "greedy"
solar_capacity_ratio = 1.0
wind_capacity_ratio = 1.0
max_wind_capacity_mw_per_cell = 1000
max_solar_capacity_mw_per_cell = 500
[selection.greedy]
ranking_metric = "median_daily_capacity"
[dispatch]
dispatch_years = [2024]
bess_initial_soc = 0.0
bess_rte = 0.88
bess_capacity_ratio = 0.2
bess_duration = 4
"""


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class ParseMwCellTest(unittest.TestCase):
    def test_integer_and_whole_float_accepted(self) -> None:
        self.assertEqual(parse_mw_cell("839"), 839)
        self.assertEqual(parse_mw_cell("9809.0"), 9809)
        self.assertEqual(parse_mw_cell("-12"), -12)

    def test_fractional_mw_kept_as_float(self) -> None:
        self.assertEqual(parse_mw_cell("1347.2465633539523"), 1347.2465633539523)

    def test_blank_and_non_finite_rejected(self) -> None:
        for value in ("", "nan", "inf", "MW"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_mw_cell(value)


class HourToDisplayTimeTest(unittest.TestCase):
    def test_non_leap_year_maps_hour_1_and_8760_onto_the_calendar(self) -> None:
        self.assertEqual(hour_to_display_time(1, 2023), datetime(2023, 1, 1, 0, 0))
        self.assertEqual(hour_to_display_time(8760, 2023), datetime(2023, 12, 31, 23, 0))
        self.assertEqual(
            hour_to_display_time(FIRST_HOUR_AFTER_LEAP_DAY, 2023), datetime(2023, 3, 1, 0, 0)
        )

    def test_leap_year_skips_february_29(self) -> None:
        self.assertEqual(hour_to_display_time(1416, 2024), datetime(2024, 2, 28, 23, 0))
        self.assertEqual(
            hour_to_display_time(FIRST_HOUR_AFTER_LEAP_DAY, 2024), datetime(2024, 3, 1, 0, 0)
        )
        self.assertEqual(hour_to_display_time(8760, 2024), datetime(2024, 12, 31, 23, 0))

    def test_out_of_range_hour_fails(self) -> None:
        for hour in (0, HOURS_PER_YEAR + 1, 1.0):
            with self.subTest(hour=hour), self.assertRaises(DataError):
                hour_to_display_time(hour, 2024)  # type: ignore[arg-type]

    def test_display_times_use_the_dispatch_year(self) -> None:
        self.assertEqual(
            display_times_for((1, 2), 2024),
            ("2024-01-01 00:00:00", "2024-01-01 01:00:00"),
        )


FULL_YEAR = tuple(range(1, HOURS_PER_YEAR + 1))


class BucketsTest(unittest.TestCase):
    def test_daily_buckets_are_whole_days_with_noon_midpoints(self) -> None:
        buckets = buckets_for(FULL_YEAR, 2023, Resolution.DAILY)
        self.assertEqual(len(buckets), 365)
        self.assertTrue(all(bucket.stop - bucket.start == 24 for bucket in buckets))
        self.assertEqual(buckets[0].midpoint, datetime(2023, 1, 1, 12))
        self.assertEqual(buckets[-1].midpoint, datetime(2023, 12, 31, 12))

    def test_leap_year_skips_february_29(self) -> None:
        days = buckets_for(FULL_YEAR, 2024, Resolution.DAILY)
        self.assertEqual(len(days), 365)
        self.assertEqual(days[58].midpoint, datetime(2024, 2, 28, 12))
        self.assertEqual(days[59].midpoint, datetime(2024, 3, 1, 12))
        self.assertEqual(days[59].start, FIRST_HOUR_AFTER_LEAP_DAY - 1)

        months = buckets_for(FULL_YEAR, 2024, Resolution.MONTHLY)
        self.assertEqual(months[1].stop - months[1].start, 28 * 24)
        self.assertEqual(months[1].midpoint, datetime(2024, 2, 15))
        self.assertEqual(months[2].start, FIRST_HOUR_AFTER_LEAP_DAY - 1)

    def test_monthly_buckets_follow_month_lengths(self) -> None:
        months = buckets_for(FULL_YEAR, 2023, Resolution.MONTHLY)
        self.assertEqual(len(months), 12)
        self.assertEqual(
            [(bucket.stop - bucket.start) // 24 for bucket in months],
            [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31],
        )
        self.assertEqual(months[0].midpoint, datetime(2023, 1, 16, 12))
        self.assertEqual(months[3].midpoint, datetime(2023, 4, 16))
        self.assertEqual(months[-1].stop, HOURS_PER_YEAR)

    def test_partial_bucket_fails(self) -> None:
        with self.assertRaisesRegex(DataError, "2023-01-02 00:00:00 has 23 hours, expected 24"):
            buckets_for(tuple(range(1, 48)), 2023, Resolution.DAILY)
        with self.assertRaisesRegex(DataError, "2023-03-01 00:00:00 has 72 hours, expected 744"):
            buckets_for(tuple(range(1, 744 + 672 + 72 + 1)), 2023, Resolution.MONTHLY)

    def test_gap_in_hours_fails(self) -> None:
        hours = tuple(h for h in FULL_YEAR if h != 100)
        with self.assertRaisesRegex(DataError, "99 is followed by 101"):
            buckets_for(hours, 2023, Resolution.DAILY)


class AggregatePanelsTest(unittest.TestCase):
    def test_hourly_passes_values_through(self) -> None:
        panels = (("Demand", (1, 2), True),)
        times, result = aggregate_panels((1, 2), 2024, panels, Resolution.HOURLY)
        self.assertEqual(times, ("2024-01-01 00:00:00", "2024-01-01 01:00:00"))
        self.assertEqual(result, panels)

    def test_daily_mean_of_each_day(self) -> None:
        hour_of_day = tuple(float((h - 1) % 24) for h in FULL_YEAR)
        day_number = tuple(float((h - 1) // 24) for h in FULL_YEAR)
        times, result = aggregate_panels(
            FULL_YEAR,
            2023,
            (("Solar", hour_of_day, True), ("Demand", day_number, False)),
            Resolution.DAILY,
        )
        self.assertEqual(times[0], "2023-01-01 12:00:00")
        self.assertEqual(set(result[0][1]), {11.5})
        self.assertEqual(result[1][1], tuple(float(day) for day in range(365)))
        self.assertEqual(
            [(title, pin) for title, _, pin in result], [("Solar", True), ("Demand", False)]
        )

    def test_monthly_mean_weights_each_hour_equally(self) -> None:
        # January hours are 1, February hours are 2, ...; one extra spike at the start of March.
        months = buckets_for(FULL_YEAR, 2024, Resolution.MONTHLY)
        values = [0.0] * HOURS_PER_YEAR
        for month, bucket in enumerate(months, start=1):
            values[bucket.start : bucket.stop] = [float(month)] * (bucket.stop - bucket.start)
        values[months[2].start] += 744.0
        times, result = aggregate_panels(
            FULL_YEAR, 2024, (("Demand", tuple(values), True),), Resolution.MONTHLY
        )
        self.assertEqual(times[1], "2024-02-15 00:00:00")
        self.assertEqual(result[0][1][:4], (1.0, 2.0, 4.0, 4.0))

    def test_value_count_must_match_hours(self) -> None:
        with self.assertRaisesRegex(DataError, "Demand has 2 values for 8760 hours"):
            aggregate_panels(FULL_YEAR, 2023, (("Demand", (1, 2), True),), Resolution.DAILY)


def hourly_rows(
    values_by_ba: dict[str, dict[str, list[int]]],
    *,
    year: int | None = None,
) -> list[dict[str, object]]:
    rows = []
    for ba, columns in values_by_ba.items():
        hours = len(next(iter(columns.values())))
        for hour in range(1, hours + 1):
            row: dict[str, object] = {"balancing_area": ba, "hour": hour}
            if year is not None:
                row["year"] = year
            for column, series in columns.items():
                row[column] = series[hour - 1]
            rows.append(row)
    return rows


class LoadHourlyTableTest(TempDirTest):
    def test_loads_long_format_by_balancing_area(self) -> None:
        path = self.dir / "demand.csv"
        write_csv(
            path,
            ("balancing_area", "hour", "demand_mw"),
            hourly_rows(
                {"East.a": {"demand_mw": [10, 20, 30]}, "West.a": {"demand_mw": [1, 2, 3]}}
            ),
        )
        table = load_hourly_table(path, ("demand_mw",), hours_in_year=HOURS)
        self.assertEqual(table.balancing_areas, ("East.a", "West.a"))
        self.assertEqual(table.hours, (1, 2, 3))
        self.assertEqual(values(table, "East.a", "demand_mw"), (10, 20, 30))

    def test_accepts_whole_and_fractional_mw(self) -> None:
        path = self.dir / "demand.csv"
        write_csv(
            path,
            ("balancing_area", "hour", "demand_mw"),
            hourly_rows({"East.a": {"demand_mw": [9809.0, 1347.25, -1.0]}}),
        )
        table = load_hourly_table(path, ("demand_mw",), hours_in_year=HOURS)
        self.assertEqual(values(table, "East.a", "demand_mw"), (9809, 1347.25, -1))

    def test_checks_dispatch_year_on_every_row(self) -> None:
        path = self.dir / "dispatch_hourly_2024.csv"
        write_csv(
            path,
            ("balancing_area", "year", "hour", "battery_output_mw", "net_demand_mw"),
            hourly_rows(
                {"East.a": {"battery_output_mw": [0, 1, -2], "net_demand_mw": [9, 8, 7]}},
                year=2023,
            ),
        )
        with self.assertRaisesRegex(DataError, "year is 2023, expected 2024"):
            load_hourly_table(
                path,
                ("battery_output_mw", "net_demand_mw"),
                hours_in_year=HOURS,
                expected_year=2024,
            )

    def test_missing_hour_and_non_integer_mw_fail(self) -> None:
        path = self.write(
            "demand.csv",
            "balancing_area,hour,demand_mw\nEast.a,1,10\nEast.a,3,30\n",
        )
        with self.assertRaisesRegex(DataError, "expected hour 2, got 3"):
            load_hourly_table(path, ("demand_mw",), hours_in_year=HOURS)

        path = self.write(
            "bad.csv",
            "balancing_area,hour,demand_mw\nEast.a,1,10\nEast.a,2,inf\nEast.a,3,30\n",
        )
        with self.assertRaisesRegex(DataError, "not a number of MW"):
            load_hourly_table(path, ("demand_mw",), hours_in_year=HOURS)

    def test_incomplete_balancing_area_fails(self) -> None:
        path = self.write(
            "demand.csv",
            "balancing_area,hour,demand_mw\nEast.a,1,10\nEast.a,2,20\n",
        )
        with self.assertRaisesRegex(DataError, "East.a has 2 hours, expected 3"):
            load_hourly_table(path, ("demand_mw",), hours_in_year=HOURS)

    def test_non_contiguous_balancing_area_fails(self) -> None:
        path = self.write(
            "demand.csv",
            "balancing_area,hour,demand_mw\n"
            "East.a,1,1\nEast.a,2,2\nEast.a,3,3\n"
            "West.a,1,1\nWest.a,2,2\nWest.a,3,3\n"
            "East.a,1,1\nEast.a,2,2\nEast.a,3,3\n",
        )
        with self.assertRaisesRegex(DataError, "East.a rows are not contiguous"):
            load_hourly_table(path, ("demand_mw",), hours_in_year=HOURS)


class AlignAndPanelsTest(TempDirTest):
    def _table(self, name: str, column: str, series: dict[str, list[int]]):
        path = self.dir / name
        write_csv(
            path,
            ("balancing_area", "hour", column),
            hourly_rows({ba: {column: values} for ba, values in series.items()}),
        )
        return load_hourly_table(path, (column,), hours_in_year=HOURS)

    def test_mismatched_hours_or_areas_fail(self) -> None:
        a = self._table("a.csv", "demand_mw", {"East.a": [1, 2, 3]})
        b = self._table("b.csv", "wind_mw", {"West.a": [1, 2, 3]})
        with self.assertRaisesRegex(DataError, "balancing areas"):
            require_aligned((a, b))

    def test_default_balancing_area_prefers_ontario(self) -> None:
        self.assertEqual(
            default_balancing_area(("Alberta.a", DEFAULT_BALANCING_AREA)), DEFAULT_BALANCING_AREA
        )
        self.assertEqual(default_balancing_area(("Alberta.a", "Quebec.a")), "Alberta.a")

    def test_panels_read_each_source_column(self) -> None:
        demand = self._table("demand.csv", "demand_mw", {"East.a": [100, 110, 120]})
        vre_path = self.dir / "vre.csv"
        write_csv(
            vre_path,
            ("balancing_area", "hour", "wind_mw", "solar_mw"),
            hourly_rows({"East.a": {"wind_mw": [10, 11, 12], "solar_mw": [1, 2, 3]}}),
        )
        vre = load_hourly_table(vre_path, ("wind_mw", "solar_mw"), hours_in_year=HOURS)
        dispatch_path = self.dir / "dispatch.csv"
        write_csv(
            dispatch_path,
            ("balancing_area", "hour", "battery_output_mw", "net_demand_mw"),
            hourly_rows(
                {"East.a": {"battery_output_mw": [0, -5, 4], "net_demand_mw": [89, 102, 101]}}
            ),
        )
        dispatch = load_hourly_table(
            dispatch_path, ("battery_output_mw", "net_demand_mw"), hours_in_year=HOURS
        )
        panels = panels_for("East.a", demand, vre, dispatch)
        self.assertEqual(
            [title for title, _, _ in panels],
            ["Wind generation", "Solar generation", "Demand", "Battery output", "Net demand"],
        )
        self.assertEqual(panels[0][1], (10, 11, 12))
        self.assertEqual(panels[3][1], (0, -5, 4))


class LoadDashboardTablesTest(TempDirTest):
    def test_requires_matching_files_for_the_dispatch_year(self) -> None:
        scenario = self.write("default.toml", SCENARIO)
        ctx = create_run(scenario, self.dir / "output", datetime(2026, 10, 27, 21, 47))
        data = self.dir / "data"
        data.mkdir()
        write_csv(
            data / "balancing_area_demand_hourly_2024.csv",
            ("balancing_area", "hour", "demand_mw"),
            hourly_rows({"East.a": {"demand_mw": [100, 110, 120]}}),
        )
        write_csv(
            paths.vre_hourly_path(ctx),
            ("balancing_area", "hour", "wind_mw", "solar_mw"),
            hourly_rows({"East.a": {"wind_mw": [10, 11, 12], "solar_mw": [0, 1, 2]}}),
        )
        write_csv(
            paths.dispatch_hourly_path(ctx, 2024),
            ("balancing_area", "year", "hour", "battery_output_mw", "net_demand_mw"),
            hourly_rows(
                {"East.a": {"battery_output_mw": [0, 0, 1], "net_demand_mw": [90, 98, 105]}},
                year=2024,
            ),
        )
        demand, vre, dispatch = load_dashboard_tables(ctx, 2024, data, hours_in_year=HOURS)
        self.assertEqual(values(demand, "East.a", "demand_mw"), (100, 110, 120))
        self.assertEqual(values(vre, "East.a", "solar_mw"), (0, 1, 2))
        self.assertEqual(values(dispatch, "East.a", "net_demand_mw"), (90, 98, 105))


class BuildFigureTest(unittest.TestCase):
    def test_shared_x_axis_and_hover_spike_through_every_panel(self) -> None:
        times = ("2024-01-01 00:00:00", "2024-01-01 01:00:00")
        figure = build_figure(
            "East.a",
            2024,
            times,
            (
                ("Wind generation", (1, 2), True),
                ("Demand", (10, 11), True),
                ("Net demand", (9, 9), True),
            ),
        )
        self.assertEqual(len(figure.data), 3)
        self.assertEqual(list(figure.data[0].x), list(times))
        self.assertEqual(list(figure.data[2].x), list(times))
        xaxis = figure.layout.xaxis
        self.assertEqual(xaxis.type, "date")
        self.assertTrue(xaxis.showspikes)
        self.assertEqual(xaxis.spikemode, "across")
        self.assertTrue(xaxis.rangeslider.visible)
        self.assertEqual(figure.layout.hovermode, "x")
        self.assertEqual(figure.layout.hoversubplots, "axis")
        self.assertEqual(figure.layout.uirevision, "East.a|2024|Hourly|Full year")
        self.assertEqual(figure.layout.xaxis.anchor, "y3")
        domains = [
            figure.layout.yaxis.domain,
            figure.layout.yaxis2.domain,
            figure.layout.yaxis3.domain,
        ]
        for previous, current in zip(domains[:-1], domains[1:], strict=True):
            self.assertLessEqual(current[1], previous[0])

    def test_resolution_sets_hover_and_ui_revision(self) -> None:
        panels = (("Demand", (10, 11), True),)
        times = ("2024-01-16 12:00:00", "2024-02-15 00:00:00")
        monthly = build_figure("East.a", 2024, times, panels, Resolution.MONTHLY)
        self.assertIn("%b %Y", monthly.data[0].hovertemplate)
        self.assertEqual(monthly.layout.uirevision, "East.a|2024|Monthly|Full year")
        self.assertIsNone(monthly.layout.xaxis.range)

    def test_zoom_shows_the_last_days_of_the_year(self) -> None:
        panels = (("Demand", (10, 11), True),)
        times = ("2024-12-30 12:00:00", "2024-12-31 12:00:00")
        figure = build_figure("East.a", 2024, times, panels, Resolution.DAILY, Zoom.WEEK)
        self.assertEqual(
            list(figure.layout.xaxis.range), ["2024-12-25 00:00:00", "2025-01-01 00:00:00"]
        )
        self.assertEqual(figure.layout.uirevision, "East.a|2024|Daily|7 days")

    def test_short_zoom_rejected_at_monthly_resolution(self) -> None:
        self.assertEqual(ZOOM_OPTIONS[Resolution.MONTHLY], (Zoom.FULL_YEAR,))
        with self.assertRaisesRegex(DataError, "30 days zoom is not available"):
            build_figure(
                "East.a",
                2024,
                ("2024-01-16 12:00:00",),
                (("Demand", (10,), True),),
                Resolution.MONTHLY,
                Zoom.MONTH,
            )


class SelectRunDirTest(TempDirTest):
    def test_no_argument_prints_notice_and_opens_the_newest_run(self) -> None:
        scenario = self.write("default.toml", SCENARIO)
        create_run(scenario, self.dir / "output", datetime(2026, 10, 27, 21, 30))
        newest = create_run(scenario, self.dir / "output", datetime(2026, 10, 27, 21, 47))
        captured = StringIO()
        with redirect_stdout(captured):
            run_dir = _select_run_dir(["app.py"], self.dir / "output")
        self.assertEqual(run_dir, newest.run_dir)
        self.assertEqual(
            captured.getvalue(),
            "No output specified. Opening dashboard on most recent model run 20261027-2147.\n",
        )

    def test_explicit_folder_is_used_without_a_notice(self) -> None:
        chosen = self.dir / "output" / "chosen"
        captured = StringIO()
        with redirect_stdout(captured):
            run_dir = _select_run_dir(["app.py", str(chosen)], self.dir / "output")
        self.assertEqual(run_dir, chosen)
        self.assertEqual(captured.getvalue(), "")

    def test_extra_arguments_are_rejected(self) -> None:
        with self.assertRaisesRegex(FluxMatchError, "Usage: bin/viz"):
            _select_run_dir(["app.py", "a", "b"], self.dir / "output")


if __name__ == "__main__":
    unittest.main()
