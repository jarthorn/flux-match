import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flux_match import paths
from flux_match.config import Config, parse_override
from flux_match.errors import ConfigError

SOURCE = Path("test.toml")

# Fixed fixtures for inheritance tests so they do not depend on scenarios/*.toml content.
FIXED_DEFAULTS = """\
[fetch]
provinces = ["ON", "QC"]
demand_years = [2020, 2021]
capacity_factor_years = [2021]

[selection]
solar_capacity_ratio = 2.5
wind_capacity_ratio = 2.5
max_wind_capacity_mw_per_cell = 5000
max_solar_capacity_mw_per_cell = 5000

[selection.greedy]
ranking_metric = "annual_capacity"

[dispatch]
dispatch_years = [2021]
bess_initial_soc = 0.0
bess_rte = 0.88
bess_capacity_ratio = 0.5
bess_duration = 4
"""

BALANCED_SCENARIO = """\
[selection]
strategy = "greedy"
"""

HIGHWIND_SCENARIO = """\
[selection]
strategy = "greedy"
solar_capacity_ratio = 1.0
wind_capacity_ratio = 4.0
"""

TVCI_SCENARIO = """\
[selection]
strategy = "tvci"

[selection.tvci]
candidate_multiple = 2.0
"""


def config(data: dict) -> Config:
    return Config(data, SOURCE)


class ConfigTest(unittest.TestCase):
    def test_missing_key_reports_dotted_path(self):
        with self.assertRaisesRegex(ConfigError, r"'selection\.greedy\.cap'"):
            config({"selection": {"greedy": {}}}).section("selection").section("greedy").get(
                "cap", float
            )

    def test_int_accepted_where_float_expected(self):
        value = config({"x": 3}).get("x", float)
        self.assertEqual(value, 3.0)
        self.assertIsInstance(value, float)

    def test_bool_rejected_where_number_expected(self):
        for expected in (int, float):
            with self.subTest(expected=expected), self.assertRaises(ConfigError):
                config({"x": True}).get("x", expected)

    def test_wrong_type_rejected(self):
        with self.assertRaisesRegex(ConfigError, "must be float"):
            config({"x": "1.0"}).get("x", float)

    def test_list_elements_type_checked_with_index_in_message(self):
        self.assertEqual(config({"years": [2023, 2024]}).get_list("years", int), [2023, 2024])
        with self.assertRaisesRegex(ConfigError, r"'years\[1\]' must be int"):
            config({"years": [2023, "2024"]}).get_list("years", int)

    def test_list_must_be_non_empty_array(self):
        for value in ([], 2024):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                config({"years": value}).get_list("years", int)

    def test_unique_list_rejects_duplicates(self):
        self.assertEqual(config({"x": [1, 1]}).get_list("x", int), [1, 1])
        with self.assertRaisesRegex(ConfigError, r"'x' has duplicates: \[1\]"):
            config({"x": [1, 2, 1]}).get_list("x", int, unique=True)

    def test_section_must_be_table(self):
        with self.assertRaisesRegex(ConfigError, "must be a table"):
            config({"dispatch": 1}).section("dispatch")


class ParseOverrideTest(unittest.TestCase):
    def test_values_are_toml_literals(self):
        cases = {
            "dispatch.bess_rte=0.9": ("dispatch.bess_rte", 0.9),
            "a.b=4": ("a.b", 4),
            "a=true": ("a", True),
            "a=[2023, 2024]": ("a", [2023, 2024]),
            'a="x=y"': ("a", "x=y"),
            " a = 1 ": ("a", 1),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_override(text), expected)

    def test_non_toml_value_is_a_bare_string(self):
        self.assertEqual(
            parse_override("selection.strategy=cluster"), ("selection.strategy", "cluster")
        )
        self.assertEqual(parse_override("a=x=y"), ("a", "x=y"))

    def test_malformed_overrides_rejected(self):
        for text in ("a", "a=", "=1", "a..b=1", "a.=1", "a={ b = 1 }", "a=2024-01-01"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse_override(text)


class ConfigInheritanceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scenarios = Path(tmp.name)
        self.defaults = self.scenarios / "defaults.toml"
        self.defaults.write_text(FIXED_DEFAULTS)
        self.balanced = self.scenarios / "balanced.toml"
        self.balanced.write_text(BALANCED_SCENARIO)
        self.highwind = self.scenarios / "highwind.toml"
        self.highwind.write_text(HIGHWIND_SCENARIO)
        self.tvci = self.scenarios / "tvci.toml"
        self.tvci.write_text(TVCI_SCENARIO)
        patcher = mock.patch.object(paths, "defaults_path", return_value=self.defaults)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scenario_inherits_undefined_keys_from_defaults(self):
        balanced = Config.load(self.balanced)
        self.assertEqual(balanced.section("selection").get("strategy", str), "greedy")
        self.assertEqual(balanced.section("selection").get("solar_capacity_ratio", float), 2.5)
        self.assertEqual(
            balanced.section("selection").section("greedy").get("ranking_metric", str),
            "annual_capacity",
        )
        self.assertEqual(balanced.section("dispatch").get("bess_duration", int), 4)
        self.assertEqual(balanced.section("fetch").get_list("capacity_factor_years", int), [2021])

    def test_scenario_overrides_replace_defaults(self):
        highwind = Config.load(self.highwind)
        self.assertEqual(highwind.section("selection").get("solar_capacity_ratio", float), 1.0)
        self.assertEqual(highwind.section("selection").get("wind_capacity_ratio", float), 4.0)
        # Unrelated selection keys still come from defaults.
        self.assertEqual(
            highwind.section("selection").get("max_wind_capacity_mw_per_cell", float), 5000.0
        )

    def test_defaults_file_does_not_define_strategy(self):
        defaults = Config.load(self.defaults)
        with self.assertRaisesRegex(ConfigError, "missing required key 'selection.strategy'"):
            defaults.section("selection").get("strategy", str)

    def test_inherit_defaults_false_skips_merge(self):
        path = self.scenarios / "partial.toml"
        path.write_text('[selection]\nstrategy = "greedy"\n')
        config = Config.load(path, inherit_defaults=False)
        with self.assertRaisesRegex(ConfigError, "missing required key 'fetch'"):
            config.section("fetch")

    def test_overrides_take_precedence_over_scenario_and_defaults(self):
        highwind = Config.load(
            self.highwind,
            overrides={"selection.wind_capacity_ratio": 3.0, "dispatch.bess_rte": 0.9},
        )
        # Scenario value overridden, default value overridden, other keys untouched.
        self.assertEqual(highwind.section("selection").get("wind_capacity_ratio", float), 3.0)
        self.assertEqual(highwind.section("dispatch").get("bess_rte", float), 0.9)
        self.assertEqual(highwind.section("selection").get("solar_capacity_ratio", float), 1.0)

    def test_override_of_nested_key_and_snapshot_holds_overridden_value(self):
        tvci = Config.load(self.tvci, overrides={"selection.tvci.candidate_multiple": 3})
        path = self.scenarios / "config.toml"
        path.write_text(tvci.to_toml())
        reloaded = Config.load(path, inherit_defaults=False)
        self.assertEqual(
            reloaded.section("selection").section("tvci").get("candidate_multiple", float), 3.0
        )

    def test_override_of_unknown_key_or_table_fails(self):
        for key in ("dispatch.bess_rtee", "dispatchh.bess_rte", "dispatch.bess_rte.x", "dispatch"):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "override"):
                Config.load(self.balanced, overrides={key: 1})

    def test_to_toml_round_trips_through_load(self):
        original = Config.load(self.tvci)
        path = self.scenarios / "config.toml"
        path.write_text(original.to_toml())
        reloaded = Config.load(path, inherit_defaults=False)
        self.assertEqual(
            reloaded.section("selection").get("strategy", str),
            original.section("selection").get("strategy", str),
        )
        self.assertEqual(
            reloaded.section("selection").section("tvci").get("candidate_multiple", float),
            original.section("selection").section("tvci").get("candidate_multiple", float),
        )
        self.assertEqual(
            reloaded.section("fetch").get_list("provinces", str),
            original.section("fetch").get_list("provinces", str),
        )


if __name__ == "__main__":
    unittest.main()
