"""Finite sample summaries without offset loss or intermediate overflow."""

import math
from fractions import Fraction

import numpy as np


def _scalar(value):
    return value.item() if isinstance(value, np.generic) else value


def sample_fraction(value):
    """Retain a recorded scalar exactly, including NumPy extended floats."""
    if isinstance(value, np.floating):
        # longdouble.item() can remain a NumPy scalar, which Fraction does
        # not accept. Its integer ratio also preserves wider mantissas.
        return Fraction(*value.as_integer_ratio())
    return Fraction(_scalar(value))


def finite_mean(values, *, check_cancelled=lambda: None):
    """Mean finite recorded samples, retaining integer and decimal precision."""
    values = np.asarray(values)

    def samples():
        for index, value in enumerate(values.flat):
            if index % 1024 == 0:
                check_cancelled()
            yield _scalar(value)

    if values.dtype.kind in "iuO":
        return float(sum((sample_fraction(v) for v in samples()), Fraction()) / values.size)
    try:
        return math.fsum(map(float, samples())) / values.size
    except OverflowError:
        # The sum can exceed float's range even though its mean cannot.
        return float(sum((Fraction(float(v)) for v in samples()), Fraction()) / values.size)


def finite_standard_deviation(values):
    """Population deviation with subtraction before conversion and scaling."""
    values = np.asarray(values).ravel()
    if values.dtype.kind in "iuO":
        anchor = sample_fraction(values[0])
        try:
            shifted = np.array([float(sample_fraction(v) - anchor) for v in values])
        except OverflowError:
            shifted = np.full(values.shape, np.inf)
    else:
        values = values.astype(np.float64, copy=False)
        with np.errstate(over="ignore", invalid="ignore"):
            shifted = values - values[0]
    if not np.all(np.isfinite(shifted)):
        # Opposite finite extremes can have unrepresentable differences.
        scale = float(np.max(np.abs(values.astype(float))))
        normalized = values.astype(float) / scale
    else:
        scale = float(np.max(np.abs(shifted)))
        if scale == 0:
            return 0.
        normalized = shifted / scale
    centered = normalized - finite_mean(normalized)
    variance = math.fsum(float(v) * float(v) for v in centered) / values.size
    return math.sqrt(variance) * scale
