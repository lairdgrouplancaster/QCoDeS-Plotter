"""Floating FFT spectra through stored QCoDeS arrays and native Qt controls."""

import csv
from fractions import Fraction

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_native_transform_snapping import assert_full_snap_data
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture

_CASES = [
    {"x": np.arange(3), "y": np.full(3, 60000, dtype=np.float16)},
    {"x": np.arange(3), "y": np.full(3, 3e38, dtype=np.float32)},
    {"x": np.arange(4), "y": np.array([3e38, -3e38, 3e38, -3e38], dtype=np.float32)},
    {"x": np.arange(3), "y": np.array([-2**60, 1., 2**60], dtype=float)},
    {"x": np.arange(3), "y": np.array([2**60, -1., -2**60], dtype=float)},
    {"x": np.arange(3), "y": np.array([-1e308, 1., 1e308])},
    {"x": np.arange(3), "y": np.full(3, np.finfo(float).max)},
    {"x": np.array([0., 1., 2., 3., 8.]), "y": np.array([-2**60, 1., 2**60, 1., 1.])},
]


def _spectrum(x, y, subtract_mean=False):
    """Exact finite DC and a scaled float64 oracle for the AC components."""
    if x[0] > x[-1]:
        x, y = x[::-1], y[::-1]
    samples = y.astype(np.float64)
    if subtract_mean:
        exact = [Fraction(float(value)) for value in samples]
        mean = sum(exact, Fraction()) / len(exact)
        samples = np.array([float(value - mean) for value in exact])
    if np.any(np.abs(np.diff(x) - np.diff(x)[0]) > abs(np.diff(x)[0]) / 1000):
        samples = np.interp(np.linspace(x[0], x[-1], len(x)), x, samples)
    dc = abs(float(sum((Fraction(float(v)) for v in samples), Fraction()) / len(samples)))
    scale = max(float(np.max(np.abs(samples))), 1.)
    magnitudes = np.abs(np.fft.rfft(samples / scale) / len(samples)) * scale
    magnitudes[0] = dc
    frequencies = np.fft.rfftfreq(len(x), float(x[-1] - x[0]) / (len(x) - 1))
    return frequencies, magnitudes


@pytest.mark.parametrize("fft_plots", _CASES, indirect=True)
def test_floating_fft_primary_secondary_refresh_and_raw_csv(
    fft_plots, no_callback_errors, tmp_path,
):
    window, (plot, other), _x, _y = fft_plots
    originals = [tuple(v.copy() for v in p.line.getOriginalDataset()) for p in (plot, other)]
    click_control(plot, "fftCheck")
    _key, secondary = merge(window, plot, other, x_axis="Top", y_axis="Right")
    for line in (plot.line, secondary):
        line.setDynamicRangeLimit(None)
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        for line, raw in zip((plot.line, secondary), originals, strict=True):
            expected_x, expected_y = _spectrum(*raw)
            for item in (line, line.curve):
                np.testing.assert_allclose(item.getData()[0], expected_x, rtol=2e-14, atol=0)
                np.testing.assert_allclose(item.getData()[1], expected_y, rtol=2e-14, atol=0)
                assert np.all(np.isfinite(item.getData()[1]))
            assert_full_snap_data(line, expected_x, expected_y, expected_x, expected_y)
            for observed, recorded in zip(line.getOriginalDataset(), raw, strict=True):
                assert observed.dtype == recorded.dtype
                np.testing.assert_array_equal(observed, recorded)
    target = tmp_path / "floating-fft.csv"
    assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    for offset, (_x, y) in zip((0, 2), originals, strict=True):
        assert [float(row[offset + 1]) for row in rows] == list(map(float, y))
    click_control(plot, "fftCheck")
    for line, raw in zip((plot.line, secondary), originals, strict=True):
        np.testing.assert_array_equal(line.getData()[1], raw[1])


@pytest.mark.parametrize("fft_plots", [_CASES[1], _CASES[3], _CASES[7]], indirect=True)
def test_floating_fft_mean_and_log_interactions(fft_plots, no_callback_errors):
    _window, (plot, _other), x, y = fft_plots
    plot.line.setDynamicRangeLimit(None)
    click_control(plot, "subtractMeanCheck")
    click_control(plot, "fftCheck")
    expected_x, expected_y = _spectrum(x, y, subtract_mean=True)
    np.testing.assert_allclose(plot.line.getData()[1], expected_y, rtol=2e-14, atol=0)
    click_control(plot, "logXCheck")
    np.testing.assert_allclose(plot.line.getData()[0], np.log10(expected_x[1:]), rtol=2e-14)
    np.testing.assert_allclose(plot.line.getData()[1], expected_y[1:], rtol=2e-14, atol=0)
    click_control(plot, "logXCheck")
    click_control(plot, "subtractMeanCheck")
    _expected_x, expected_y = _spectrum(x, y)
    np.testing.assert_allclose(plot.line.getData()[1], expected_y, rtol=2e-14, atol=0)


@pytest.mark.parametrize("fft_plots", [dict(_CASES[1], heatmap=True), dict(_CASES[3], heatmap=True)], indirect=True)
def test_floating_heatmap_cut_fft_preserves_finite_dc(fft_plots, no_callback_errors):
    _window, (plot, _other), _x, _y = fft_plots
    raw_x, raw_y = (v.copy() for v in plot.line.getOriginalDataset())
    expected_x, expected_y = _spectrum(raw_x, raw_y)
    plot.line.setDynamicRangeLimit(None)
    click_control(plot, "fftCheck")
    for row in (0, 1):
        plot.picker.slider.setValue(row)
        np.testing.assert_allclose(plot.line.getData()[0], expected_x, rtol=2e-14, atol=0)
        np.testing.assert_allclose(plot.line.getData()[1], expected_y, rtol=2e-14, atol=0)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw_y)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0., 1e-309]), "y": np.ones(2)},
    {"x": np.arange(8) * 1e-309, "y": np.ones(8)},
], indirect=True)
def test_unrepresentable_fft_frequency_axis_is_rejected(fft_plots, no_callback_errors):
    _window, plots, _x, _y = fft_plots
    for plot in plots:
        raw = tuple(v.copy() for v in plot.line.getOriginalDataset())
        with np.errstate(all="raise"):
            click_control(plot, "fftCheck")
        assert not plot.plot.ctrl.fftCheck.isChecked()
        assert not plot.line.opts["fftMode"]
        assert "FFT requires" in plot.statusBar().currentMessage()
        for observed, expected in zip(plot.line.getData(), raw, strict=True):
            np.testing.assert_array_equal(observed, expected)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.arange(4) * 4e-309, "y": np.ones(4)},
], indirect=True)
def test_representable_tiny_fft_frequency_axis_is_supported(fft_plots, no_callback_errors):
    _window, plots, _x, _y = fft_plots
    for plot in plots:
        x, y = plot.line.getOriginalDataset()
        click_control(plot, "fftCheck")
        assert plot.plot.ctrl.fftCheck.isChecked()
        spacing = (Fraction(float(max(x))) - Fraction(float(min(x)))) / (len(x) - 1)
        expected_x = [float(Fraction(k, len(x)) / spacing) for k in range(len(x) // 2 + 1)]
        np.testing.assert_allclose(plot.line.getData()[0], expected_x, rtol=2e-14, atol=0)
        np.testing.assert_array_equal(plot.line.getData()[1], [1., 0., 0.])


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0., 1.]), "y": np.array([0., 1e-309])},
], indirect=True)
def test_refresh_to_unrepresentable_fft_coordinates_rolls_back_and_recovers(
    fft_plots, no_callback_errors,
):
    window, (plot, other), _x, _y = fft_plots
    _key, secondary = merge(window, plot, other, x_axis="Top", y_axis="Right")
    click_control(plot, "fftCheck")
    assert plot.plot.ctrl.fftCheck.isChecked()
    assert plot.set_plot_axes_swapped(True)
    wait_for(lambda: not plot.worker.running)
    plot.monitor.stop()
    assert not plot.plot.ctrl.fftCheck.isChecked()
    assert "FFT requires" in plot.statusBar().currentMessage()
    for line in (plot.line, secondary):
        assert not line.opts["fftMode"]
        for observed, raw in zip(line.getData(), line.getOriginalDataset(), strict=True):
            np.testing.assert_array_equal(observed, raw)
    assert plot.set_plot_axes_swapped(False)
    wait_for(lambda: not plot.worker.running)
    plot.monitor.stop()
    click_control(plot, "fftCheck")
    assert plot.plot.ctrl.fftCheck.isChecked()
    for line in (plot.line, secondary):
        assert np.all(np.isfinite(line.getData()[0]))
