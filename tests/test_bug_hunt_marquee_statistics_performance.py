"""Selection summaries avoid per-sample rational objects on ordinary records."""

from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtCore

from qplot.tools import sample_statistics
from tests.windows.test_numeric_sample_preservation import array_plot


@pytest.mark.parametrize("records_kind", ["integer", "mixed_binary", "mixed_signed"])
def test_real_large_selection_menu_avoids_per_cell_fractions(
        tmp_path, monkeypatch, qapplication, records_kind):
    values = np.arange(300_000, dtype=np.int64)
    if records_kind == "integer":
        records = [{"x": values, "signal": values}]
    else:
        first, second = np.split(values, 2)
        second_values = (second.astype(float) + .5 if records_kind == "mixed_binary"
                         else second.astype(np.uint64))
        records = [{"x": first, "signal": first}, {"x": second, "signal": second_values}]
    with array_plot(tmp_path, monkeypatch, records) as (_main, plot):
        original_x, original_y = (values.copy() for values in plot.line.getOriginalDataset())
        if records_kind != "integer":
            assert original_y.dtype.kind == "O"
        plot.set_marquee_rect(QtCore.QRectF(-1., -1., 300_001., 300_001.))
        calls = 0
        original_fraction = sample_statistics.sample_fraction

        def fraction(value):
            nonlocal calls
            calls += 1
            return original_fraction(value)

        monkeypatch.setattr(sample_statistics, "sample_fraction", fraction)
        menu = plot._new_marquee_context_menu()
        assert calls <= 1
        assert any(action.text() == "Stats..." and action.isEnabled() for action in menu.actions())
        selected = plot._marquee_line_values()
        assert selected.size == values.size
        expected = original_y.astype(float)
        text = plot._marquee_stats_text()
        assert f"Average: {plot.formatNum(np.mean(expected))}" in text
        assert f"Standard deviation: {plot.formatNum(np.std(expected))}" in text
        for actual, recorded in zip(plot.line.getOriginalDataset(), (original_x, original_y), strict=True):
            np.testing.assert_array_equal(actual, recorded)


@pytest.mark.parametrize("dtype", [np.int64, np.uint64])
def test_large_integer_mean_cancels_between_bounded_exact_sums(monkeypatch, dtype):
    values = np.arange(150_000, dtype=dtype)
    consumed = 0
    original_sum = np.sum

    def observe_sum(values, *args, **kwargs):
        nonlocal consumed
        consumed += values.size
        return original_sum(values, *args, **kwargs)

    def cancel():
        if consumed:
            raise InterruptedError("cancelled")

    monkeypatch.setattr(sample_statistics.np, "sum", observe_sum)
    with pytest.raises(InterruptedError, match="cancelled"):
        sample_statistics.finite_mean(values, check_cancelled=cancel)
    assert 0 < consumed <= 65_536
    np.testing.assert_array_equal(values, np.arange(values.size, dtype=dtype))


def test_mixed_binary_mean_cancels_during_exact_conversion():
    consumed = 0

    class MeasuredFloat(float):
        def as_integer_ratio(self):
            nonlocal consumed
            consumed += 1
            return super().as_integer_ratio()

    values = np.array([MeasuredFloat(.25) for _ in range(8192)], dtype=object)

    def cancel():
        if consumed >= 1024:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        sample_statistics.finite_mean(values, check_cancelled=cancel)
    assert consumed == 1024


@pytest.mark.parametrize("values", [
    np.array([np.iinfo(np.int64).min, np.iinfo(np.int64).max], dtype=np.int64),
    np.array([0, np.iinfo(np.uint64).max], dtype=np.uint64),
    np.array([2**63 + 1, -2.**63, .25], dtype=object),
    np.array([2**51, 2.**51 + .5, 2.**51 + .5], dtype=object),
])
def test_fast_exact_totals_preserve_integer_endpoints_and_fractional_residuals(values):
    exact = [Fraction(value.item() if isinstance(value, np.generic) else value) for value in values]
    mean = sum(exact) / len(exact)
    centered = np.array([float(value - exact[0]) for value in exact])
    assert sample_statistics.finite_mean(values) == float(mean)
    assert sample_statistics.finite_standard_deviation(values) == pytest.approx(np.std(centered), rel=2e-15)
