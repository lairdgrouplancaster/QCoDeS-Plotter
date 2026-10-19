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


def _binary_total(values, check_cancelled):
    """Accumulate recorded integers/binary floats without per-cell Fractions."""
    total, denominator = 0, 1
    if values.dtype.kind in "iu":
        flat = values.ravel()
        for start in range(0, values.size, 65536):
            check_cancelled()
            total += np.sum(flat[start:start + 65536], dtype=object)
        return int(total), denominator
    for index, value in enumerate(values.flat):
        if index % 1024 == 0:
            check_cancelled()
        if isinstance(value, (int, np.integer)):
            total += int(value) * denominator
        elif isinstance(value, (float, np.floating)):
            numerator, divisor = value.as_integer_ratio()
            # Binary denominators are powers of two, so their maximum is
            # their common denominator. Neither source nor total is rounded.
            if divisor > denominator:
                total *= divisor // denominator
                denominator = divisor
            total += numerator * (denominator // divisor)
        else:
            return None
    return total, denominator


def finite_mean(values, *, check_cancelled=lambda: None):
    """Mean finite recorded samples, retaining integer and decimal precision."""
    values = np.asarray(values)

    def samples():
        for index, value in enumerate(values.flat):
            if index % 1024 == 0:
                check_cancelled()
            yield value

    # Extended floats also lose recorded residuals when math.fsum converts
    # each input to a Python float before summing. Reuse exact binary totals.
    if values.dtype.kind in "iuO" or (values.dtype.kind == "f" and values.dtype.itemsize > 8):
        exact = _binary_total(values, check_cancelled)
        if exact is not None:
            total, denominator = exact
            check_cancelled()
            try:
                return float(Fraction(total, denominator * values.size))
            except OverflowError:
                # A finite extended mean may exceed the display float range.
                # Retain the ordinary float path's signed infinity result.
                return math.inf if total > 0 else -math.inf
        return float(sum((sample_fraction(v) for v in samples()), Fraction()) / values.size)
    try:
        return math.fsum(samples()) / values.size
    except OverflowError:
        # The sum can exceed float's range even though its mean cannot.
        return float(sum((Fraction(float(v)) for v in samples()), Fraction()) / values.size)


def finite_standard_deviation(values):
    """Population deviation with subtraction before conversion and scaling."""
    values = np.asarray(values).ravel()
    # Subtract extended floating anchors before narrowing to display precision,
    # just as for nearby integer/object samples above float64's precision.
    if values.dtype.kind in "iuO" or (values.dtype.kind == "f" and values.dtype.itemsize > 8):
        anchor = sample_fraction(values[0])
        try:
            if values.dtype.kind in "iu":
                # Python-integer subtraction cannot wrap at signed/unsigned
                # endpoints and converts only the already-centered values.
                shifted = (values.astype(object) - anchor.numerator).astype(float)
            else:
                def difference(value):
                    if isinstance(value, (int, np.integer)):
                        numerator, denominator = int(value), 1
                    elif isinstance(value, (float, np.floating)):
                        numerator, denominator = value.as_integer_ratio()
                    else:
                        return float(sample_fraction(value) - anchor)
                    return (numerator * anchor.denominator - anchor.numerator * denominator) / (
                        denominator * anchor.denominator)
                shifted = np.fromiter((difference(v) for v in values), dtype=float, count=values.size)
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
