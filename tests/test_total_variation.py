import unittest

import numpy as np

from flux_match.errors import DataError
from flux_match.schema import HOURS_PER_YEAR
from flux_match.selection.total_variation import (
    total_variation,
    total_variation_complementarity,
)


def reference_phi(f1: list[float], f2: list[float]) -> float:
    """Direct, loop-based transcription of eqs. 4 and 5 to check the vectorised code against."""

    def tv(f: list[float]) -> float:
        return sum(abs(f[k + 1] - f[k]) for k in range(len(f) - 1))

    return 1.0 - tv([a + b for a, b in zip(f1, f2, strict=True)]) / (tv(f1) + tv(f2))


def phi(f1: list[float], f2: list[float]) -> float:
    return float(total_variation_complementarity(f1, [f2])[0])


class TotalVariationTest(unittest.TestCase):
    def test_sums_absolute_steps(self) -> None:
        self.assertEqual(float(total_variation([0.0, 0.5, 0.25, 1.0])), 0.5 + 0.25 + 0.75)

    def test_constant_series_is_zero(self) -> None:
        self.assertEqual(float(total_variation([0.3] * 10)), 0.0)

    def test_monotonic_series_is_end_to_end_difference(self) -> None:
        self.assertAlmostEqual(float(total_variation([0.0, 0.1, 0.4, 0.9])), 0.9)

    def test_scales_with_absolute_constant(self) -> None:
        series = np.array([0.0, 1.0, 0.5, 0.75])
        scaled = float(total_variation(-3 * series))
        self.assertAlmostEqual(scaled, 3 * float(total_variation(series)))

    def test_two_d_gives_one_value_per_row(self) -> None:
        result = total_variation([[0.0, 1.0, 0.0], [0.0, 0.0, 0.5]])
        self.assertEqual(result.tolist(), [2.0, 0.5])

    def test_rejects_single_sample(self) -> None:
        with self.assertRaises(ValueError):
            total_variation([1.0])


class ComplementarityIndexTest(unittest.TestCase):
    def test_opposite_series_are_perfectly_complementary(self) -> None:
        self.assertAlmostEqual(phi([0.0, 1.0, 0.0, 1.0], [1.0, 0.0, 1.0, 0.0]), 1.0)

    def test_identical_series_have_zero_complementarity(self) -> None:
        self.assertAlmostEqual(phi([0.0, 1.0, 0.25, 0.75], [0.0, 1.0, 0.25, 0.75]), 0.0)

    def test_partial_complementarity_hand_computed(self) -> None:
        # TV(f1)=3, TV(f2)=1, f1+f2=[0,1,1,2] has TV=2, so phi = 1 - 2/4.
        self.assertAlmostEqual(phi([0.0, 1.0, 0.0, 1.0], [0.0, 0.0, 1.0, 1.0]), 0.5)

    def test_constant_candidate_gives_zero(self) -> None:
        # Adding a constant changes nothing, so TV(sum) = TV(target).
        self.assertAlmostEqual(phi([0.0, 1.0, 0.5, 0.2], [0.4] * 4), 0.0)

    def test_constant_target_gives_zero(self) -> None:
        self.assertAlmostEqual(phi([0.4] * 4, [0.0, 1.0, 0.5, 0.2]), 0.0)

    def test_symmetric_in_its_arguments(self) -> None:
        f1, f2 = [0.1, 0.9, 0.3, 0.6, 0.2], [0.8, 0.2, 0.5, 0.1, 0.7]
        self.assertAlmostEqual(phi(f1, f2), phi(f2, f1))

    def test_unchanged_by_constant_offsets(self) -> None:
        # Eq. 6: phi equals phi of the anomalies, so adding constants has no effect.
        f1, f2 = [0.1, 0.9, 0.3, 0.6, 0.2], [0.8, 0.2, 0.5, 0.1, 0.7]
        shifted = [x + 0.25 for x in f2]
        self.assertAlmostEqual(phi(f1, f2), phi(f1, shifted))

    def test_depends_on_scale(self) -> None:
        # Complementarity of x with 1-x is perfect, but not once 1-x is down-weighted.
        f1, f2 = [0.0, 1.0, 0.0, 1.0], [1.0, 0.0, 1.0, 0.0]
        self.assertAlmostEqual(phi(f1, [0.5 * x for x in f2]), 1.0 - 0.5 / 1.5)

    def test_each_candidate_scored_independently(self) -> None:
        rng = np.random.default_rng(1)
        target = rng.random(50)
        candidates = rng.random((7, 50))
        batch = total_variation_complementarity(target, candidates)
        self.assertEqual(batch.shape, (7,))
        for i, candidate in enumerate(candidates):
            self.assertAlmostEqual(batch[i], reference_phi(target.tolist(), candidate.tolist()))

    def test_no_candidates_gives_empty_result(self) -> None:
        result = total_variation_complementarity([0.0, 1.0, 0.0], np.empty((0, 3)))
        self.assertEqual(result.shape, (0,))

    def test_full_year_with_values_slightly_above_one_stays_in_range(self) -> None:
        rng = np.random.default_rng(2)
        target = rng.random(HOURS_PER_YEAR) * 1.02
        candidates = rng.random((20, HOURS_PER_YEAR)) * 1.02
        result = total_variation_complementarity(target, candidates)
        self.assertTrue(((result >= 0.0) & (result <= 1.0)).all())
        # Independent noise lies between "identical" and "opposite".
        self.assertTrue(((result > 0.1) & (result < 0.5)).all())

    def test_mirrored_series_beats_identical_and_independent(self) -> None:
        rng = np.random.default_rng(3)
        target = rng.random(500)
        candidates = np.stack([target, rng.random(500), 1.0 - target])
        result = total_variation_complementarity(target, candidates)
        self.assertLess(result[0], result[1])
        self.assertLess(result[1], result[2])
        self.assertAlmostEqual(result[2], 1.0)

    def test_does_not_modify_inputs(self) -> None:
        target = np.array([0.0, 1.0, 0.5])
        candidates = np.array([[1.0, 0.0, 0.5]])
        total_variation_complementarity(target, candidates)
        self.assertEqual(target.tolist(), [0.0, 1.0, 0.5])
        self.assertEqual(candidates.tolist(), [[1.0, 0.0, 0.5]])


class ComplementarityIndexErrorTest(unittest.TestCase):
    def test_both_constant_is_undefined_and_names_the_candidate(self) -> None:
        candidates = [[0.0, 1.0, 0.0], [0.3, 0.3, 0.3]]
        with self.assertRaisesRegex(DataError, r"undefined.*\[1\]"):
            total_variation_complementarity([0.5, 0.5, 0.5], candidates)

    def test_nan_in_target_fails(self) -> None:
        with self.assertRaisesRegex(DataError, "target"):
            total_variation_complementarity([0.0, np.nan, 1.0], [[0.0, 1.0, 0.0]])

    def test_nan_in_candidate_names_the_row(self) -> None:
        candidates = [[0.0, 1.0, 0.0], [0.0, np.inf, 0.0]]
        with self.assertRaisesRegex(DataError, r"candidate.*\[1\]"):
            total_variation_complementarity([0.0, 1.0, 0.0], candidates)

    def test_length_mismatch_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "shape"):
            total_variation_complementarity([0.0, 1.0, 0.0], [[0.0, 1.0]])

    def test_candidates_must_be_two_dimensional(self) -> None:
        with self.assertRaisesRegex(ValueError, "shape"):
            total_variation_complementarity([0.0, 1.0, 0.0], [0.0, 1.0, 0.0])

    def test_target_must_be_one_dimensional(self) -> None:
        with self.assertRaisesRegex(ValueError, "1-D"):
            total_variation_complementarity([[0.0, 1.0, 0.0]], [[0.0, 1.0, 0.0]])


if __name__ == "__main__":
    unittest.main()
