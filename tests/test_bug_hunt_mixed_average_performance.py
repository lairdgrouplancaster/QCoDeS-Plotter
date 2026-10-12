"""Mixed native averages retain exact samples without GUI-scale replay."""

from fractions import Fraction

import numpy as np
import pytest

import qplot.windows._native_averaging as averaging
from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem


def test_wider_float_samples_keep_existing_fallback():
    if np.finfo(np.longdouble).nmant <= np.finfo(float).nmant:
        pytest.skip("This platform has no wider floating sample dtype.")
    values = np.array([np.longdouble(1) + np.finfo(np.longdouble).eps])
    assert averaging._binary_integer_samples(values) is None


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("shape_replacement", [False, True])
def test_mixed_average_retains_binary_samples_and_native_weights(
        qapplication, reverse, shape_replacement):
    sources = [
        np.array([1, 2**51, -3, 2**63 - 1], dtype=np.int64),
        np.array([2.1, 2**51 + .5, -.125, .25]),
        np.array([3.2, 2**51 + .5, .0625, .1]),
    ]
    if reverse:
        sources.reverse()
    weights = [2 if shape_replacement else 1, 1, 1]
    expected = [float(sum((weight * Fraction(values[index].item())
                           for weight, values in zip(weights, sources, strict=True)),
                          Fraction()) / sum(weights)) for index in range(4)]
    plot = NativePlotItem()
    try:
        if shape_replacement:
            plot.addItem(NativePlotDataItem(np.arange(2), np.zeros(2)))
        for values in sources:
            source = NativePlotDataItem(np.arange(4), values)
            source.setDynamicRangeLimit(None)
            plot.addItem(source)
        plot.ctrl.averageGroup.setChecked(True)
        count, average = next(iter(plot.avgCurves.values()))
        assert count == sum(weights)
        np.testing.assert_array_equal(average.getData()[1], expected)
        plot.recomputeAverages()
        np.testing.assert_array_equal(next(iter(plot.avgCurves.values()))[1].getData()[1], expected)
    finally:
        plot.clear()


@pytest.mark.parametrize("fractional", [False, True])
def test_large_mixed_traces_avoid_per_cell_fraction_replay(qapplication, monkeypatch, fractional):
    # The previous implementation made 600,000 finite_mean calls for this
    # ordinary GUI action, synchronously blocking the Qt event loop for seconds.
    def forbidden_replay(_values):
        pytest.fail("Ordinary mixed traces replayed Fraction means on the GUI thread")

    monkeypatch.setattr(averaging, "finite_mean", forbidden_replay)
    count = 300_000
    values = [1, 2.1 if fractional else 2., 3.2 if fractional else 3.]
    plot = NativePlotItem()
    try:
        for value in values:
            plot.addItem(NativePlotDataItem(np.arange(count), np.full(count, value)))
        plot.ctrl.averageGroup.setChecked(True)
        average = next(iter(plot.avgCurves.values()))[1]
        expected = float(sum(map(Fraction, values)) / len(values))
        np.testing.assert_array_equal(average.getData()[1], np.full(count, expected))
    finally:
        plot.clear()
