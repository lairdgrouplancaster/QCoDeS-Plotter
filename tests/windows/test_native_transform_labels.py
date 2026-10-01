"""Native Options/Transforms labels and values in real Qt/QCoDeS plots."""

import csv

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_cut_cursor_integration import (
    heatmap_cut as _heatmap_cut_fixture,
)
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_merged_trace_metadata import (
    assert_axis,
    assert_samples,
    merge,
)
from tests.windows.test_merged_trace_metadata import (
    merged_plots as _merged_plots_fixture,
)
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)

merged_plots = _merged_plots_fixture
heatmap_cut = _heatmap_cut_fixture


def set_controls(plot, controls, enabled=True):
    # Phase map resets both coordinates. Set it first and unset it last so
    # FFT+derivative never briefly produces unequal arrays in PyQtGraph.
    ordered = ("phasemapCheck", "fftCheck", "derivativeCheck", "subtractMeanCheck")
    for name in ordered if enabled else reversed(ordered):
        if name in controls:
            getattr(plot.plot.ctrl, name).setChecked(enabled)


def assert_display(line, x, y):
    actual_x, actual_y = line.getData()
    np.testing.assert_allclose(actual_x, x, equal_nan=True)
    np.testing.assert_allclose(actual_y, y, equal_nan=True)


def expected_mapping(x, y, controls):
    mapped_x, mapped_y = x, y
    if "subtractMeanCheck" in controls:
        mapped_y = y - np.mean(y)
    if "fftCheck" in controls:
        dx = np.diff(x)
        if np.any(np.abs(dx - dx[0]) > abs(dx[0]) / 1000):
            uniform_x = np.linspace(x[0], x[-1], len(x))
            mapped_y = np.interp(uniform_x, x, mapped_y)
        spacing = float(x[-1] - x[0]) / (len(x) - 1)
        mapped_x = np.fft.rfftfreq(len(x), spacing)
        mapped_y = np.abs(np.fft.rfft(mapped_y) / len(y))
    if "derivativeCheck" in controls:
        mapped_x = mapped_x[:-1]
        mapped_y = np.diff(y) / np.diff(x)
    if "phasemapCheck" in controls:
        mapped_x = y[:-1]
        mapped_y = np.diff(y) / np.diff(x)
    return mapped_x, mapped_y


SUPPORTED_CONTROLS = [
    # FFT + derivative without phase map is not supported by PyQtGraph 0.14:
    # FFT shortens X but derivative restores the longer input-sample Y.
    ("derivativeCheck",),
    ("fftCheck",),
    ("phasemapCheck",),
    ("phasemapCheck", "derivativeCheck"),
    ("phasemapCheck", "fftCheck"),
    ("phasemapCheck", "fftCheck", "derivativeCheck"),
]


@pytest.mark.parametrize("controls", SUPPORTED_CONTROLS)
@pytest.mark.parametrize("subtract_mean", [False, True])
def test_native_controls_values_labels_refresh_and_restore(merged_plots, controls, subtract_mean):
    _window, (plot, _source, _other), x, y = merged_plots
    metadata = {name: (param, param.label, param.unit) for name, param in plot.axis_param.items()}
    if subtract_mean:
        controls = (*controls, "subtractMeanCheck")
    set_controls(plot, controls)
    expected_x, expected_y = expected_mapping(x, y, controls)
    expected_x_label = "Signal" if "phasemapCheck" in controls else (
        "Frequency of Gate voltage" if "fftCheck" in controls else "Gate voltage"
    )
    expected_x_unit = "nA" if "phasemapCheck" in controls else (
        "1/V" if "fftCheck" in controls else "V"
    )
    expected_y_label = "d(Signal)/d(Gate voltage)" if (
        "phasemapCheck" in controls or "derivativeCheck" in controls
    ) else ("FFT magnitude of (Signal - mean(Signal))" if subtract_mean else "FFT magnitude of Signal")
    expected_y_unit = "nA/V" if (
        "phasemapCheck" in controls or "derivativeCheck" in controls
    ) else "nA"
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            worker = plot.worker
            wait_for(lambda worker=worker: not worker.running)
            plot.monitor.stop()
        assert_display(plot.line, expected_x, expected_y)
        assert_axis(plot, "bottom", expected_x_label, expected_x_unit)
        assert_axis(plot, "left", expected_y_label, expected_y_unit)
        assert_samples(plot.line, x, y)
        for name, (param, label, unit) in metadata.items():
            assert (plot.axis_param[name].label, plot.axis_param[name].unit) == (label, unit)
            assert (param.label, param.unit) == (label, unit)
    set_controls(plot, controls, False)
    assert_display(plot.line, x, y)
    assert_axis(plot, "bottom", "Gate voltage", "V")
    assert_axis(plot, "left", "Signal", "nA")


@pytest.mark.parametrize("controls", SUPPORTED_CONTROLS)
@pytest.mark.parametrize("merge_after_toggle", [False, True])
def test_merged_controls_secondary_axes_and_source_operations(merged_plots, controls, merge_after_toggle):
    window, (host, source, _other), x, y = merged_plots
    if merge_after_toggle:
        set_controls(host, controls)
    key, line = merge(window, host, source, x_axis="Top")
    if not merge_after_toggle:
        set_controls(host, controls)
    for operation_enabled in (True, False):
        operation_option(source, "dy/dx").input.setChecked(operation_enabled)
        assert apply_operations(source)[1:] == ([True], [])
        input_y = np.gradient(y, x) if operation_enabled else y
        input_label = "d(Signal)/d(Gate voltage)" if operation_enabled else "Signal"
        input_unit = "nA/V" if operation_enabled else "nA"
        derivative_label = f"d({input_label})/d(Gate voltage)"
        derivative_unit = "(nA/V)/V" if operation_enabled else "nA/V"
        phase = "phasemapCheck" in controls
        fft = "fftCheck" in controls
        derivative = phase or "derivativeCheck" in controls
        assert_display(line, *expected_mapping(x, input_y, controls))
        assert_samples(line, x, input_y)
        assert_axis(host, "top", input_label if phase else (
            "Frequency of Gate voltage" if fft else "Gate voltage"
        ), input_unit if phase else ("1/V" if fft else "V"))
        assert_axis(host, "right", derivative_label if derivative else f"FFT magnitude of {input_label}",
                    derivative_unit if derivative else input_unit)
        # Moving a transformed trace preserves its processing and re-resolves
        # the first trace supplying each shared axis label.
        host._set_trace_y_axis(key, "Left")
        assert_axis(host, "right", "", "")
        host._set_trace_y_axis(key, "Right")
        assert_display(line, *expected_mapping(x, input_y, controls))
    set_controls(host, controls, False)
    assert_axis(host, "top", "Gate voltage", "V")
    assert_axis(host, "right", "Signal", "nA")
    assert_display(line, x, y)


@pytest.mark.parametrize("control", ["derivativeCheck", "fftCheck", "phasemapCheck"])
def test_operations_precede_native_controls_and_axis_swap(merged_plots, control):
    _window, (plot, _source, _other), x, y = merged_plots
    operation_option(plot, "dy/dx").input.setChecked(True)
    assert apply_operations(plot)[1:] == ([True], [])
    input_y = np.gradient(y, x)
    set_controls(plot, (control,))
    assert_display(plot.line, *expected_mapping(x, input_y, (control,)))
    assert_axis(plot, "left", "FFT magnitude of d(Signal)/d(Gate voltage)" if control == "fftCheck"
                else "d(d(Signal)/d(Gate voltage))/d(Gate voltage)",
                "nA/V" if control == "fftCheck" else "(nA/V)/V")
    set_controls(plot, (control,), False)
    assert_axis(plot, "left", "d(Signal)/d(Gate voltage)", "nA/V")
    assert plot.set_plot_axes_swapped(True)
    wait_for(lambda: not plot.worker.running)
    # Use the committed, swapped operation output; neither metadata nor
    # native derivative semantics may assume the dependent parameter is Y.
    raw_x, raw_y = plot.line.getOriginalDataset()
    x_param, y_param = plot.axis_param["x"], plot.axis_param["y"]
    set_controls(plot, (control,))
    assert_display(plot.line, *expected_mapping(raw_x, raw_y, (control,)))
    if control == "phasemapCheck":
        assert_axis(plot, "bottom", y_param.label, y_param.unit)
    elif control == "fftCheck":
        reciprocal = f"1/({x_param.unit})" if "/" in x_param.unit else f"1/{x_param.unit}"
        assert_axis(plot, "bottom", f"Frequency of {x_param.label}", reciprocal)
    else:
        assert_axis(plot, "bottom", x_param.label, x_param.unit)


@pytest.mark.parametrize("controls", SUPPORTED_CONTROLS)
def test_cut_controls_moving_and_reassigning_coordinate(heatmap_cut, controls):
    _heatmap, cut = heatmap_cut
    set_controls(cut, controls)
    for reassign in (False, True):
        if reassign:
            cut.axis_dropdown["x"].setCurrentText(cut.fixed_indep)
            wait_for(lambda: cut._axis_change_transaction is None and not cut.worker.running)
        cut.picker.slider.setValue(3)
        x, y = cut.line.getOriginalDataset()
        assert_display(cut.line, *expected_mapping(x, y, controls))
        x_param, y_param = cut.axis_param["x"], cut.axis_param["y"]
        phase, fft = "phasemapCheck" in controls, "fftCheck" in controls
        assert_axis(cut, "bottom", y_param.label if phase else (
            f"Frequency of {x_param.label}" if fft else x_param.label
        ), y_param.unit if phase else (f"1/{x_param.unit}" if fft else x_param.unit))
        derivative = phase or "derivativeCheck" in controls
        assert_axis(cut, "left", f"d({y_param.label})/d({x_param.label})" if derivative
                    else f"FFT magnitude of {y_param.label}",
                    f"{y_param.unit}/{x_param.unit}" if derivative else y_param.unit)
    set_controls(cut, controls, False)
    assert_axis(cut, "bottom", x_param.label, x_param.unit)
    assert_axis(cut, "left", y_param.label, y_param.unit)


@pytest.fixture
def unit_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "coordinate-units.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("native units", sample_name="test")
    coordinate_unit, signal_unit = request.param
    coordinate = ManualParameter("coordinate", label="Coordinate", unit=coordinate_unit)
    signal = ManualParameter("signal", label="Signal", unit=signal_unit)
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(coordinate)
    measurement.register_parameter(signal, setpoints=(coordinate,))
    with measurement.run(write_in_background=False) as saver:
        for x in range(8):
            saver.add_result((coordinate, x), (signal, x ** 2 + 1))
        guid = saver.dataset.guid
    experiment.conn.close()
    protected = database_artifact_state(path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        yield plot
    finally:
        close_main_window(window)
        assert database_artifact_state(path) == protected


@pytest.mark.parametrize("unit_plot,fft_unit,derivative_unit", [
    (("s", "nA"), "1/s", "nA/s"),
    (("", "nA"), "", "nA"),
    (("V", ""), "1/V", "1/V"),
    (("", ""), "", ""),
    (("nA/V", "V"), "1/(nA/V)", "V/(nA/V)"),
], indirect=["unit_plot"])
def test_coordinate_units_and_csv_semantics(unit_plot, fft_unit, derivative_unit, tmp_path):
    plot = unit_plot
    x, y = plot.line.getOriginalDataset()
    for control in ("fftCheck", "derivativeCheck", "phasemapCheck"):
        set_controls(plot, (control,))
        assert_display(plot.line, *expected_mapping(x, y, (control,)))
        if control == "fftCheck":
            assert_axis(plot, "bottom", "Frequency of Coordinate", fft_unit)
            assert_axis(plot, "left", "FFT magnitude of Signal", plot.param.unit)
        else:
            assert_axis(plot, "left", "d(Signal)/d(Coordinate)", derivative_unit)
        # PyQtGraph's existing CSV exporter exports original samples even
        # while its curve displays native transformed coordinates.
        output = tmp_path / f"{control}.csv"
        assert plot._write_line_csv_stage(str(output), CSVExporter(plot.plot))
        with output.open(newline="") as stream:
            rows = list(csv.reader(stream))
        exported = np.asarray([[float(row[0]), float(row[1])] for row in rows[1:]])
        np.testing.assert_allclose(exported, np.column_stack((x, y)))
        set_controls(plot, (control,), False)
        assert_axis(plot, "bottom", "Coordinate", plot.axis_param["x"].unit)
        assert_axis(plot, "left", "Signal", plot.param.unit)


@pytest.mark.parametrize("controls", [("fftCheck",), ("derivativeCheck",), ("phasemapCheck",)])
@pytest.mark.parametrize("merged_plots", [[1, 2, 3, 4]], indirect=True)
def test_log_ticks_keep_transformed_quantities(merged_plots, controls):
    _window, (plot, _source, _other), x, y = merged_plots
    set_controls(plot, controls)
    labels = {
        side: (plot.plot.getAxis(side).labelText, plot.plot.getAxis(side).labelUnits)
        for side in ("bottom", "left")
    }
    plot.plot.setLogMode(x=True, y=True)
    expected_x, expected_y = expected_mapping(x, y, controls)
    if "fftCheck" in controls:
        expected_x, expected_y = expected_x[1:], expected_y[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        expected_x, expected_y = np.log10(expected_x), np.log10(expected_y)
    expected_x[~np.isfinite(expected_x)] = np.nan
    expected_y[~np.isfinite(expected_y)] = np.nan
    assert_display(plot.line, expected_x, expected_y)
    for side, (label, unit) in labels.items():
        assert_axis(plot, side, label, unit)
    plot.plot.setLogMode(x=False, y=False)
    set_controls(plot, controls, False)
    assert_display(plot.line, x, y)


def test_subtract_mean_alone_and_derivative_override(merged_plots):
    _window, (plot, _source, _other), x, y = merged_plots
    set_controls(plot, ("subtractMeanCheck",))
    assert_display(plot.line, x, y - np.mean(y))
    assert_axis(plot, "left", "Signal - mean(Signal)", "nA")
    set_controls(plot, ("derivativeCheck",))
    assert_display(plot.line, x[:-1], np.diff(y) / np.diff(x))
    assert_axis(plot, "left", "d(Signal)/d(Gate voltage)", "nA/V")
    set_controls(plot, ("derivativeCheck",), False)
    assert_axis(plot, "left", "Signal - mean(Signal)", "nA")
    set_controls(plot, ("subtractMeanCheck",), False)
    assert_display(plot.line, x, y)
    assert_axis(plot, "left", "Signal", "nA")


@pytest.mark.parametrize("control", ["derivativeCheck", "fftCheck", "phasemapCheck"])
def test_cut_operation_metadata_precedes_native_transform(heatmap_cut, control):
    _heatmap, cut = heatmap_cut
    operation_option(cut, "Differentiate Cut").input.setChecked(True)
    assert apply_operations(cut)[1:] == ([True], [])
    x, y = cut.line.getOriginalDataset()
    x_param, y_param = cut.axis_param["x"], cut.axis_param["y"]
    assert y_param.label == f"d(Conductance)/d({x_param.label})"
    set_controls(cut, (control,))
    assert_display(cut.line, *expected_mapping(x, y, (control,)))
    if control == "fftCheck":
        assert_axis(cut, "left", f"FFT magnitude of {y_param.label}", y_param.unit)
    else:
        assert_axis(cut, "left", f"d({y_param.label})/d({x_param.label})",
                    f"({y_param.unit})/{x_param.unit}")
    set_controls(cut, (control,), False)
    assert_axis(cut, "left", y_param.label, y_param.unit)
    assert (cut.param.label, cut.param.unit) == ("Conductance", "uS")


def test_turning_off_phase_map_restores_remaining_transform_labels(merged_plots):
    _window, (plot, _source, _other), x, y = merged_plots
    set_controls(plot, ("phasemapCheck", "fftCheck", "subtractMeanCheck"))
    assert_axis(plot, "bottom", "Signal", "nA")
    assert_axis(plot, "left", "d(Signal)/d(Gate voltage)", "nA/V")
    set_controls(plot, ("phasemapCheck",), False)
    assert_display(plot.line, *expected_mapping(x, y, ("fftCheck", "subtractMeanCheck")))
    assert_axis(plot, "bottom", "Frequency of Gate voltage", "1/V")
    assert_axis(plot, "left", "FFT magnitude of (Signal - mean(Signal))", "nA")
    set_controls(plot, ("fftCheck",), False)
    assert_display(plot.line, x, y - np.mean(y))
    assert_axis(plot, "bottom", "Gate voltage", "V")
    assert_axis(plot, "left", "Signal - mean(Signal)", "nA")
    set_controls(plot, ("subtractMeanCheck",), False)
    assert_display(plot.line, x, y)
    assert_axis(plot, "left", "Signal", "nA")


@pytest.mark.parametrize("control", ["derivativeCheck", "fftCheck", "phasemapCheck"])
def test_primary_trace_on_secondary_axes_and_merged_bottom_metadata(merged_plots, control):
    window, (host, source, _other), x, y = merged_plots
    operation_option(source, "dy/dx").input.setChecked(True)
    assert apply_operations(source)[1:] == ([True], [])
    _key, line = merge(window, host, source, y_axis="Left")
    style = host._trace_styles[host.label]
    style.x_axis, style.y_axis = "Top", "Right"
    set_controls(host, (control,))
    host._apply_trace_style(host.label, host.line)
    assert_display(host.line, *expected_mapping(x, y, (control,)))
    assert_display(line, *expected_mapping(x, np.gradient(y, x), (control,)))
    if control == "phasemapCheck":
        assert_axis(host, "top", "Signal", "nA")
        assert_axis(host, "bottom", "d(Signal)/d(Gate voltage)", "nA/V")
    else:
        for side in ("top", "bottom"):
            assert_axis(host, side, "Frequency of Gate voltage" if control == "fftCheck"
                        else "Gate voltage", "1/V" if control == "fftCheck" else "V")
    assert_axis(host, "right", "FFT magnitude of Signal" if control == "fftCheck"
                else "d(Signal)/d(Gate voltage)", "nA" if control == "fftCheck" else "nA/V")
    assert_axis(host, "left", "FFT magnitude of d(Signal)/d(Gate voltage)" if control == "fftCheck"
                else "d(d(Signal)/d(Gate voltage))/d(Gate voltage)",
                "nA/V" if control == "fftCheck" else "(nA/V)/V")
    set_controls(host, (control,), False)
    assert_axis(host, "right", "Signal", "nA")
    assert_axis(host, "left", "d(Signal)/d(Gate voltage)", "nA/V")
    for side in ("top", "bottom"):
        assert_axis(host, side, "Gate voltage", "V")
