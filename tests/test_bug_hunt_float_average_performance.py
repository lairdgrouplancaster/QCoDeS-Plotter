"""Ordinary floating trace averages must not replay each displayed cell."""

from fractions import Fraction

import numpy as np
import pytest

import qplot.windows._native_averaging as averaging
from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("first_negative", [1, 6])
@pytest.mark.parametrize("missing", [False, True])
def test_large_mixed_sign_float_average_avoids_per_cell_replay(
        qapplication, monkeypatch, replacement, first_negative, missing):
    def forbidden_replay(_values):
        raise AssertionError("Ordinary floating traces replayed each cell on the GUI thread")

    monkeypatch.setattr(averaging, "finite_mean", forbidden_replay)
    values = [(-1.) ** index * (1. + index / 10) for index in range(12)]
    values[:first_negative] = [abs(value) for value in values[:first_negative]]
    values[first_negative] = -abs(values[first_negative])
    weights = [2 if replacement else 1, *([1] * 11)]
    expected = float(sum(weight * Fraction(value) for weight, value in zip(weights, values, strict=True))
                     / sum(weights))
    plot = NativePlotItem()
    try:
        if replacement:
            plot.addItem(NativePlotDataItem(np.arange(7), np.zeros(7)))
        for value in values:
            samples = np.full(300_000, value)
            if missing:
                samples[0] = np.nan
            plot.addItem(NativePlotDataItem(np.arange(300_000), samples))
        # Run the actual average action synchronously so failure is a pytest
        # assertion instead of an unhandled Qt signal exception.
        plot.ctrl.averageGroup.blockSignals(True)
        plot.ctrl.averageGroup.setChecked(True)
        plot.ctrl.averageGroup.blockSignals(False)
        plot.recomputeAverages()
        count, average = next(iter(plot.avgCurves.values()))
        assert count == sum(weights)
        expected_values = np.full(300_000, expected)
        if missing:
            expected_values[0] = np.nan
        np.testing.assert_array_equal(average.getData()[1], expected_values)
        for source, value in zip(plot._average_display_sources()[-12:], values, strict=True):
            expected_values = np.full(300_000, value)
            if missing:
                expected_values[0] = np.nan
            np.testing.assert_array_equal(source[0].getOriginalDataset()[1], expected_values)
    finally:
        plot.ctrl.averageGroup.setChecked(False)
        plot.clear()


@pytest.mark.parametrize("replacement", [False, True])
def test_float_average_keeps_extreme_dynamic_range_fallback(qapplication, replacement):
    values = [np.array([1e308, 1e-308]), np.array([-1e308, 2e-308]), np.array([1., -3e-308])]
    weights = [2 if replacement else 1, 1, 1]
    plot = NativePlotItem()
    try:
        if replacement:
            source = NativePlotDataItem(np.arange(1), np.zeros(1))
            plot.addItem(source)
            plot.addAvgCurve(source)
        for samples in values:
            source = NativePlotDataItem(np.arange(2), samples)
            source.setDynamicRangeLimit(None)
            plot.addItem(source)
            plot.addAvgCurve(source)
        count, average = next(iter(plot.avgCurves.values()))
        expected = [float(sum(weight * Fraction(float(samples[index]))
                              for weight, samples in zip(weights, values, strict=True)) / sum(weights))
                    for index in range(2)]
        assert count == sum(weights)
        np.testing.assert_array_equal(average.getData()[1], expected)
    finally:
        plot.clear()


@pytest.mark.parametrize("last", [np.nan, np.inf, -np.inf])
def test_nonfinite_float_trace_invalidates_previously_promoted_total(qapplication, last):
    plot = NativePlotItem()
    try:
        for value in (1., -1., last):
            source = NativePlotDataItem(np.arange(2), np.full(2, value))
            source.setDynamicRangeLimit(None)
            plot.addItem(source)
            plot.addAvgCurve(source)
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 3
        np.testing.assert_array_equal(average.getData()[1], np.full(2, last))
        assert average._qplot_integer_total is None
    finally:
        plot.clear()


def test_clear_skips_discarded_averages_but_single_removal_and_reuse_work(qapplication, monkeypatch):
    plot = NativePlotItem()
    try:
        sources = [NativePlotDataItem(np.arange(3), np.full(3, value)) for value in (1., -2., 4.)]
        for source in sources:
            plot.addItem(source)
        plot.ctrl.averageGroup.setChecked(True)
        plot.removeItem(sources[-1])
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 2
        np.testing.assert_array_equal(average.getData()[1], [-.5] * 3)
        plot._queue_view_averages()
        assert plot._qplot_average_refresh_timer.isActive()
        with monkeypatch.context() as context:
            def forbidden_average(_source):
                raise AssertionError("clear recalculated an average being discarded")

            context.setattr(plot, "addAvgCurve", forbidden_average)
            plot.clear()
        assert plot.items == [] and plot.avgCurves == {}
        assert plot._qplot_average_display_sources == ()
        assert not plot._qplot_average_refresh_timer.isActive()
        assert plot.ctrl.averageGroup.isChecked()
        assert not plot._qplot_recomputing_averages
        plot.addItem(NativePlotDataItem(np.arange(3), np.full(3, 7.)))
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 1
        np.testing.assert_array_equal(average.getData()[1], [7.] * 3)
    finally:
        plot.clear()
