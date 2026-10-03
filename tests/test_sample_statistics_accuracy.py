"""Independent rational oracles for finite selection summaries."""

from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import pytest

from qplot.tools.sample_statistics import finite_mean, finite_standard_deviation


def summary_oracle(values):
    exact = [Fraction(v.item() if isinstance(v, np.generic) else v) for v in values.flat]
    mean = sum(exact) / len(exact)
    variance = sum((v - mean) ** 2 for v in exact) / len(exact)
    with localcontext() as context:
        context.prec = 90
        deviation = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
    return float(mean), float(deviation)


@pytest.mark.parametrize("values", [
    np.arange(2**60, 2**60 + 4, dtype=np.int64),
    np.arange(2**64 - 4, 2**64, dtype=np.uint64),
    np.array([1e200, -1e200]),
    np.array([1e-200, -1e-200]),
    np.array([1e16, 1., -1e16]),
    np.full(3, np.finfo(float).max),
    np.array([1e308, -1e308, -1e308]),
    np.array([Decimal('1e308'), Decimal('-1e308'), Decimal('-1e308')], dtype=object),
    np.array([Decimal(2**60), Decimal(2**60) + Decimal('.25')], dtype=object),
])
def test_finite_summaries_match_exact_sample_oracle(values):
    original = values.copy()
    mean, deviation = summary_oracle(values)
    assert finite_mean(values) == mean
    assert finite_standard_deviation(values) == pytest.approx(deviation, rel=2e-15, abs=0)
    np.testing.assert_array_equal(values, original)


def test_group_mean_cancels_during_large_group():
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise InterruptedError('cancelled')

    with pytest.raises(InterruptedError):
        finite_mean(np.ones(8192), check_cancelled=cancel)
