import math
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path

from flux_match import paths
from flux_match.config import Config
from flux_match.errors import ConfigError, DataError
from flux_match.io import read_csv
from flux_match.run_context import create_run
from flux_match.schema import DISPATCH_COLUMNS, HOURS_PER_YEAR
from flux_match.steps.dispatch import (
    Battery,
    DispatchParams,
    PairedHour,
    dispatch_hour,
    dispatch_rows,
    paired_hours,
    run,
    size_batteries,
)

HOURS = 3
DISPATCH_SECTION = {
    "dispatch_years": [2024],
    "bess_initial_soc": 0.0,
    "bess_rte": 0.5,
    "bess_capacity_ratio": 0.2,
    "bess_duration": 4,
}


def params(**overrides: object) -> DispatchParams:
    section = Config({"dispatch": {**DISPATCH_SECTION, **overrides}}, Path("test.toml"))
    return DispatchParams.from_config(section.section("dispatch"))


def hourly_csv(column: str, values: dict[str, list[float]]) -> str:
    rows = [
        f"{ba},{hour},{value}"
        for ba, hours in values.items()
        for hour, value in enumerate(hours, start=1)
    ]
    return "\n".join([f"balancing_area,hour,{column}", *rows]) + "\n"


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class DispatchParamsTest(unittest.TestCase):
    def test_default_scenario_is_valid(self) -> None:
        config = Config.load(paths.SCENARIOS_DIR / "default.toml")
        DispatchParams.from_config(config.section("dispatch"))

    def test_integer_duration_accepted_as_float(self) -> None:
        self.assertEqual(params(bess_duration=4).bess_duration, 4.0)

    def test_out_of_range_values_rejected(self) -> None:
        for key, value in [
            ("bess_initial_soc", 1.5),
            ("bess_initial_soc", -0.1),
            ("bess_rte", 0.0),
            ("bess_rte", 1.01),
            ("bess_capacity_ratio", -1.0),
            ("bess_capacity_ratio", math.inf),
            ("bess_duration", 0),
            ("bess_duration", math.nan),
        ]:
            with (
                self.subTest(key=key, value=value),
                self.assertRaisesRegex(ConfigError, f"dispatch.{key}"),
            ):
                params(**{key: value})

    def test_zero_capacity_and_initial_soc_allowed(self) -> None:
        p = params(bess_capacity_ratio=0.0, bess_initial_soc=0.0)
        self.assertEqual(p.bess_capacity_ratio, 0.0)


class SizeBatteriesTest(unittest.TestCase):
    def test_capacity_is_days_of_average_demand_and_power_is_capacity_over_duration(self) -> None:
        # 100 MW average load is 2400 MWh per day.
        got = size_batteries({"East.a": 100.0 * HOURS_PER_YEAR}, 0.5, 4.0)
        self.assertAlmostEqual(got["East.a"].capacity_mwh, 1200.0)
        self.assertAlmostEqual(got["East.a"].power_mw, 300.0)

    def test_negative_annual_demand_fails(self) -> None:
        with self.assertRaisesRegex(DataError, "East.a: annual demand is negative"):
            size_batteries({"East.a": -1.0}, 0.5, 4.0)


class DispatchHourTest(unittest.TestCase):
    battery = Battery(capacity_mwh=100.0, power_mw=50.0)

    def step(self, shortfall: float, soc: float, rte: float = 0.5) -> tuple[float, float]:
        return dispatch_hour(shortfall, soc, self.battery, rte)

    def test_discharge_covers_shortfall(self) -> None:
        self.assertEqual(self.step(30.0, 80.0), (30.0, 50.0))

    def test_discharge_limited_by_power(self) -> None:
        self.assertEqual(self.step(70.0, 80.0), (50.0, 30.0))

    def test_discharge_limited_by_state_of_charge(self) -> None:
        self.assertEqual(self.step(30.0, 10.0), (10.0, 0.0))

    def test_charge_stores_drawn_energy_times_rte(self) -> None:
        self.assertEqual(self.step(-40.0, 0.0), (-40.0, 20.0))

    def test_charge_limited_by_power_on_the_drawn_side(self) -> None:
        self.assertEqual(self.step(-80.0, 0.0), (-50.0, 25.0))

    def test_charge_limited_by_remaining_room_after_losses(self) -> None:
        # 10 MWh of room at 50% efficiency takes 20 MW drawn.
        self.assertEqual(self.step(-40.0, 90.0), (-20.0, 100.0))

    def test_full_battery_is_idle_with_positive_zero_output(self) -> None:
        output, soc = self.step(-40.0, 100.0)
        self.assertEqual(soc, 100.0)
        self.assertEqual(math.copysign(1.0, output), 1.0)
        self.assertEqual(output, 0.0)

    def test_zero_capacity_battery_does_nothing(self) -> None:
        empty = Battery(0.0, 0.0)
        self.assertEqual(dispatch_hour(30.0, 0.0, empty, 0.5), (0.0, 0.0))
        self.assertEqual(dispatch_hour(-30.0, 0.0, empty, 0.5), (0.0, 0.0))

    def test_state_of_charge_never_exceeds_capacity_with_inexact_rte(self) -> None:
        soc = 0.0
        for _ in range(1000):
            _, soc = self.step(-0.7, soc, rte=0.88)
        self.assertLessEqual(soc, self.battery.capacity_mwh)


class DispatchRowsTest(unittest.TestCase):
    def test_battery_resets_per_balancing_area_and_net_demand_subtracts_battery(self) -> None:
        hours = [
            PairedHour("East.a", 1, 10.0, 30.0),  # 20 MW surplus stores 10 MWh
            PairedHour("East.a", 2, 30.0, 0.0),  # 30 MW short, only 10 MWh stored
            PairedHour("West.a", 1, 30.0, 0.0),  # starts again at initial SOC
        ]
        batteries = {"East.a": Battery(100.0, 50.0), "West.a": Battery(100.0, 50.0)}
        rows = list(dispatch_rows(2024, hours, batteries, params(bess_initial_soc=0.25)))
        self.assertEqual([tuple(r) for r in rows], [DISPATCH_COLUMNS] * 3)
        # East.a starts at 25 MWh: stores 10 MWh more, then discharges 30 of the 35 MWh.
        self.assertEqual(rows[0]["battery_output_mw"], -20.0)
        self.assertEqual(rows[0]["net_demand_mw"], 0.0)
        self.assertEqual(rows[1]["battery_output_mw"], 30.0)
        self.assertEqual(rows[1]["net_demand_mw"], 0.0)
        self.assertEqual(rows[2]["battery_output_mw"], 25.0)
        self.assertEqual(rows[2]["net_demand_mw"], 5.0)
        self.assertEqual(rows[2]["year"], 2024)

    def test_surplus_beyond_battery_is_negative_net_demand(self) -> None:
        hours = [PairedHour("East.a", 1, 10.0, 100.0)]
        rows = list(dispatch_rows(2024, hours, {"East.a": Battery(100.0, 50.0)}, params()))
        self.assertEqual(rows[0]["battery_output_mw"], -50.0)
        self.assertEqual(rows[0]["net_demand_mw"], -40.0)

    def test_balancing_area_without_battery_fails(self) -> None:
        hours = [PairedHour("Nowhere.a", 1, 10.0, 0.0)]
        with self.assertRaisesRegex(DataError, "Nowhere.a: no annual demand"):
            list(dispatch_rows(2024, hours, {}, params()))


class PairedHoursTest(TempDirTest):
    def pair(self, demand: str, vre: str) -> list[PairedHour]:
        return list(
            paired_hours(
                self.write("demand.csv", demand), self.write("vre.csv", vre), hours_in_year=HOURS
            )
        )

    def demand(self, values: dict[str, list[float]]) -> str:
        return hourly_csv("demand_mw", values)

    def vre(self, values: dict[str, list[float]]) -> str:
        return hourly_csv("variable_generation_mw", values)

    def test_pairs_rows_from_both_files(self) -> None:
        got = self.pair(
            self.demand({"East.a": [1, 2, 3], "West.a": [4, 5, 6]}),
            self.vre({"East.a": [0.5, 0, 0], "West.a": [0, 0, 7]}),
        )
        self.assertEqual(len(got), 6)
        self.assertEqual(got[0], PairedHour("East.a", 1, 1.0, 0.5))
        self.assertEqual(got[5], PairedHour("West.a", 3, 6.0, 7.0))

    def test_different_balancing_areas_fail(self) -> None:
        with self.assertRaisesRegex(DataError, "row 5: .*West.a hour 1.*North.a hour 1"):
            self.pair(
                self.demand({"East.a": [1, 2, 3], "West.a": [4, 5, 6]}),
                self.vre({"East.a": [0, 0, 0], "North.a": [0, 0, 0]}),
            )

    def test_missing_balancing_area_fails(self) -> None:
        with self.assertRaisesRegex(DataError, "different balancing areas: vre.csv ends"):
            self.pair(
                self.demand({"East.a": [1, 2, 3], "West.a": [4, 5, 6]}),
                self.vre({"East.a": [0, 0, 0]}),
            )

    def test_incomplete_balancing_area_fails(self) -> None:
        with self.assertRaisesRegex(DataError, "demand.csv: East.a has 2 hours, expected 3"):
            self.pair(self.demand({"East.a": [1, 2]}), self.vre({"East.a": [0, 0, 0]}))

    def test_out_of_order_hours_fail(self) -> None:
        text = "balancing_area,hour,demand_mw\nEast.a,1,1\nEast.a,3,1\nEast.a,2,1\n"
        with self.assertRaisesRegex(DataError, "demand.csv:3: East.a: expected hour 2, got 3"):
            self.pair(text, self.vre({"East.a": [0, 0, 0]}))

    def test_non_contiguous_balancing_area_fails(self) -> None:
        text = self.demand({"East.a": [1, 1, 1], "West.a": [1, 1, 1]}) + "East.a,1,1\n"
        with self.assertRaisesRegex(DataError, "East.a rows are not contiguous"):
            self.pair(text, self.vre({"East.a": [0, 0, 0], "West.a": [0, 0, 0], "X": [0]}))

    def test_bad_values_fail(self) -> None:
        for bad in ("abc", "", "nan", "inf"):
            with self.subTest(value=bad):
                text = self.demand({"East.a": [1, 1, 1]}).replace("East.a,2,1", f"East.a,2,{bad}")
                with self.assertRaisesRegex(DataError, "demand.csv:3: .*demand_mw"):
                    self.pair(text, self.vre({"East.a": [0, 0, 0]}))

    def test_empty_file_fails(self) -> None:
        with self.assertRaisesRegex(DataError, "demand.csv: no rows"):
            self.pair("balancing_area,hour,demand_mw\n", self.vre({"East.a": [0, 0, 0]}))


class DispatchRunTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.data_dir = self.dir / "data"
        self.data_dir.mkdir()
        # 100 MW average East.a load: capacity 0.2 days = 480 MWh, power 120 MW.
        (self.data_dir / paths.BA_DEMAND_FILENAME).write_text(
            f"balancing_area,2023,2024\nEast.a,0,{100.0 * HOURS_PER_YEAR}\n"
        )
        (self.data_dir / paths.ba_demand_hourly_filename(2024)).write_text(
            hourly_csv("demand_mw", {"East.a": [100.0, 100.0, 300.0]})
        )
        scenario = self.write(
            "test.toml",
            "[dispatch]\n"
            "dispatch_years = [2024]\n"
            "bess_initial_soc = 0.0\n"
            "bess_rte = 0.5\n"
            "bess_capacity_ratio = 0.2\n"
            "bess_duration = 4\n",
        )
        self.ctx = create_run(scenario, self.dir / "output", datetime(2026, 10, 27, 20, 0))
        self.ctx.vre_hourly_path.write_text(
            "balancing_area,hour,wind_mw,solar_mw,variable_generation_mw\n"
            "East.a,1,300,0,300\nEast.a,2,0,0,0\nEast.a,3,0,0,0\n"
        )

    def _run(self) -> None:
        with redirect_stdout(StringIO()):
            run(self.ctx, data_dir=self.data_dir, hours_in_year=HOURS)

    def test_writes_one_file_per_dispatch_year_sized_from_newest_demand_year(self) -> None:
        self._run()
        rows = read_csv(self.ctx.dispatch_hourly_path(2024), DISPATCH_COLUMNS)
        self.assertEqual([r["hour"] for r in rows], ["1", "2", "3"])
        # Hour 1: 200 MW surplus, charge capped at 120 MW drawn, storing 60 MWh.
        self.assertEqual(float(rows[0]["battery_output_mw"]), -120.0)
        self.assertEqual(float(rows[0]["net_demand_mw"]), -80.0)
        # Hour 2: 100 MW short, covered from the 60 MWh stored.
        self.assertEqual(float(rows[1]["battery_output_mw"]), 60.0)
        self.assertEqual(float(rows[1]["net_demand_mw"]), 40.0)
        self.assertEqual(float(rows[2]["battery_output_mw"]), 0.0)
        self.assertEqual(float(rows[2]["net_demand_mw"]), 300.0)

    def test_missing_hourly_demand_year_fails_with_hint(self) -> None:
        (self.data_dir / paths.ba_demand_hourly_filename(2024)).unlink()
        with self.assertRaisesRegex(DataError, "Add 2024 to fetch.demand_years"):
            self._run()

    def test_mismatched_inputs_leave_no_output_file(self) -> None:
        self.ctx.vre_hourly_path.write_text(
            "balancing_area,hour,variable_generation_mw\nWest.a,1,0\nWest.a,2,0\nWest.a,3,0\n"
        )
        with self.assertRaises(DataError):
            self._run()
        self.assertEqual(sorted(p.name for p in self.ctx.run_dir.glob("dispatch*")), [])


if __name__ == "__main__":
    unittest.main()
