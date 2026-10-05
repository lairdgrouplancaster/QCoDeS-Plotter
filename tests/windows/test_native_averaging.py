"""Average real QCoDeS traces through the actual native Qt controls."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtTest, QtWidgets

from qplot.windows._native_averaging import NativePlotItem
from tests.windows.test_merged_trace_metadata import assert_samples, merge
from tests.windows.test_native_fft_coordinates import (
    assert_spectrum,
)
from tests.windows.test_native_fft_coordinates import (
    fft_plots as _fft_plots_fixture,
)
from tests.windows.test_native_transform_labels import click_control, expected_mapping
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture

X = np.arange(16, dtype=float)
Y = X ** 2 + 1
RUNS = {"x": X, "y": Y, "other_x": X, "other_y": 2 * Y + 3, "scalar": True}


def click_average(host):
    control = host.plot.ctrl.averageGroup
    control.show()
    option = QtWidgets.QStyleOptionGroupBox()
    control.initStyleOption(option)
    rect = control.style().subControlRect(
        QtWidgets.QStyle.ComplexControl.CC_GroupBox, option,
        QtWidgets.QStyle.SubControl.SC_GroupBoxCheckBox, control,
    )
    QtTest.QTest.mouseClick(control, QtCore.Qt.MouseButton.LeftButton, pos=rect.center())


def assert_average(host, expected_x, expected_y, count):
    assert len(host.plot.avgCurves) == 1
    actual_count, average = next(iter(host.plot.avgCurves.values()))
    assert actual_count == count
    assert average not in host.lines.values()
    assert_spectrum(average, expected_x, expected_y)
    for key in ("fftMode", "derivativeMode", "phasemapMode", "subtractMeanMode"):
        assert not average.opts[key]
    return average


@pytest.mark.parametrize("fft_plots", [RUNS], indirect=True)
@pytest.mark.parametrize("order", [("averageGroup", "fftCheck"), ("fftCheck", "averageGroup")])
@pytest.mark.parametrize("multiple", [False, True])
def test_native_fft_average_once_and_disable(fft_plots, no_callback_errors, order, multiple):
    window, (host, source), x, y = fft_plots
    if multiple:
        merge(window, host, source, x_axis="Bottom", y_axis="Left")
    for name in order:
        click_average(host) if name == "averageGroup" else click_control(host, name)
    assert host.plot.ctrl.averageGroup.isChecked()
    frequencies = np.fft.rfftfreq(16)
    transformed = [np.abs(np.fft.rfft(line.getOriginalDataset()[1]) / 16)
                   for line in host.lines.values()]
    for line, expected in zip(host.lines.values(), transformed, strict=True):
        assert_spectrum(line, frequencies, expected)
    assert host.line.getData()[1][0] == pytest.approx(78.5)
    for _ in range(3):
        assert_average(host, frequencies, np.mean(transformed, axis=0), 2 if multiple else 1)
        host.plot.recomputeAverages()
    assert_samples(host.line, x, y)
    click_control(host, "fftCheck")
    originals = [line.getOriginalDataset()[1] for line in host.lines.values()]
    assert_average(host, x, np.mean(originals, axis=0), 2 if multiple else 1)


def assert_transformed_average(host, controls):
    processed = []
    for line in host.lines.values():
        x, y = line.getOriginalDataset()
        expected = expected_mapping(x, y, controls)
        assert_spectrum(line, *expected)
        processed.append(expected)
    return assert_average(host, processed[0][0], np.mean([p[1] for p in processed], axis=0),
                          len(processed))


@pytest.mark.parametrize("fft_plots", [RUNS], indirect=True)
@pytest.mark.parametrize("controls", [
    ("subtractMeanCheck",), ("derivativeCheck",), ("phasemapCheck",),
    ("subtractMeanCheck", "fftCheck"), ("subtractMeanCheck", "derivativeCheck"),
    ("phasemapCheck", "fftCheck", "subtractMeanCheck"),
    ("phasemapCheck", "fftCheck", "derivativeCheck", "subtractMeanCheck"),
])
@pytest.mark.parametrize("average_first", [False, True])
@pytest.mark.parametrize("multiple", [False, True])
def test_native_average_transform_combinations_and_disable(
    fft_plots, no_callback_errors, controls, average_first, multiple,
):
    window, (host, source), _x, _y = fft_plots
    if multiple:
        merge(window, host, source, x_axis="Bottom", y_axis="Left")
    originals = [tuple(v.copy() for v in line.getOriginalDataset()) for line in host.lines.values()]
    if average_first:
        click_average(host)
    active = []
    for name in controls:
        click_control(host, name)
        active.append(name)
        if average_first:
            assert_transformed_average(host, active)
    if not average_first:
        click_average(host)
    assert_transformed_average(host, active)
    # Re-enabling averaging must rebuild from measured curves even after
    # their processing changed while the old generated curves were hidden.
    click_average(host)
    for _count, average in host.plot.avgCurves.values():
        assert not average.isVisible()
    for name in reversed(controls):
        click_control(host, name)
        active.remove(name)
    click_average(host)
    assert_transformed_average(host, ())
    for line, original in zip(host.lines.values(), originals, strict=True):
        assert_samples(line, *original)


@pytest.mark.parametrize("fft_plots", [RUNS], indirect=True)
@pytest.mark.parametrize("controls", [("fftCheck",), ("derivativeCheck",), ("phasemapCheck",)])
def test_average_rebuilds_for_merge_refresh_and_removal(fft_plots, no_callback_errors, controls):
    window, (host, source), _x, _y = fft_plots
    click_average(host)
    for name in controls:
        click_control(host, name)
    assert_transformed_average(host, controls)
    key, secondary = merge(window, host, source, x_axis="Bottom", y_axis="Left")
    assert_transformed_average(host, controls)
    host.refreshWindow(force=True)
    wait_for(lambda: not host.worker.running)
    host.monitor.stop()
    assert_transformed_average(host, controls)
    source.refreshWindow(force=True)
    wait_for(lambda: not source.worker.running)
    source.monitor.stop()
    assert_transformed_average(host, controls)
    host.remove_line(source.label, trace_key=key)
    assert secondary not in host.plot.items
    assert_transformed_average(host, controls)


@pytest.mark.parametrize("fft_plots", [RUNS], indirect=True)
def test_average_log_and_downsampling_process_display_once(fft_plots, no_callback_errors):
    _window, (host, _source), _x, _y = fft_plots
    click_average(host)
    click_control(host, "fftCheck")
    click_control(host, "logXCheck")
    click_control(host, "logYCheck")
    host.plot.setDownsampling(ds=2, auto=False, mode="subsample")
    display_x, display_y = host.line.getData()
    assert len(display_x) == 4
    average = assert_average(host, display_x, display_y, 1)
    assert average.opts["logMode"] == [False, False]
    assert average.opts["downsample"] == 1
    click_control(host, "logXCheck")
    click_control(host, "logYCheck")
    host.plot.setDownsampling(ds=False)
    assert_transformed_average(host, ("fftCheck",))


@pytest.mark.parametrize("fft_plots", [{**RUNS, "other_x": X + 100}], indirect=True)
def test_average_keeps_native_positional_coordinate_compatibility(fft_plots, no_callback_errors):
    window, (host, source), x, y = fft_plots
    merge(window, host, source, x_axis="Bottom", y_axis="Left")
    click_average(host)
    assert_average(host, x, (y + 2 * y + 3) / 2, 2)


@pytest.mark.parametrize("fft_plots", [{**RUNS, "other_x": X[:8], "other_y": Y[:8]}], indirect=True)
def test_average_keeps_native_shape_replacement(fft_plots, no_callback_errors):
    window, (host, source), _x, _y = fft_plots
    merge(window, host, source, x_axis="Bottom", y_axis="Left")
    click_average(host)
    assert_average(host, X[:8], Y[:8], 2)
    click_control(host, "fftCheck")
    assert_average(host, np.fft.rfftfreq(8), np.abs(np.fft.rfft(Y[:8]) / 8), 2)


def test_average_metadata_grouping_and_skip_average(qapplication):
    plot = NativePlotItem()
    sources = []
    for offset, group in enumerate(("a", "a", "b")):
        line = pg.PlotDataItem(X, Y + offset)
        plot.addItem(line, params={"sample": group, "repeat": offset})
        sources.append(line)
    plot.addItem(pg.PlotDataItem(X, Y + 100), skipAverage=True)
    for index in range(plot.ctrl.avgParamList.count()):
        item = plot.ctrl.avgParamList.item(index)
        item.setCheckState(QtCore.Qt.CheckState.Checked if item.text() == "repeat"
                           else QtCore.Qt.CheckState.Unchecked)
        plot.ctrl.avgParamList.itemClicked.emit(item)
    plot.ctrl.averageGroup.setChecked(True)
    assert len(plot.avgCurves) == 2
    assert sorted(entry[0] for entry in plot.avgCurves.values()) == [1, 2]
    groups = {dict(key)["sample"]: average for key, (_count, average) in plot.avgCurves.items()}
    assert_spectrum(groups["a"], X, Y + .5)
    assert_spectrum(groups["b"], X, Y + 2)
    for _ in range(3):
        plot.recomputeAverages()
        assert sorted(entry[0] for entry in plot.avgCurves.values()) == [1, 2]
    plot.clear()
    assert plot.items == []
    assert plot.curves == []


@pytest.mark.parametrize("fft_plots", [{**RUNS, "y": np.tile([1., 5., 2., 4.], 4)}], indirect=True)
def test_phase_average_is_excluded_from_fft_coordinate_validation(fft_plots, no_callback_errors):
    _window, (host, _source), _x, _y = fft_plots
    click_average(host)
    click_control(host, "phasemapCheck")
    click_control(host, "fftCheck")
    assert_transformed_average(host, ("phasemapCheck", "fftCheck"))
    # The generated phase-map X reverses direction. FFT eligibility depends
    # on the measured coordinate X, which remains a valid increasing sweep.
    click_control(host, "phasemapCheck")
    assert host.plot.ctrl.fftCheck.isChecked()
    assert_transformed_average(host, ("fftCheck",))
