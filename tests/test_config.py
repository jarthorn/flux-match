import unittest
from pathlib import Path

from flux_match.config import Config
from flux_match.errors import ConfigError

SOURCE = Path("test.toml")


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

    def test_section_must_be_table(self):
        with self.assertRaisesRegex(ConfigError, "must be a table"):
            config({"dispatch": 1}).section("dispatch")


if __name__ == "__main__":
    unittest.main()
