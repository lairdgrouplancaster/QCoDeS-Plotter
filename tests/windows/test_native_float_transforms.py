"""Wide native arithmetic through real QCoDeS arrays and Qt controls."""

import numpy as np
import pytest

from qplot.windows._native_transforms import native_fft_coordinate_error
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture

_FLOAT16_Y = np.array([-60000, 60000, -60000], dtype=np.float16)
_FLOAT32_Y = np.array([-3e38, 3e38, -3e38], dtype=np.float32)
_FLOAT16_X = np.array([-60000, 0, 60000], dtype=np.float16)
_FLOAT32_X = np.array([-3e38, 0, 3e38], dtype=np.float32)


def _wide_derivative(x, y):
    return np.array([
        (float(y[index + 1]) - float(y[index]))
        / (float(x[index + 1]) - float(x[index]))
        for index in range(len(y) - 1)
    ])


def _assert_original(line, x, y):
    original_x, original_y = line.getOriginalDataset()
    assert original_x.dtype == x.dtype
    assert original_y.dtype == y.dtype
    np.testing.assert_array_equal(original_x, x)
    np.testing.assert_array_equal(original_y, y)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.arange(3), "y": _FLOAT16_Y},
    {"x": np.arange(3), "y": _FLOAT32_Y},
    {"x": np.array([-60000, 60000, -60000], dtype=np.float16),
     "y": np.array([0, 1, 0])},
    {"x": np.array([-3e38, 3e38, -3e38], dtype=np.float32),
     "y": np.array([0, 1, 0])},
], indirect=True, ids=["float16-y", "float32-y", "float16-x", "float32-x"])
@pytest.mark.parametrize("controls", [
    ("derivativeCheck",),
    ("phasemapCheck",),
    ("derivativeCheck", "subtractMeanCheck"),
    ("phasemapCheck", "subtractMeanCheck"),
])
def test_native_derivative_and_phase_widen_before_subtracting(
    fft_plots, no_callback_errors, controls,
):
    window, (plot, source), x, y = fft_plots
    plot.line.setDynamicRangeLimit(None)
    key, secondary = merge(window, plot, source, x_axis="Top", y_axis="Right")
    secondary.setDynamicRangeLimit(None)
    for name in controls:
        click_control(plot, name)
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        for line in (plot.line, plot.lines[key]):
            raw_x, raw_y = line.getOriginalDataset()
            expected_x = raw_y[:-1] if "phasemapCheck" in controls else raw_x[:-1]
            expected_y = _wide_derivative(raw_x, raw_y)
            np.testing.assert_array_equal(line.getData()[0], expected_x)
            np.testing.assert_array_equal(line.getData()[1], expected_y)
            assert np.all(np.isfinite(line.getData()[1]))
            _assert_original(line, raw_x, raw_y)
    for name in reversed(controls):
        click_control(plot, name)
    _assert_original(plot.line, x, y)
    np.testing.assert_array_equal(plot.line.getData()[1], y)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.arange(3), "y": _FLOAT16_Y},
    {"x": np.arange(3), "y": _FLOAT32_Y},
], indirect=True, ids=["float16-y", "float32-y"])
@pytest.mark.parametrize("controls", [
    ("phasemapCheck", "fftCheck"),
    ("phasemapCheck", "subtractMeanCheck", "fftCheck"),
    ("phasemapCheck", "fftCheck", "derivativeCheck"),
])
def test_phase_uses_original_samples_in_fft_combinations(
    fft_plots, no_callback_errors, controls,
):
    _window, (plot, _source), x, y = fft_plots
    plot.line.setDynamicRangeLimit(None)
    for name in controls:
        click_control(plot, name)
    np.testing.assert_array_equal(plot.line.getData()[0], y[:-1])
    np.testing.assert_array_equal(plot.line.getData()[1], _wide_derivative(x, y))
    assert np.all(np.isfinite(plot.line.getData()[1]))
    _assert_original(plot.line, x, y)


@pytest.mark.parametrize("fft_plots", [
    {"x": _FLOAT16_X, "y": np.array([1, 0, -1])},
    {"x": _FLOAT16_X[::-1], "y": np.array([-1, 0, 1])},
    {"x": np.array([-60000, -10000, 60000], dtype=np.float16),
     "y": np.array([1, 0, -1])},
    {"x": _FLOAT32_X, "y": np.array([1, 0, -1])},
], indirect=True, ids=["float16-x", "float16-descending", "float16-nonuniform",
                   "float32-x"])
def test_native_fft_widens_coordinate_span_before_validation(
    fft_plots, no_callback_errors,
):
    window, (plot, source), x, y = fft_plots
    key, secondary = merge(window, plot, source, x_axis="Top", y_axis="Right")
    assert native_fft_coordinate_error(x) is None
    click_control(plot, "fftCheck")
    assert plot.plot.ctrl.fftCheck.isChecked()
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        for line in (plot.line, plot.lines[key]):
            raw_x, raw_y = line.getOriginalDataset()
            ordered_x = raw_x.astype(np.float64)
            ordered_y = raw_y.astype(np.float64)
            if ordered_x[0] > ordered_x[-1]:
                ordered_x, ordered_y = ordered_x[::-1], ordered_y[::-1]
            spacing = (ordered_x[-1] - ordered_x[0]) / (len(raw_x) - 1)
            if not np.allclose(np.diff(ordered_x), np.diff(ordered_x)[0], rtol=1e-3):
                ordered_y = np.interp(
                    np.linspace(ordered_x[0], ordered_x[-1], len(raw_x)),
                    ordered_x, ordered_y,
                )
            frequencies = np.fft.rfftfreq(len(raw_x), spacing)
            magnitudes = np.abs(np.fft.rfft(ordered_y) / len(raw_y))
            np.testing.assert_allclose(line.getData()[0], frequencies, rtol=1e-14)
            np.testing.assert_allclose(line.getData()[1], magnitudes, rtol=1e-14)
            assert np.all(np.isfinite(line.getData()[0]))
            assert np.all(np.isfinite(line.getData()[1]))
            _assert_original(line, raw_x, raw_y)
    click_control(plot, "fftCheck")
    _assert_original(plot.line, x, y)
    np.testing.assert_array_equal(plot.line.getData()[0], x)
    np.testing.assert_array_equal(plot.line.getData()[1], y)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([-60000, 60000, 0], dtype=np.float16),
     "y": np.array([1, 0, -1])},
], indirect=True)
def test_overflowing_float16_span_does_not_hide_invalid_fft_sweep(
    fft_plots, no_callback_errors,
):
    _window, (plot, _source), x, y = fft_plots
    assert "FFT requires" in native_fft_coordinate_error(x)
    click_control(plot, "fftCheck")
    assert not plot.plot.ctrl.fftCheck.isChecked()
    _assert_original(plot.line, x, y)
