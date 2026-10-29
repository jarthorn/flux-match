import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path

from flux_match.errors import DataError, FluxMatchError
from flux_match.io import write_csv
from flux_match.run_context import create_run
from flux_match.schema import FIRST_HOUR_AFTER_LEAP_DAY, HOURS_PER_YEAR
from flux_match.viz.app import _select_run_dir
from flux_match.viz.charts import (
    DEFAULT_AUTHORITY,
    build_figure,
    default_authority,
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
        self.assertEqual(table.authorities, ("East.a", "West.a"))
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

    def test_default_authority_prefers_ontario(self) -> None:
        self.assertEqual(default_authority(("Alberta.a", DEFAULT_AUTHORITY)), DEFAULT_AUTHORITY)
        self.assertEqual(default_authority(("Alberta.a", "Quebec.a")), "Alberta.a")

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
            ctx.vre_hourly_path,
            ("balancing_area", "hour", "wind_mw", "solar_mw"),
            hourly_rows({"East.a": {"wind_mw": [10, 11, 12], "solar_mw": [0, 1, 2]}}),
        )
        write_csv(
            ctx.dispatch_hourly_path(2024),
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
        self.assertEqual(figure.layout.uirevision, "East.a|2024")
        self.assertEqual(figure.layout.xaxis.anchor, "y3")
        domains = [
            figure.layout.yaxis.domain,
            figure.layout.yaxis2.domain,
            figure.layout.yaxis3.domain,
        ]
        for previous, current in zip(domains[:-1], domains[1:], strict=True):
            self.assertLessEqual(current[1], previous[0])


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
