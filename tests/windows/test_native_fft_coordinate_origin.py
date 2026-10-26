"""Native spectra do not depend on the origin of stored float coordinates."""

import cmath
import csv
from fractions import Fraction

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from qplot.windows._native_transforms import (
    NativePlotDataItem,
    native_fft_coordinate_error,
)
from tests.windows.test_merged_trace_metadata import assert_axis, merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_native_transform_snapping import assert_full_snap_data
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture


def _spectrum(x, y, *, subtract_mean=False):
    """Exact interpolation in recorded coordinates, followed by a direct DFT."""
    pairs = sorted(zip(map(Fraction, map(float, x)), map(Fraction, map(float, y)), strict=True))
    coordinates, samples = map(list, zip(*pairs, strict=True))
    if subtract_mean:
        mean = sum(samples) / len(samples)
        samples = [value - mean for value in samples]
    spacing = (coordinates[-1] - coordinates[0]) / (len(samples) - 1)
    interpolated = []
    for index in range(len(samples)):
        target = coordinates[0] + index * spacing
        segment = next(j for j in range(len(samples) - 1)
                        if coordinates[j] <= target <= coordinates[j + 1])
        weight = (target - coordinates[segment]) / (coordinates[segment + 1] - coordinates[segment])
        interpolated.append(samples[segment] + weight * (samples[segment + 1] - samples[segment]))
    frequencies = [float(Fraction(k, len(samples)) / spacing)
                   for k in range(len(samples) // 2 + 1)]
    magnitudes = [abs(sum(float(value) * cmath.exp(-2j * np.pi * k * j / len(samples))
                         for j, value in enumerate(interpolated)) / len(samples))
                  for k in range(len(frequencies))]
    magnitudes[0] = abs(float(sum(interpolated) / len(samples)))
    return np.array(frequencies), np.array(magnitudes)


_OPTIONS = [
    {"x": 2.**30 + np.array([0., 1., 2., 4.]) * 2.**-22, "y": [0., 2., 4., 8.]},
    {"x": -2.**30 + np.array([0., 1., 2., 4.]) * 2.**-22, "y": [3., 10., -2., 11.]},
    {"x": 1e16 + np.array([0., 2., 4., 8.]), "y": [0., 2., 4., 8.]},
    {"x": np.array([0., 2., 4., 8.]), "y": [0., 2., 4., 8.]},
    {"x": 2.**30 + np.arange(4) * 2.**-22, "y": [3., 10., -2., 11.]},
]


@pytest.mark.parametrize("fft_plots", _OPTIONS, indirect=True)
def test_offset_fft_primary_secondary_refresh_and_raw_csv(
    fft_plots, no_callback_errors, tmp_path,
):
    window, (plot, other), _x, _y = fft_plots
    originals = [tuple(v.copy() for v in p.line.getOriginalDataset()) for p in (plot, other)]
    click_control(plot, "fftCheck")
    _key, secondary = merge(window, plot, other, x_axis="Top", y_axis="Right")
    assert plot.plot.ctrl.fftCheck.isChecked()
    assert_axis(plot, "bottom", "Frequency of Coordinate", "1/V")
    assert_axis(plot, "left", "FFT magnitude of Signal", "A")
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        for line, raw in zip((plot.line, secondary), originals, strict=True):
            expected_x, expected_y = _spectrum(*raw)
            for item in (line, line.curve):
                actual_x, actual_y = item.getData()
                np.testing.assert_allclose(actual_x, expected_x, rtol=2e-14, atol=0)
                np.testing.assert_allclose(actual_y, expected_y, rtol=2e-14, atol=1e-13)
            assert_full_snap_data(line, expected_x, expected_y, expected_x, expected_y)
            for observed, recorded in zip(line.getOriginalDataset(), raw, strict=True):
                np.testing.assert_array_equal(observed, recorded)
    target = tmp_path / "origin.csv"
    assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    for offset, (x, y) in zip((0, 2), originals, strict=True):
        np.testing.assert_array_equal([float(row[offset]) for row in rows], x)
        np.testing.assert_array_equal([float(row[offset + 1]) for row in rows], y)


@pytest.mark.parametrize("fft_plots", _OPTIONS[:2], indirect=True)
def test_offset_fft_center_log_phase_and_restore(fft_plots, no_callback_errors):
    _window, plots, _x, _y = fft_plots
    for plot in plots:
        x, y = (values.copy() for values in plot.line.getOriginalDataset())
        click_control(plot, "subtractMeanCheck")
        click_control(plot, "fftCheck")
        expected_x, expected_y = _spectrum(x, y, subtract_mean=True)
        np.testing.assert_allclose(plot.line.getData()[1], expected_y, rtol=2e-14, atol=1e-13)
        click_control(plot, "logXCheck")
        np.testing.assert_allclose(plot.line.getData()[0], np.log10(expected_x[1:]), rtol=2e-14)
        np.testing.assert_allclose(plot.line.getData()[1], expected_y[1:], rtol=2e-14, atol=1e-13)
        click_control(plot, "logXCheck")
        click_control(plot, "phasemapCheck")
        np.testing.assert_array_equal(plot.line.getData()[0], y[:-1])
        np.testing.assert_allclose(plot.line.getData()[1], np.diff(y) / np.diff(x))
        click_control(plot, "phasemapCheck")
        np.testing.assert_allclose(plot.line.getData()[1], expected_y, rtol=2e-14, atol=1e-13)
        click_control(plot, "fftCheck")
        click_control(plot, "subtractMeanCheck")
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], x)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], y)


@pytest.mark.parametrize("x", [
    np.array([-1e16, 0., 1.]),
    np.array([-1e308, 0., 1e308]),
    np.array([0., 2., 4., 8.], dtype=np.float32),
    np.array([2**60, 2**60 + 2, 2**60 + 4, 2**60 + 8], dtype=np.int64),
])
def test_fft_origin_retains_wide_and_narrow_coordinate_controls(x):
    y = np.arange(len(x), dtype=float)
    assert native_fft_coordinate_error(x) is None
    line = NativePlotDataItem(x=x, y=y, fftMode=True)
    expected_x, expected_y = _spectrum(x, y) if x.dtype.kind != "i" else _spectrum(x - x[0], y)
    actual_x, actual_y = line.getData()
    np.testing.assert_allclose(actual_x, expected_x, rtol=2e-14, atol=0)
    np.testing.assert_allclose(actual_y, expected_y, rtol=2e-14, atol=1e-13)
    np.testing.assert_array_equal(line.getOriginalDataset()[0], x)
