"""Integer-safe native controls through actual QCoDeS storage and Qt slots."""

import csv
from decimal import Decimal

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from qplot.windows._native_transforms import _safe_secant
from qplot.windows._plot1d_snap import _line_snap_data
from tests.support.windows.merged_trace_metadata import merge
from tests.support.windows.native_averaging import assert_average, click_average
from tests.support.windows.native_integer_transforms import (
    _CASES as _CASES,
)
from tests.support.windows.native_integer_transforms import (
    _INT_MAX as _INT_MAX,
)
from tests.support.windows.native_integer_transforms import (
    _INT_MIN as _INT_MIN,
)
from tests.support.windows.native_integer_transforms import (
    _UINT_MAX as _UINT_MAX,
)
from tests.support.windows.native_integer_transforms import (
    integer_plots as integer_plots,
)
from tests.support.windows.native_transform_labels import (
    SUPPORTED_CONTROLS,
    assert_display,
    click_control,
)
from tests.support.windows.native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.support.windows.native_transform_snapping import assert_snap
from tests.support.windows.plot_integration import wait_for

no_callback_errors = _no_callback_errors_fixture


def assert_raw_display(line, x, y):
    for actual, expected in zip(line.getOriginalDataset(), (x, y), strict=True):
        np.testing.assert_array_equal(actual, expected)
    # Compare exact Python integers, independently of the display guard.
    collapses = any(
        len(set(map(float, values))) < len(set(map(int, values)))
        for values in (x, y) if values.dtype.kind in "iuO"
    )
    if collapses:
        assert line.getData()[0].size == 0
        assert "display precision" in line._qplot_precision_error
        assert _line_snap_data(line) is None
    else:
        assert_display(line, x, y)


def assert_integer_mapping(plot, x, y, slopes, *, phase=False):
    mapped_x = y[:-1] if phase else x[:-1]
    np.testing.assert_array_equal(_safe_secant(x, y), slopes)
    collapses = (mapped_x.dtype.kind in "iuO" and
                 len(set(map(float, mapped_x))) < len(set(map(int, mapped_x))))
    if mapped_x.dtype.kind == "O":
        mapped_x = mapped_x.astype(float)
    for line in getattr(plot, "lines", {None: plot.line}).values():
        if collapses:
            assert line.getData()[0].size == 0
            assert "display precision" in line._qplot_precision_error
            assert _line_snap_data(line) is None
        else:
            assert_display(line, mapped_x, slopes)
            assert_display(line.curve, mapped_x, slopes)
        original = line.getOriginalDataset()
        for actual, expected in zip(original, (x, y), strict=True):
            assert actual.dtype == expected.dtype
            np.testing.assert_array_equal(actual, expected)
        if collapses:
            continue
        samples = _line_snap_data(line)
        assert samples is not None
        np.testing.assert_array_equal(samples.x_raw, mapped_x)
        np.testing.assert_array_equal(samples.y_raw, slopes)
        np.testing.assert_array_equal(samples.x_view, mapped_x.astype(float))
        np.testing.assert_array_equal(samples.y_view, slopes)


def decimal_center(y):
    samples = [Decimal(int(value)) for value in y]
    mean = sum(samples) / Decimal(len(samples))
    return np.array([float(value - mean) for value in samples])


@pytest.mark.parametrize(
    "integer_plots",
    [(name, False) for name in (
        "adjacent_uint_y", "adjacent_uint_max_y",
        "adjacent_signed_min_y", "adjacent_signed_max_y",
        "full_uint_range", "signed_y_overflow",
    )], indirect=True,
)
def test_large_integer_mean_native_controls_and_fft(
    integer_plots, no_callback_errors, tmp_path,
):
    window, (plot, source), x, y, _slopes = integer_plots
    expected = decimal_center(y)
    click_control(plot, "subtractMeanCheck")
    key, secondary = merge(window, plot, source, x_axis="Top", y_axis="Right")
    secondary.setDynamicRangeLimit(None)

    def assert_centered():
        for line in (plot.line, plot.lines[key]):
            np.testing.assert_array_equal(line.getData()[1], expected)
            np.testing.assert_array_equal(line.curve.getData()[1], expected)
            np.testing.assert_array_equal(line.getOriginalDataset()[1], y)
            np.testing.assert_array_equal(_line_snap_data(line).y_raw, expected)

    assert_centered()
    target = tmp_path / "raw-after-centering.csv"
    assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    for offset in (0, 2):
        assert [int(row[offset + 1]) for row in rows] == y.tolist()

    if np.array_equal(x, np.arange(4)):
        frequencies = np.fft.rfftfreq(len(y))
        magnitudes = np.abs(np.fft.rfft(expected) / len(y))
        click_control(plot, "fftCheck")
        for line in (plot.line, plot.lines[key]):
            np.testing.assert_array_equal(line.getData()[0], frequencies)
            np.testing.assert_allclose(line.getData()[1], magnitudes, rtol=0, atol=1e-14)
            np.testing.assert_allclose(line.curve.getData()[1], magnitudes, rtol=0, atol=1e-14)
            np.testing.assert_array_equal(line.getOriginalDataset()[1], y)
        click_control(plot, "logXCheck")
        for line in (plot.line, plot.lines[key]):
            np.testing.assert_array_equal(line.getData()[0], np.log10(frequencies[1:]))
            np.testing.assert_allclose(line.getData()[1], magnitudes[1:], rtol=0, atol=1e-14)
        click_control(plot, "logXCheck")
        click_control(plot, "fftCheck")
        assert_centered()
    plot.refreshWindow(force=True)
    wait_for(lambda: not plot.worker.running)
    plot.monitor.stop()
    assert_centered()
    click_control(plot, "subtractMeanCheck")
    for line in (plot.line, plot.lines[key]):
        assert_raw_display(line, x, y)
    click_control(plot, "subtractMeanCheck")
    assert_centered()


@pytest.mark.parametrize("integer_plots", [("adjacent_uint_y", False)], indirect=True)
def test_large_integer_mean_average_uses_centered_display(integer_plots, no_callback_errors):
    window, (plot, source), x, y, _slopes = integer_plots
    merge(window, plot, source, x_axis="Bottom", y_axis="Left")
    expected = decimal_center(y)
    click_control(plot, "subtractMeanCheck")
    click_average(plot)
    assert_average(plot, x, expected, 2)
    click_control(plot, "fftCheck")
    assert_average(plot, np.fft.rfftfreq(len(x)),
                   np.abs(np.fft.rfft(expected) / len(x)), 2)
    plot.plot.recomputeAverages()
    assert_average(plot, np.fft.rfftfreq(len(x)),
                   np.abs(np.fft.rfft(expected) / len(x)), 2)
    for line in plot.lines.values():
        np.testing.assert_array_equal(line.getOriginalDataset()[1], y)


@pytest.mark.parametrize(
    "integer_plots", [(name, False) for name in _CASES], ids=list(_CASES), indirect=True,
)
@pytest.mark.parametrize("control", ["derivativeCheck", "phasemapCheck"])
def test_integer_differences_and_raw_csv(integer_plots, no_callback_errors, control, tmp_path):
    window, (plot, source), x, y, slopes = integer_plots
    _key, secondary = merge(window, plot, source, x_axis="Top", y_axis="Right")
    secondary.setDynamicRangeLimit(None)
    for _ in range(2):
        click_control(plot, control)
        assert_integer_mapping(plot, x, y, slopes, phase=control == "phasemapCheck")
        # CSV exports original measurements even with the native transform on.
        target = tmp_path / "original.csv"
        assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
        with target.open(newline="") as stream:
            rows = list(csv.reader(stream))[1:]
        for offset in (0, 2):
            for column, expected in enumerate((x, y), start=offset):
                convert = int if expected.dtype.kind in "iu" else float
                assert [convert(row[column]) for row in rows] == expected.tolist()
        click_control(plot, control)
        for line in plot.lines.values():
            assert_raw_display(line, x, y)


@pytest.mark.parametrize("integer_plots", [("reported_uint_y", False)], indirect=True)
@pytest.mark.parametrize("controls", SUPPORTED_CONTROLS[0:1] + SUPPORTED_CONTROLS[2:])
def test_integer_phase_combinations_refresh_and_secondary_snapping(
    integer_plots, no_callback_errors, controls,
):
    window, (plot, source), x, y, slopes = integer_plots
    for control in (*controls, "subtractMeanCheck"):
        click_control(plot, control)
    key, line = merge(window, plot, source, x_axis="Top", y_axis="Right")
    phase = "phasemapCheck" in controls
    assert_integer_mapping(plot, x, y, slopes, phase=phase)
    source.refreshWindow(force=True)
    wait_for(lambda: not source.worker.running)
    source.monitor.stop()
    plot.refreshWindow(force=True)
    wait_for(lambda: not plot.worker.running)
    plot.monitor.stop()
    assert_integer_mapping(plot, x, y, slopes, phase=phase)
    plot.snap_to_trace_action.setChecked(True)
    plot.line.setVisible(False)
    owner = line.getViewBox()
    mapped_x = y[:-1] if phase else x[:-1]
    owner.setRange(xRange=[-1, 11], yRange=[-3, -1], padding=0)
    for index in (0, 2):
        assert_snap(plot, owner, key, mapped_x, slopes, mapped_x, slopes, index)
    plot.line.setVisible(True)
    # Leaving a combined phase mode must retain compatible derivative arrays.
    if phase:
        click_control(plot, "phasemapCheck")
        assert not (plot.plot.ctrl.fftCheck.isChecked() and plot.plot.ctrl.derivativeCheck.isChecked())
        if plot.plot.ctrl.derivativeCheck.isChecked():
            assert_integer_mapping(plot, x, y, slopes)


@pytest.mark.parametrize("integer_plots", [("reported_uint_y", True)], indirect=True)
@pytest.mark.parametrize("control", ["derivativeCheck", "phasemapCheck"])
def test_real_integer_heatmap_cut(integer_plots, no_callback_errors, control):
    _window, (cut, _source), x, y, slopes = integer_plots
    # Geometry uses floating coordinates; full-resolution cell values retain
    # integers in object cells. Preserve both across controls and cursor moves.
    x, y = x.astype(float), y.astype(object)
    click_control(cut, control)
    assert_integer_mapping(cut, x, y, slopes, phase=control == "phasemapCheck")
    cut.picker.slider.setValue(1)
    assert_integer_mapping(cut, x, y, slopes, phase=control == "phasemapCheck")
    click_control(cut, control)
    for actual, expected in zip(cut.line.getData(), (x, y), strict=True):
        np.testing.assert_array_equal(actual, expected)
