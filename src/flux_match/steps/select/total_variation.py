"""Total Variation Complementarity Index between time series.

Implements sections 2.1 and 2.2 of "Total Variation-Based Metrics for Assessing Complementarity
in Energy Resources Time Series" (Sustainability 2022, 14, 8514). Citation:

  Cantor, D.; Ochoa, A.; Mesa, O.
  Total Variation-Based Metrics for Assessing Complementarity in Energy Resources Time Series.
  Sustainability 2022, 14, 8514.
  https://doi.org/10.3390/su14148514

For a time series sampled at consecutive points, the total variation is the sum of the absolute
changes between neighbouring samples (eq. 4):

    TV(f) = sum_k |f[k+1] - f[k]|

and the complementarity index of two series is (eq. 5):

    phi(f1, f2) = 1 - TV(f1 + f2) / (TV(f1) + TV(f2))

By the triangle inequality TV(f1 + f2) <= TV(f1) + TV(f2), so 0 <= phi <= 1. phi = 0 means the
changes in the two series fully reinforce each other, and phi = 1 means the sum is constant (the
series cancel each other perfectly). The index is undefined when both series are constant.

Note that phi is not scale-invariant: it weighs the two series by their given magnitudes. Callers
that want to evaluate a weighted combination (e.g. a candidate site joining a portfolio of
several sites) should scale the inputs to those weights.
"""

import numpy as np
from numpy.typing import ArrayLike, NDArray

from flux_match.errors import DataError


def total_variation(series: ArrayLike) -> NDArray[np.float64]:
    """Total variation along the last axis (eq. 4). A 1-D series gives a scalar-like 0-d
    array; a 2-D array gives one value per row."""
    values = np.asarray(series, dtype=np.float64)
    if values.ndim == 0 or values.shape[-1] < 2:
        raise ValueError(f"need at least 2 samples per series, got shape {values.shape}")
    return np.abs(np.diff(values, axis=-1)).sum(axis=-1)


def total_variation_complementarity(
    target: ArrayLike, candidates: ArrayLike
) -> NDArray[np.float64]:
    """Complementarity index phi (eq. 5) between `target` and each candidate series.

    `target` has shape (T,) and `candidates` has shape (n, T); the result has shape (n,), where
    element i is phi(target, candidates[i]). Higher means more complementary. Series are
    compared sample for sample, so they must cover the same hours in the same order. They are
    typically hourly capacity factors (T = 8760, values in 0-1, possibly slightly above 1), but
    any real values are accepted.

    Raises ValueError for misshapen input, and DataError for non-finite values or when the
    index is undefined (the target and a candidate are both constant).
    """
    target_values = np.asarray(target, dtype=np.float64)
    candidate_values = np.asarray(candidates, dtype=np.float64)
    if target_values.ndim != 1:
        raise ValueError(f"target must be 1-D, got shape {target_values.shape}")
    if candidate_values.ndim != 2 or candidate_values.shape[1] != target_values.shape[0]:
        raise ValueError(
            f"candidates must have shape (n, {target_values.shape[0]}) to match the target, "
            f"got {candidate_values.shape}"
        )
    if not np.isfinite(target_values).all():
        raise DataError("target time series contains NaN or infinite values")
    bad_rows = np.flatnonzero(~np.isfinite(candidate_values).all(axis=1))
    if bad_rows.size:
        raise DataError(
            f"candidate time series contain NaN or infinite values at row index(es) "
            f"{bad_rows.tolist()}"
        )

    separate = total_variation(target_values) + total_variation(candidate_values)
    undefined = np.flatnonzero(separate == 0.0)
    if undefined.size:
        raise DataError(
            "complementarity index is undefined when both series are constant; "
            f"target and candidate row index(es) {undefined.tolist()} are all constant"
        )
    combined = total_variation(target_values + candidate_values)
    # The triangle inequality bounds the ratio by 1, but rounding can push it a few ulps
    # past the bounds (e.g. identical series), so clamp to the documented range.
    return np.clip(1.0 - combined / separate, 0.0, 1.0)
