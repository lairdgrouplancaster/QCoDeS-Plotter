"""Native FFT controls on real QCoDeS coordinate sweeps."""

import csv

import numpy as np
import pytest
from PyQt6 import QtCore
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import main as main_window
from qplot.windows._native_transforms import NativePlotDataItem
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_merged_trace_metadata import assert_axis, assert_samples, merge
from tests.windows.test_native_transform_labels import assert_display, click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_native_transform_snapping import (
    assert_full_snap_data,
    assert_snap,
    set_sample_range,
)
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for

no_callback_errors = _no_callback_errors_fixture

NONUNIFORM = np.array([0., 4., 7., 9.])
UNIFORM = np.array([0., 3., 6., 9.])


@pytest.fixture
def fft_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    options = request.param if isinstance(request.param, dict) else {"x": request.param}
    heatmap = options.get("heatmap", False)
    scalar = options.get("scalar", False)
    coordinates = np.asarray(options["x"])
    values = np.asarray(options.get("y", [1., 17., 50., 82.]))[:len(coordinates)]
    path = tmp_path / "fft.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("native FFT", sample_name="test")
    guids = []
    try:
        traces = (
            (coordinates, values),
            (np.asarray(options.get("other_x", coordinates[::-1])),
             np.asarray(options.get("other_y", values[::-1]))),
        )
        for x, y in traces:
            measurement = Measurement(exp=experiment)
            paramtype = "numeric" if scalar else "array"
            measurement.register_custom_parameter("x", paramtype=paramtype, label="Coordinate", unit="V")
            if heatmap:
                measurement.register_custom_parameter("slow", paramtype="array")
            measurement.register_custom_parameter(
                "signal", paramtype=paramtype, setpoints=("slow", "x") if heatmap else ("x",),
                label="Signal", unit="A",
            )
            with measurement.run(write_in_background=False) as saver:
                if heatmap:
                    saver.add_result(
                        ("x", np.tile(x, (2, 1))),
                        ("slow", np.repeat(np.arange(2)[:, None], len(x), axis=1)),
                        ("signal", np.tile(y, (2, 1))),
                    )
                elif scalar:
                    for coordinate, value in zip(x, y, strict=True):
                        saver.add_result(("x", coordinate), ("signal", value))
                else:
                    saver.add_result(("x", x), ("signal", y))
            guids.append(saver.dataset.guid)
    finally:
        experiment.conn.close()
    protected = database_state(path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        plots = []
        for guid in guids:
            prior_count = len(window.windows)
            window.openPlot(guid=guid, show=True)
            wait_for(lambda prior_count=prior_count: len(window.windows) > prior_count)
            plot = window.windows[-1]
            wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
            plot.monitor.stop()
            if heatmap:
                plot.z_index = [0, 0]
                plot.openSweep("h")
                plot = window.windows[-1]
                wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
                plot.monitor.stop()
            plots.append(plot)
        yield window, plots, coordinates, values
    finally:
        close_main_window(window)
        assert database_state(path) == protected


def assert_spectrum(line, frequencies, magnitudes):
    assert_display(line, frequencies, magnitudes)
    assert_display(line.curve, frequencies, magnitudes)


@pytest.mark.parametrize("fft_plots", [
    NONUNIFORM, UNIFORM, {"x": NONUNIFORM, "scalar": True},
], indirect=True)
@pytest.mark.parametrize("subtract_mean", [False, True])
def test_descending_fft_matches_increasing(fft_plots, no_callback_errors, subtract_mean):
    _window, (increasing, descending), x, y = fft_plots
    resampled = [1., 13., 39., 82.] if np.array_equal(x, NONUNIFORM) else y
    if subtract_mean:
        resampled = np.asarray(resampled) - np.mean(y)
    frequencies = np.array([0., 1 / 12, 1 / 6])
    magnitudes = np.abs(np.fft.rfft(resampled) / 4)
    for plot, raw_x, raw_y in ((increasing, x, y), (descending, x[::-1], y[::-1])):
        if subtract_mean:
            click_control(plot, "subtractMeanCheck")
        for _ in range(2):
            click_control(plot, "fftCheck")
            assert plot.plot.ctrl.fftCheck.isChecked()
            assert_spectrum(plot.line, frequencies, magnitudes)
            assert_samples(plot.line, raw_x, raw_y)
            assert_axis(plot, "bottom", "Frequency of Coordinate", "1/V")
            plot.refreshWindow(force=True)
            wait_for(lambda plot=plot: not plot.worker.running)
            plot.monitor.stop()
            assert_spectrum(plot.line, frequencies, magnitudes)
            click_control(plot, "fftCheck")
            assert_display(plot.line, raw_x, raw_y - np.mean(y) if subtract_mean else raw_y)


@pytest.mark.parametrize("fft_plots", [NONUNIFORM], indirect=True)
def test_descending_fft_secondary_log_snap_and_phase(fft_plots, no_callback_errors):
    window, (increasing, descending), x, y = fft_plots
    click_control(descending, "fftCheck")
    key, line = merge(window, descending, increasing, x_axis="Top", y_axis="Right")
    frequencies = np.array([0., 1 / 12, 1 / 6])
    magnitudes = np.abs(np.fft.rfft([1., 13., 39., 82.]) / 4)
    assert_spectrum(line, frequencies, magnitudes)
    descending.snap_to_trace_action.trigger()
    for log_x in (True, False, True):
        descending._axis_scale_log_toggled("x", log_x)
        descending._axis_scale_log_toggled("x2", log_x)
        raw_x, raw_y = (frequencies[1:], magnitudes[1:]) if log_x else (frequencies, magnitudes)
        view_x = np.log10(raw_x) if log_x else raw_x
        for trace_key, trace in descending.lines.items():
            for other in descending.lines.values():
                other.setVisible(other is trace)
            owner = trace.getViewBox()
            set_sample_range(owner, view_x, raw_y, 0, len(raw_x) - 1)
            assert_spectrum(trace, view_x, raw_y)
            assert_full_snap_data(trace, raw_x, raw_y, view_x, raw_y)
            for index in (0, len(raw_x) - 1):
                assert_snap(descending, owner, trace_key, raw_x, raw_y, view_x, raw_y, index)
        for trace in descending.lines.values():
            trace.setVisible(True)
    assert key in descending.lines
    click_control(descending, "subtractMeanCheck")
    click_control(descending, "phasemapCheck")
    slopes = np.diff(y) / np.diff(x)
    for trace, raw_y, expected_y in (
        (descending.line, y[::-1], slopes[::-1]), (line, y, slopes),
    ):
        assert_spectrum(trace, np.log10(raw_y[:-1]), expected_y)
        assert_full_snap_data(trace, raw_y[:-1], expected_y, np.log10(raw_y[:-1]), expected_y)
    click_control(descending, "phasemapCheck")
    expected = np.abs(np.fft.rfft(np.array([1., 13., 39., 82.]) - np.mean(y)) / 4)
    assert_spectrum(descending.line, np.log10(frequencies[1:]), expected[1:])


@pytest.mark.parametrize("fft_plots", [NONUNIFORM], indirect=True)
def test_descending_derived_plot_fft(fft_plots, no_callback_errors):
    _window, (increasing, descending), x, y = fft_plots
    derived = np.gradient(y, x)
    resampled = np.interp([0., 3., 6., 9.], x, derived)
    expected = np.abs(np.fft.rfft(resampled) / 4)
    for plot in (increasing, descending):
        operation_option(plot, "dy/dx").input.setChecked(True)
        assert apply_operations(plot)[1:] == ([True], [])
        raw_x, raw_y = plot.line.getOriginalDataset()
        click_control(plot, "fftCheck")
        assert_spectrum(plot.line, [0., 1 / 12, 1 / 6], expected)
        assert_samples(plot.line, raw_x, raw_y)
        assert_axis(plot, "left", "FFT magnitude of d(Signal)/d(Coordinate)", "A/V")


@pytest.mark.parametrize("fft_plots", [
    {"x": NONUNIFORM, "heatmap": True}, {"x": UNIFORM, "heatmap": True},
], indirect=True)
def test_real_heatmap_cut_fft(fft_plots, no_callback_errors):
    _window, (increasing, descending), x, y = fft_plots
    expected_y = [1., 13., 39., 82.] if np.array_equal(x, NONUNIFORM) else y
    for cut in (increasing, descending):
        raw_x, raw_y = cut.line.getOriginalDataset()
        click_control(cut, "fftCheck")
        expected = np.abs(np.fft.rfft(expected_y) / 4)
        assert_spectrum(cut.line, [0., 1 / 12, 1 / 6], expected)
        assert_samples(cut.line, raw_x, raw_y)
        cut.picker.slider.setValue(1)
        assert_spectrum(cut.line, [0., 1 / 12, 1 / 6], expected)
        click_control(cut, "fftCheck")
        assert_display(cut.line, raw_x, raw_y)


@pytest.mark.parametrize("fft_plots", [
    [2., 2., 2., 2.],  # zero span
    [0., 3., 1., 4.],  # reversing, nonzero span
    [0., 2., 1., 0.],  # return to start
    [0., 1., 1., 4.],  # repeated coordinate
    [0., 1., np.inf, 4.],
], indirect=True)
def test_unsupported_fft_inputs_and_recovery(fft_plots, no_callback_errors):
    window, (increasing, descending), x, y = fft_plots
    key, secondary = merge(window, descending, increasing, x_axis="Top", y_axis="Right")
    click_control(descending, "subtractMeanCheck")
    with np.errstate(all="raise"):
        click_control(descending, "fftCheck")
        assert not descending.plot.ctrl.fftCheck.isChecked()
        for line, raw_x, raw_y in (
            (descending.line, x[::-1], y[::-1]), (secondary, x, y),
        ):
            assert not line.opts["fftMode"]
            assert_spectrum(line, raw_x, raw_y - np.mean(y))
            assert_full_snap_data(line, raw_x, raw_y - np.mean(y), raw_x, raw_y - np.mean(y))
        assert "FFT requires" in descending.statusBar().currentMessage()
        with np.errstate(divide="ignore", invalid="ignore"):
            click_control(descending, "logXCheck")
            log_x = np.log10(x[::-1])
            log_x[~np.isfinite(log_x)] = np.nan
            assert_display(descending.line, log_x, y[::-1] - np.mean(y))
            click_control(descending, "logXCheck")
        descending.refreshWindow(force=True)
        wait_for(lambda: not descending.worker.running)
        descending.monitor.stop()
        assert_display(descending.line, x[::-1], y[::-1] - np.mean(y))
        # Retrying the still-invalid trace remains a safe rejected toggle.
        click_control(descending, "fftCheck")
    assert not descending.plot.ctrl.fftCheck.isChecked()
    assert "FFT requires" in descending.statusBar().currentMessage()
    assert_samples(descending.line, x[::-1], y[::-1])
    assert_samples(secondary, x, y)
    assert_display(descending.line, x[::-1], y[::-1] - np.mean(y))
    assert key in descending.lines


@pytest.mark.parametrize("fft_plots", [
    np.array([2**63 + n for n in (0, 4, 7, 9)], dtype=np.uint64),
    np.array([2**53 + n for n in (0, 4, 7, 9)], dtype=np.int64),
], indirect=True)
def test_integer_fft_coordinates_keep_spacing_and_order(fft_plots, no_callback_errors):
    _window, (increasing, descending), x, y = fft_plots
    expected = np.abs(np.fft.rfft([1., 13., 39., 82.]) / 4)
    for plot, raw_x, raw_y in ((increasing, x, y), (descending, x[::-1], y[::-1])):
        click_control(plot, "fftCheck")
        assert_spectrum(plot.line, [0., 1 / 12, 1 / 6], expected)
        assert_samples(plot.line, raw_x, raw_y)
        assert plot.line.getOriginalDataset()[0].dtype == x.dtype


@pytest.mark.parametrize("fft_plots", [[5.]], indirect=True)
def test_single_sample_fft_keeps_native_dc_semantics(fft_plots, no_callback_errors):
    _window, plots, _x, _y = fft_plots
    for plot in plots:
        click_control(plot, "fftCheck")
        assert_spectrum(plot.line, [0.], [1.])
        click_control(plot, "subtractMeanCheck")
        assert_spectrum(plot.line, [0.], [0.])
        click_control(plot, "logXCheck")
        assert_display(plot.line, [], [])


@pytest.mark.parametrize("fft_plots", [[0., 3., 1., 4.]], indirect=True)
def test_reversing_phase_precedence_and_valid_coordinate_recovery(fft_plots, no_callback_errors):
    _window, (_increasing, descending), x, y = fft_plots
    x, y = x[::-1], y[::-1]
    click_control(descending, "phasemapCheck")
    click_control(descending, "fftCheck")
    click_control(descending, "subtractMeanCheck")
    assert_spectrum(descending.line, y[:-1], np.diff(y) / np.diff(x))
    assert "FFT requires" not in descending.statusBar().currentMessage()
    click_control(descending, "phasemapCheck")
    assert not descending.plot.ctrl.fftCheck.isChecked()
    assert_spectrum(descending.line, x, y - np.mean(y))
    assert "FFT requires" in descending.statusBar().currentMessage()
    # A real axis reassignment refreshes from QCoDeS with valid coordinates.
    assert descending.set_plot_axes_swapped(True)
    wait_for(lambda: not descending.worker.running)
    descending.monitor.stop()
    click_control(descending, "fftCheck")
    assert descending.plot.ctrl.fftCheck.isChecked()
    increasing_y = y[::-1]
    samples = np.interp(np.linspace(1., 82., 4), increasing_y, (x - np.mean(x))[::-1])
    assert_spectrum(descending.line, np.fft.rfftfreq(4, 27.), np.abs(np.fft.rfft(samples) / 4))
    assert "FFT requires" not in descending.statusBar().currentMessage()


def test_nan_coordinate_guard_before_pyqtgraph_interpolation():
    # Ordinary QCoDeS publication filters NaN coordinates. Exercise the guard
    # too for derived samples supplied directly to the shared native item.
    line = NativePlotDataItem(x=[0., np.nan, 2., 4.], y=[1., 17., 50., 82.])
    with np.errstate(all="raise"):
        line.setFftMode(True)
    assert_display(line, [], [])
    assert "FFT requires" in line._qplot_fft_error


ZERO_SPAN_AND_VALID = {
    "x": [1., 1., 1., 1.], "y": [2., 2., 2., 2.], "scalar": True,
    "other_x": [0., 1., 2., 3.], "other_y": [1., 5., 10., 17.],
}


@pytest.mark.parametrize("fft_plots", [ZERO_SPAN_AND_VALID], indirect=True)
@pytest.mark.parametrize("invalid_primary", [False, True])
@pytest.mark.parametrize("merge_after_activation", [False, True])
@pytest.mark.parametrize("subtract_mean", [False, True])
def test_zero_span_fft_rejection_keeps_ui_exports_and_retry_usable(
    fft_plots, no_callback_errors, tmp_path, invalid_primary, merge_after_activation, subtract_mean,
):
    window, (invalid, valid), _x, _y = fft_plots
    host, source = (invalid, valid) if invalid_primary else (valid, invalid)
    if subtract_mean:
        click_control(host, "subtractMeanCheck")
    if merge_after_activation:
        click_control(host, "fftCheck")
        assert host.plot.ctrl.fftCheck.isChecked() == (not invalid_primary)
    key, secondary = merge(window, host, source, x_axis="Top", y_axis="Right")
    if not merge_after_activation:
        with np.errstate(all="raise"):
            click_control(host, "fftCheck")
    assert not host.plot.ctrl.fftCheck.isChecked()
    assert not host.plot.saveState()["fftCheck"]
    assert "FFT requires" in host.statusBar().currentMessage()
    label = "Signal - mean(Signal)" if subtract_mean else "Signal"
    for side in ("bottom", "top"):
        assert_axis(host, side, "Coordinate", "V")
    for side in ("left", "right"):
        assert_axis(host, side, label, "A")
    host.snap_to_trace_action.trigger()
    originals = []
    for trace_key, line in host.lines.items():
        assert not line.opts["fftMode"]
        x, y = line.getOriginalDataset()
        originals.append((x.copy(), y.copy()))
        mapped_y = y - np.mean(y) if subtract_mean else y
        for _ in range(3):
            assert_spectrum(line, x, mapped_y)
        for other in host.lines.values():
            other.setVisible(other is line)
        owner = line.getViewBox()
        owner.setRange(xRange=[min(x) - 1, max(x) + 1],
                       yRange=[min(mapped_y) - 1, max(mapped_y) + 1], padding=0)
        assert_full_snap_data(line, x, mapped_y, x, mapped_y)
        assert_snap(host, owner, trace_key, x, mapped_y, x, mapped_y, 0)
    for line in host.lines.values():
        line.setVisible(True)
    target = tmp_path / "after-rejected-fft.csv"
    assert host._write_line_csv_stage(str(target), CSVExporter(host.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    for pair, (x, y) in enumerate(originals):
        actual = np.array([[float(row[2 * pair]), float(row[2 * pair + 1])] for row in rows])
        np.testing.assert_array_equal(actual, np.column_stack((x, y)))
    image = host._plot_image_at_size(QtCore.QSize(640, 400))
    assert not image.isNull()
    assert image.save(str(tmp_path / "after-rejected-fft.png"))
    if not invalid_primary:
        # Removing the invalid secondary leaves real QCoDeS samples eligible
        # for a successful retry in the same plot and actual FFT control.
        host.remove_line(source.label, trace_key=key)
        assert secondary not in host.plot.items
        click_control(host, "fftCheck")
        assert host.plot.ctrl.fftCheck.isChecked()
        x, y = host.line.getOriginalDataset()
        samples = y - np.mean(y) if subtract_mean else y
        assert_spectrum(host.line, np.fft.rfftfreq(4), np.abs(np.fft.rfft(samples) / 4))
        assert "FFT requires" not in host.statusBar().currentMessage()


@pytest.mark.parametrize("fft_plots", [{
    "x": [0., 1., 2., 3.], "y": [2., 2., 2., 2.], "scalar": True,
}], indirect=True)
def test_active_fft_rolls_back_all_curves_on_real_coordinate_refresh(fft_plots, no_callback_errors):
    window, (host, source), _x, _y = fft_plots
    _key, secondary = merge(window, host, source, x_axis="Top", y_axis="Right")
    click_control(host, "fftCheck")
    assert_spectrum(host.line, np.fft.rfftfreq(4), [2., 0., 0.])
    # Swapping the actual plot axes refreshes from the same read-only QCoDeS
    # dataset and makes the constant signal the coordinate.
    assert host.set_plot_axes_swapped(True)
    wait_for(lambda: not host.worker.running)
    host.monitor.stop()
    assert not host.plot.ctrl.fftCheck.isChecked()
    assert "FFT requires" in host.statusBar().currentMessage()
    for line in (host.line, secondary):
        assert not line.opts["fftMode"]
        assert_spectrum(line, *line.getOriginalDataset())
    assert host.set_plot_axes_swapped(False)
    wait_for(lambda: not host.worker.running)
    host.monitor.stop()
    click_control(host, "fftCheck")
    assert host.plot.ctrl.fftCheck.isChecked()
    for line in (host.line, secondary):
        assert_spectrum(line, np.fft.rfftfreq(4), [2., 0., 0.])


@pytest.mark.parametrize("fft_plots", [[0., 3., 1., 4.]], indirect=True)
def test_rejected_fft_retains_previous_native_derivative(fft_plots, no_callback_errors):
    _window, plots, _x, _y = fft_plots
    for plot in plots:
        x, y = plot.line.getOriginalDataset()
        click_control(plot, "derivativeCheck")
        click_control(plot, "fftCheck")
        assert plot.plot.ctrl.derivativeCheck.isChecked()
        assert not plot.plot.ctrl.fftCheck.isChecked()
        assert plot.line.opts["derivativeMode"]
        assert_spectrum(plot.line, x[:-1], np.diff(y) / np.diff(x))
        assert_axis(plot, "left", "d(Signal)/d(Coordinate)", "A/V")
