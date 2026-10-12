"""Native averages must round the mean of recorded integers only once."""

from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem
from tests.windows.test_native_averaging import click_average


@pytest.mark.parametrize("baseline", [2**40, 2**51, 2**52, 2**53, 2**63])
@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("mixed_float", [False, True])
def test_same_sign_integer_averages_round_once(qapplication, baseline, sign, mixed_float):
    if sign < 0 and baseline == 2**63:
        pytest.skip("Negative values exceed int64 in this case.")
    values = [sign * baseline, sign * (baseline + 1), sign * (baseline + 1)]
    arrays = [np.full(4, value, dtype=np.uint64 if value >= 2**63 else np.int64)
              for value in values]
    if mixed_float:
        arrays[0] = arrays[0].astype(float)
    expected = float(sum(Fraction(array[0].item()) for array in arrays) / len(arrays))
    plot = NativePlotItem()
    try:
        sources = [NativePlotDataItem(np.arange(4), array) for array in arrays]
        for source in sources:
            plot.addItem(source)
        click_average(SimpleNamespace(plot=plot))
        for _ in range(2):
            count, average = next(iter(plot.avgCurves.values()))
            assert count == 3
            np.testing.assert_array_equal(average.getData()[1], np.full(4, expected))
            plot.recomputeAverages()
        for source, array in zip(sources, arrays, strict=True):
            np.testing.assert_array_equal(source.getOriginalDataset()[1], array)
    finally:
        plot.clear()


@pytest.mark.parametrize("baseline", [2**52, 2**62])
def test_integer_average_shape_replacement_keeps_native_weights(qapplication, baseline):
    plot = NativePlotItem()
    try:
        for size, value in [(2, 0), (4, baseline), (4, baseline + 1), (4, baseline + 1)]:
            plot.addItem(NativePlotDataItem(np.arange(size), np.full(size, value, dtype=np.int64)))
        click_average(SimpleNamespace(plot=plot))
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 4
        expected = float(Fraction(4 * baseline + 2, 4))
        np.testing.assert_array_equal(average.getData()[1], np.full(4, expected))
    finally:
        plot.clear()


def test_integer_average_total_cannot_overflow_or_wrap(qapplication):
    values = [np.uint64(2**64 - 1), np.int64(-(2**63)), np.int64(-(2**63) + 4)]
    plot = NativePlotItem()
    try:
        for value in values:
            plot.addItem(NativePlotDataItem(np.arange(4), np.full(4, value, dtype=value.dtype)))
        click_average(SimpleNamespace(plot=plot))
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 3
        np.testing.assert_array_equal(average.getData()[1], np.ones(4))
    finally:
        plot.clear()


def test_mixed_fractional_average_below_integer_precision_boundary(qapplication):
    baseline = 2**51
    values = [baseline, baseline + .5, baseline + .5]
    plot = NativePlotItem()
    try:
        for value in values:
            plot.addItem(NativePlotDataItem(np.arange(4), np.full(4, value)))
        click_average(SimpleNamespace(plot=plot))
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 3
        expected = float(sum(Fraction(value) for value in values) / 3)
        np.testing.assert_array_equal(average.getData()[1], np.full(4, expected))
    finally:
        plot.clear()
