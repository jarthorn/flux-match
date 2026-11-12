import base64
import gzip
import unittest

import numpy as np

from flux_match import paths
from flux_match.errors import DataError
from flux_match.viz.cf_animation import (
    COLOR_BOOST,
    GENERATION_TYPES,
    MONTH_NAMES,
    capacity_factor_path,
    cell_polygons,
    check_spacing,
    color_scale_max,
    hour_label,
    month_start_days,
    pack_frames,
    quantize,
)


class CellPolygonsTest(unittest.TestCase):
    def test_corners_of_known_cell(self):
        geojson = cell_polygons(["7", "8"], [-100.0, -99.375], [50.0, 50.5])
        first, second = geojson["features"]
        self.assertEqual(first["id"], "7")
        self.assertEqual(
            first["geometry"]["coordinates"][0],
            [
                [-100.3125, 49.75],
                [-100.3125, 50.25],
                [-99.6875, 50.25],
                [-99.6875, 49.75],
                [-100.3125, 49.75],
            ],
        )
        self.assertEqual(second["geometry"]["coordinates"][0][0], [-99.6875, 50.25])

    def test_off_lattice_cell_fails(self):
        with self.assertRaisesRegex(DataError, "grid cell 8: longitude"):
            cell_polygons(["7", "8"], [-100.0, -99.5], [50.0, 50.0])
        with self.assertRaisesRegex(DataError, "grid cell 8: latitude"):
            cell_polygons(["7", "8"], [-100.0, -99.375], [50.0, 50.25])

    def test_neighbours_tile_without_gaps(self):
        features = cell_polygons(["1", "2"], [0.0, 0.625], [0.0, 0.0])["features"]
        east_of_first = features[0]["geometry"]["coordinates"][0][2][0]
        west_of_second = features[1]["geometry"]["coordinates"][0][0][0]
        self.assertAlmostEqual(east_of_first, west_of_second)


class SpacingTest(unittest.TestCase):
    def test_expected_spacing_passes(self):
        check_spacing([0.0, 0.625, 1.875], [10.0, 10.5, 12.0])

    def test_unexpected_spacing_fails(self):
        with self.assertRaisesRegex(DataError, "longitude spacing"):
            check_spacing([0.0, 1.25], [10.0, 10.5])
        with self.assertRaisesRegex(DataError, "latitude spacing"):
            check_spacing([0.0, 0.625], [10.0, 11.0])


class CapacityFactorPathTest(unittest.TestCase):
    def test_wind_defaults_to_coders(self):
        self.assertEqual(
            capacity_factor_path("wind", 2021),
            paths.CODERS_DATA_DIR / "wind_capacity_factor_2021.csv",
        )

    def test_wind_ninja_uses_ninja_dir(self):
        self.assertEqual(
            capacity_factor_path("wind", 2021, wind_source="ninja"),
            paths.NINJA_DATA_DIR / "wind_capacity_factor_2021.csv",
        )

    def test_solar_always_uses_coders(self):
        for wind_source in ("coders", "ninja"):
            self.assertEqual(
                capacity_factor_path("solar", 2021, wind_source=wind_source),
                paths.CODERS_DATA_DIR / "solar_capacity_factor_2021.csv",
            )

    def test_invalid_wind_source_fails(self):
        with self.assertRaisesRegex(ValueError, "wind_source must be one of"):
            capacity_factor_path("wind", 2021, wind_source="nrel")


class QuantizeTest(unittest.TestCase):
    def test_endpoints(self):
        result = quantize(np.array([0.0, 1.0]))
        self.assertEqual(result.dtype, np.uint8)
        self.assertEqual(result.tolist(), [0, 255])

    def test_rounds_to_nearest_with_halves_up(self):
        # 0.5 * 255 = 127.5 exactly, so it is a true midpoint; 0.25 * 255 = 63.75.
        values = np.array([0.4 / 255, 0.6 / 255, 0.25, 0.5, 1.4 / 255, 1.6 / 255])
        self.assertEqual(quantize(values).tolist(), [0, 1, 64, 128, 1, 2])

    def test_out_of_range_fails(self):
        for bad in (-0.001, 1.001, float("nan")):
            with self.assertRaises(DataError):
                quantize(np.array([0.5, bad]))


class PackFramesTest(unittest.TestCase):
    def test_round_trips_in_hour_major_order(self):
        frames = np.arange(12, dtype=np.uint8).reshape(3, 4)
        raw = gzip.decompress(base64.b64decode(pack_frames(frames)))
        self.assertEqual(raw, bytes(range(12)))

    def test_non_uint8_fails(self):
        with self.assertRaises(ValueError):
            pack_frames(np.zeros((2, 2), dtype=np.float64))


class ColorScaleTest(unittest.TestCase):
    def test_wind_uses_full_scale_and_solar_is_boosted_20_percent(self):
        self.assertEqual(color_scale_max("wind"), 1.0)
        self.assertAlmostEqual(color_scale_max("solar"), 1 / 1.2)

    def test_every_generation_type_has_a_boost(self):
        for generation_type in GENERATION_TYPES:
            self.assertGreaterEqual(COLOR_BOOST[generation_type], 1.0)


class HourLabelTest(unittest.TestCase):
    def test_known_hours(self):
        self.assertEqual(hour_label(1), "Jan 1 00:00-01:00 UTC")
        self.assertEqual(hour_label(24), "Jan 1 23:00-24:00 UTC")
        self.assertEqual(hour_label(25), "Jan 2 00:00-01:00 UTC")
        self.assertEqual(hour_label(1417), "Mar 1 00:00-01:00 UTC")
        self.assertEqual(hour_label(1416), "Feb 28 23:00-24:00 UTC")
        self.assertEqual(hour_label(4387), "Jul 2 18:00-19:00 UTC")
        self.assertEqual(hour_label(8760), "Dec 31 23:00-24:00 UTC")

    def test_out_of_range_fails(self):
        for bad in (0, 8761):
            with self.assertRaises(ValueError):
                hour_label(bad)

    def test_month_starts_match_labels(self):
        starts = month_start_days()
        self.assertEqual(len(starts), 12)
        for name, day in zip(MONTH_NAMES, starts, strict=True):
            self.assertEqual(hour_label(day * 24 + 1), f"{name} 1 00:00-01:00 UTC")
            if day:
                self.assertNotEqual(hour_label(day * 24)[:3], name)


if __name__ == "__main__":
    unittest.main()
