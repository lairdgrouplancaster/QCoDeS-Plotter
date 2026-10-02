"""Integer-safe native controls through actual QCoDeS storage and Qt slots."""

import csv
from decimal import Decimal

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import main as main_window
from qplot.windows._plot1d_snap import _line_snap_data
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_averaging import assert_average, click_average
from tests.windows.test_native_transform_labels import (
    SUPPORTED_CONTROLS,
    assert_display,
    click_control,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_native_transform_snapping import assert_snap
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for

no_callback_errors = _no_callback_errors_fixture

_UINT_MAX = 2**64 - 1
_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1
_CASES = {
    "reported_uint_y": (
        np.arange(4), np.array([10, 8, 6, 4], dtype=np.uint64), [-2, -2, -2],
    ),
    "adjacent_uint_y": (
        np.arange(4), np.array([2**63 + n for n in (3, 2, 1, 0)], dtype=np.uint64),
        [-1, -1, -1],
    ),
    "adjacent_uint_max_y": (
        np.arange(4), np.array([_UINT_MAX - n for n in (0, 1, 2, 3)], dtype=np.uint64),
        [-1, -1, -1],
    ),
    "adjacent_signed_min_y": (
        np.arange(4), np.array([_INT_MIN + n for n in (3, 2, 1, 0)], dtype=np.int64),
        [-1, -1, -1],
    ),
    "adjacent_signed_max_y": (
        np.arange(4), np.array([_INT_MAX - n for n in (0, 1, 2, 3)], dtype=np.int64),
        [-1, -1, -1],
    ),
    "adjacent_descending_uint_x": (
        np.array([2**63 + n for n in (3, 2, 1, 0)], dtype=np.uint64),
        np.array([10., 8., 6., 4.]), [2, 2, 2],
    ),
    "uint_signed_boundary": (
        np.arange(4), np.array([2**63 + n for n in (-1, 0, 1, 2)], dtype=np.uint64),
        [1, 1, 1],
    ),
    "full_uint_range": (
        np.array([_UINT_MAX, 0, _UINT_MAX, 0], dtype=np.uint64),
        np.array([0, _UINT_MAX, 0, _UINT_MAX], dtype=np.uint64), [-1, -1, -1],
    ),
    "signed_y_overflow": (
        np.arange(4), np.array([_INT_MIN, _INT_MAX, _INT_MIN, _INT_MAX]),
        [float(_UINT_MAX), -float(_UINT_MAX), float(_UINT_MAX)],
    ),
    "signed_x_overflow": (
        np.array([_INT_MIN, _INT_MAX, _INT_MIN, _INT_MAX]),
        np.array([_INT_MAX, _INT_MIN, _INT_MAX, _INT_MIN]), [-1, -1, -1],
    ),
    "narrow_signed_overflow": (
        np.array([127, -128, 127, -128], dtype=np.int8),
        np.array([-128, 127, -128, 127], dtype=np.int8), [-1, -1, -1],
    ),
    "adjacent_signed_y_float_x": (
        np.arange(4, dtype=float), np.array([2**53 + n for n in (3, 2, 1, 0)]),
        [-1, -1, -1],
    ),
    "nonuniform_uint": (
        np.array([10, 8, 4, 3], dtype=np.uint64),
        np.array([10, 8, 6, 4], dtype=np.uint64), [1, .5, 2],
    ),
    "single_sample": (
        np.array([0], dtype=np.uint64), np.array([2**63], dtype=np.uint64), [],
    ),
}


@pytest.fixture
def integer_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    case, heatmap = request.param
    x, y, slopes = _CASES[case]
    path = tmp_path / "integer-transforms.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("integer transforms", sample_name="test")
    guids = []
    try:
        for _ in range(2):
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter("x", paramtype="array")
            if heatmap:
                measurement.register_custom_parameter("slow", paramtype="array")
            measurement.register_custom_parameter(
                "signal", paramtype="array", setpoints=("slow", "x") if heatmap else ("x",),
            )
            with measurement.run(write_in_background=False) as saver:
                if heatmap:
                    saver.add_result(
                        ("x", np.tile(x, (2, 1))),
                        ("slow", np.repeat(np.arange(2)[:, None], len(x), axis=1)),
                        ("signal", np.tile(y, (2, 1))),
                    )
                else:
                    saver.add_result(("x", x), ("signal", y))
            stored = saver.dataset.get_parameter_data("signal")["signal"]
            for name, original in (("x", x), ("signal", y)):
                assert stored[name].dtype == original.dtype
                np.testing.assert_array_equal(stored[name].ravel()[:len(x)], original)
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
            window.openPlot(guid=guid, show=True)
            plot = window.windows[-1]
            wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
            plot.monitor.stop()
            if heatmap:
                plot.z_index = [0, 0]
                plot.openSweep("h")
                plot = window.windows[-1]
                wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
                plot.monitor.stop()
            # Extreme-range cases test arithmetic, without rendering-only
            # clipping imposed by an unrelated viewbox's current range.
            plot.line.setDynamicRangeLimit(None)
            plots.append(plot)
        yield window, plots, x, y, slopes
    finally:
        close_main_window(window)
        assert database_state(path) == protected


def assert_integer_mapping(plot, x, y, slopes, *, phase=False):
    mapped_x = y[:-1] if phase else x[:-1]
    if mapped_x.dtype.kind == "O":
        mapped_x = mapped_x.astype(float)
    for line in getattr(plot, "lines", {None: plot.line}).values():
        assert_display(line, mapped_x, slopes)
        assert_display(line.curve, mapped_x, slopes)
        original = line.getOriginalDataset()
        for actual, expected in zip(original, (x, y), strict=True):
            assert actual.dtype == expected.dtype
            np.testing.assert_array_equal(actual, expected)
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
        np.testing.assert_array_equal(line.getData()[1], y)
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
            for actual, expected in zip(line.getData(), (x, y), strict=True):
                np.testing.assert_array_equal(actual, expected)


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
    plot.snap_to_trace_action.trigger()
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
