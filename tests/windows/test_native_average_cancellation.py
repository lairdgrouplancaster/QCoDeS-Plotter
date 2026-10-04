"""Generated averages retain source cancellation and native nonfinite rules."""

from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem
from tests.windows.test_native_averaging import click_average


@pytest.mark.parametrize('samples', [
    [1e16, 1., -1e16], [-1e16, 1., 1e16],
    [1e308, 1., -1e308], [2**60 + 1, -2**60],
])
def test_native_average_matches_source_oracle(qapplication, samples):
    plot = NativePlotItem()
    try:
        sources = [NativePlotDataItem(np.arange(4), np.full(4, v)) for v in samples]
        for source in sources:
            plot.addItem(source)
        click_average(SimpleNamespace(plot=plot))
        expected = float(sum(Fraction(v) for v in samples) / len(samples))
        for _ in range(2):
            count, average = next(iter(plot.avgCurves.values()))
            assert count == len(samples)
            np.testing.assert_array_equal(average.getData()[1], np.full(4, expected))
            plot.recomputeAverages()
        for source, value in zip(sources, samples, strict=True):
            np.testing.assert_array_equal(source.getOriginalDataset()[1], np.full(4, value))
    finally:
        plot.clear()


def test_average_nonfinite_and_shape_replacement(qapplication):
    plot = NativePlotItem()
    try:
        for values in ([9., 9.], [1e16, np.nan, np.inf, -np.inf],
                       [1., 2., 1., -1.], [-2e16, 3., -np.inf, -1.]):
            source = NativePlotDataItem(np.arange(len(values)), np.array(values))
            source.setDynamicRangeLimit(None)
            plot.addItem(source)
        click_average(SimpleNamespace(plot=plot))
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 4
        np.testing.assert_array_equal(average.getData()[1], [.25, np.nan, np.nan, -np.inf])
    finally:
        plot.clear()
