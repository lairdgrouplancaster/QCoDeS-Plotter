"""Independent rational oracles for finite selection summaries."""

from decimal import Decimal

import numpy as np
import pytest

from qplot.tools.sample_statistics import finite_mean, finite_standard_deviation
from tests.support.tools.sample_statistics_accuracy import (
    summary_oracle as summary_oracle,
)


@pytest.mark.parametrize("values", [
    pytest.param(np.arange(2**60, 2**60 + 4, dtype=np.int64), id="large-signed-integers"),
    pytest.param(np.arange(2**64 - 4, 2**64, dtype=np.uint64), id="large-unsigned-integers"),
    pytest.param(np.array([1e200, -1e200]), id="large-opposite-floats"),
    pytest.param(np.array([1e-200, -1e-200]), id="tiny-opposite-floats"),
    pytest.param(np.array([1e16, 1., -1e16]), id="cancellation-residual"),
    pytest.param(np.full(3, np.finfo(float).max), id="maximum-finite-floats"),
    pytest.param(np.array([1e308, -1e308, -1e308]), id="overflowing-differences"),
    pytest.param(np.array([1, 1.25], dtype=np.longdouble), id="numpy-longdouble"),
    pytest.param(np.array([Decimal('1e308'), Decimal('-1e308'), Decimal('-1e308')], dtype=object),
                 id="decimal-extremes"),
    pytest.param(np.array([Decimal(2**60), Decimal(2**60) + Decimal('.25')], dtype=object),
                 id="decimal-small-variation"),
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
