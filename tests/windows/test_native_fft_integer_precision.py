"""Native FFT magnitudes retain the meaning of recorded integer arrays."""

import csv
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0]), "y": np.array([np.iinfo(dtype).min], dtype=dtype)}
    for dtype in (np.int8, np.int16, np.int32, np.int64)
], indirect=True)
def test_singleton_signed_minimum_has_positive_fft_magnitude(fft_plots, no_callback_errors):
    _window, (plot, _other), x, y = fft_plots
    expected = np.array([float(-int(y[0]))])
    click_control(plot, "fftCheck")
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        for item in (plot.line, plot.line.curve):
            np.testing.assert_array_equal(item.getData()[0], [0.])
            np.testing.assert_array_equal(item.getData()[1], expected)
        raw_x, raw_y = plot.line.getOriginalDataset()
        assert raw_y.dtype == y.dtype
        np.testing.assert_array_equal(raw_x, x)
        np.testing.assert_array_equal(raw_y, y)
    click_control(plot, "subtractMeanCheck")
    np.testing.assert_array_equal(plot.line.getData()[1], [0.])
    click_control(plot, "subtractMeanCheck")
    np.testing.assert_array_equal(plot.line.getData()[1], expected)
    click_control(plot, "fftCheck")
    np.testing.assert_array_equal(plot.line.getData()[1], y)


_OFFSET_CASES = [
    {"x": np.arange(4), "y": np.array([2**60 + k for k in range(4)])},
    {"x": np.arange(4), "y": np.array([2**64 - 4 + k for k in range(4)], dtype=np.uint64)},
    {"x": np.array([0, 1, 2, 6]), "y": np.array([2**60 + k for k in range(4)])},
    {"x": np.array([0, 1, 2, 6]), "y": np.array([-(2**63) + k for k in range(4)])},
]


def _offset_spectrum(x, y):
    """An independent low-offset oracle; interpolate only small differences."""
    if x[0] > x[-1]:
        x, y = x[::-1], y[::-1]
    exact = [Fraction(value.item() if isinstance(value, np.generic) else value) for value in y]
    differences = np.array([float(value - exact[0]) for value in exact])
    sampled = np.interp(np.linspace(x[0], x[-1], len(x)), x.astype(float), differences)
    magnitudes = np.abs(np.fft.rfft(sampled) / len(sampled))
    dc = exact[0] + sum((Fraction(float(value)) for value in sampled), Fraction()) / len(sampled)
    magnitudes[0] = abs(float(dc))
    frequencies = np.fft.rfftfreq(len(x), float(x[-1] - x[0]) / (len(x) - 1))
    return frequencies, magnitudes


@pytest.mark.parametrize("fft_plots", _OFFSET_CASES, indirect=True)
def test_integer_fft_preserves_ac_dc_secondary_refresh_and_csv(
    fft_plots, no_callback_errors, tmp_path,
):
    window, (plot, other), _x, _y = fft_plots
    raw = tuple(array.copy() for array in plot.line.getOriginalDataset())
    other_raw = tuple(array.copy() for array in other.line.getOriginalDataset())
    expected_x, expected_y = _offset_spectrum(*raw)
    plot.line.setDynamicRangeLimit(None)
    click_control(plot, "fftCheck")
    _key, secondary = merge(window, plot, other, x_axis="Top", y_axis="Right")
    secondary.setDynamicRangeLimit(None)
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        for line, original in ((plot.line, raw), (secondary, other_raw)):
            # Test the small AC coefficients separately from the large DC bin.
            for item in (line, line.curve):
                np.testing.assert_allclose(item.getData()[0], expected_x, rtol=1e-14, atol=0)
                np.testing.assert_allclose(item.getData()[1][1:], expected_y[1:], rtol=1e-14, atol=0)
                assert item.getData()[1][0] == expected_y[0]
            for observed, recorded in zip(line.getOriginalDataset(), original, strict=True):
                assert observed.dtype == recorded.dtype
                np.testing.assert_array_equal(observed, recorded)
    target = tmp_path / "integer-fft-raw.csv"
    assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    for offset, original in ((0, raw), (2, other_raw)):
        assert [int(row[offset + 1]) for row in rows] == list(map(int, original[1]))
    click_control(plot, "logXCheck")
    np.testing.assert_allclose(plot.line.getData()[0], np.log10(expected_x[1:]), rtol=1e-14)
    np.testing.assert_allclose(plot.line.getData()[1], expected_y[1:], rtol=1e-14, atol=0)
    click_control(plot, "logXCheck")
    click_control(plot, "fftCheck")
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw[1])


@pytest.mark.parametrize("fft_plots", [dict(_OFFSET_CASES[0], heatmap=True)], indirect=True)
def test_integer_heatmap_cut_fft_retains_object_cells(fft_plots, no_callback_errors):
    _window, (plot, _other), _x, _y = fft_plots
    raw_x, raw_y = plot.line.getOriginalDataset()
    assert raw_y.dtype.kind == "O"
    expected_x, expected_y = _offset_spectrum(raw_x, raw_y)
    plot.line.setDynamicRangeLimit(None)
    click_control(plot, "fftCheck")
    for row in (0, 1):
        plot.picker.slider.setValue(row)
        np.testing.assert_allclose(plot.line.getData()[0], expected_x, rtol=1e-14)
        np.testing.assert_allclose(plot.line.getData()[1][1:], expected_y[1:], rtol=1e-14, atol=0)
        assert plot.line.getData()[1][0] == expected_y[0]
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw_y)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.arange(3), "y": np.array([-(2**60), 1, 2**60])},
], indirect=True)
def test_integer_fft_dc_is_exact_original_mean(fft_plots, no_callback_errors):
    _window, (plot, _other), _x, _y = fft_plots
    click_control(plot, "fftCheck")
    assert plot.line.getData()[1][0] == 1 / 3
    np.testing.assert_allclose(plot.line.getData()[1][1], 2**60 / np.sqrt(3), rtol=1e-14)


@pytest.mark.parametrize("fft_plots", [_OFFSET_CASES[0]], indirect=True)
def test_integer_fft_retains_fractional_limit_cells(fft_plots, no_callback_errors, tmp_path):
    _window, (plot, _other), _x, y = fft_plots
    bound = Decimal(int(y[0])) + Decimal("0.5")
    option = operation_option(plot, "Limit Maximum")
    option.input.setChecked(True)
    option.operation_row.input.setText(str(bound))
    assert apply_operations(plot)[1:] == ([True], [])
    raw_x, raw_y = plot.line.getOriginalDataset()
    assert raw_y.dtype.kind == "O"
    assert raw_y.tolist() == [int(y[0]), bound, bound, bound]
    expected_x, expected_y = _offset_spectrum(raw_x, raw_y)
    click_control(plot, "fftCheck")
    np.testing.assert_allclose(plot.line.getData()[0], expected_x, rtol=1e-14)
    np.testing.assert_allclose(plot.line.getData()[1][1:], expected_y[1:], rtol=1e-14, atol=0)
    assert plot.line.getData()[1][0] == expected_y[0]
    target = tmp_path / "fractional-fft.csv"
    assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    assert [Decimal(row[1]) for row in rows] == list(map(Decimal, raw_y))
