"""Publish merged samples and axis metadata through real Qt/QCoDeS windows."""

import threading

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.tools.operation_registry import OperationCall, OperationExecutionError
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@pytest.fixture
def merged_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "merged.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("merged metadata", sample_name="test")
    gate = ManualParameter("gate", label="Gate voltage", unit="V")
    signal = ManualParameter("signal", label="Signal", unit="nA")
    coordinates = np.asarray(getattr(request, "param", [0, 1, 2, 3]), dtype=float)
    values = coordinates ** 2
    guids = []
    for index in range(3):
        measurement = Measurement(exp=experiment, name=f"line {index}")
        measurement.register_parameter(gate)
        measurement.register_parameter(signal, setpoints=(gate,))
        with measurement.run(write_in_background=False) as datasaver:
            for coordinate, value in zip(coordinates, values, strict=True):
                datasaver.add_result((gate, coordinate), (signal, value))
            dataset = datasaver.dataset
            guids.append(dataset.guid)
    # Measurement datasets share the experiment's writer connection.
    experiment.conn.close()
    protected = {
        suffix: (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
        for suffix in ("", "-wal", "-journal")
        for path in [database_path.with_name(database_path.name + suffix)]
    }
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        plots = []
        for guid in guids:
            window.openPlot(guid=guid, show=True)
            plot = window.windows[-1]
            wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
            plot.monitor.stop()
            monkeypatch.setattr(plot, "show_error", lambda *_args: None)
            assert not plot.ds.running
            plots.append(plot)
        yield window, plots, coordinates, values
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


def merge(window, host, source, *, x_axis="Bottom", y_axis="Right"):
    assert window.add_trace_to_plot(
        host, source._dataset_key, source.param.name, param=source.param,
    )
    key = host._window_trace_key(source)
    line = host.lines[key]
    style = host._trace_styles[key]
    style.x_axis, style.y_axis = x_axis, y_axis
    host._apply_trace_style(key, line)
    return key, line


def assert_axis(plot, side, label, unit):
    axis = plot.plot.getAxis(side)
    assert (axis.labelText, axis.labelUnits) == (label, unit)


def assert_samples(line, x, y):
    actual_x, actual_y = line.getOriginalDataset()
    np.testing.assert_allclose(actual_x, x)
    np.testing.assert_allclose(actual_y, y)


@pytest.mark.parametrize("swapped,x_axis,y_axis", [
    (False, "Bottom", "Right"),
    (False, "Top", "Right"),
    (True, "Top", "Right"),
])
def test_completed_merged_operations_publish_samples_and_metadata(
    merged_plots, swapped, x_axis, y_axis,
):
    window, (host, source, _other), x, y = merged_plots
    if swapped:
        assert host.set_plot_axes_swapped(True)
        wait_for(lambda: not host.worker.running)
        host.monitor.stop()
    key, line = merge(window, host, source, x_axis=x_axis, y_axis=y_axis)
    derivative = operation_option(source, "dy/dx")
    host_worker = host.worker

    for enabled in (True, False, True, False):
        derivative.input.setChecked(enabled)
        # Toggling controls alone must retain the displayed metadata.
        previous_label = "Signal" if enabled else "d(Signal)/d(Gate voltage)"
        previous_unit = "nA" if enabled else "nA/V"
        assert_axis(host, "top" if swapped else "right", previous_label, previous_unit)
        _worker, finished, errors = apply_operations(source)
        assert finished == [True]
        assert errors == []
        displayed_y = np.gradient(y, x) if enabled else y
        label = "d(Signal)/d(Gate voltage)" if enabled else "Signal"
        unit = "nA/V" if enabled else "nA"
        assert_samples(source.line, x, displayed_y)
        assert_axis(source, "left", label, unit)
        assert_samples(line, displayed_y if swapped else x, x if swapped else displayed_y)
        assert_axis(host, "top" if swapped else "right", label, unit)
        assert_axis(host, "right" if swapped else "bottom", "Gate voltage", "V")
        if not swapped and x_axis == "Top":
            assert_axis(host, "top", "Gate voltage", "V")
        assert host.worker is host_worker  # completed target needs no reload
        assert host._trace_styles[key].x_axis == x_axis
        assert host._trace_styles[key].y_axis == y_axis


def test_shared_axis_keeps_first_trace_metadata_and_uses_current_assignment(merged_plots):
    window, (host, source, other), x, y = merged_plots
    source_key, source_line = merge(window, host, source)
    other_key, other_line = merge(window, host, other)
    operation_option(other, "dy/dx").input.setChecked(True)
    assert apply_operations(other)[1:] == ([True], [])
    assert_samples(other_line, x, np.gradient(y, x))
    assert_axis(host, "right", "Signal", "nA")

    # Move the first trace after merging: publication follows its current side.
    host._set_trace_y_axis(source_key, "left")
    assert_axis(host, "right", "d(Signal)/d(Gate voltage)", "nA/V")
    operation_option(source, "dy/dx").input.setChecked(True)
    assert apply_operations(source)[1:] == ([True], [])
    assert_samples(source_line, x, np.gradient(y, x))
    assert_axis(host, "left", "Signal", "nA")  # main trace still supplies label
    host._set_trace_y_axis(source_key, "right")
    assert_axis(host, "right", "d(Signal)/d(Gate voltage)", "nA/V")
    operation_option(other, "dy/dx").input.setChecked(False)
    assert apply_operations(other)[1:] == ([True], [])
    assert_samples(other_line, x, y)
    assert_axis(host, "right", "d(Signal)/d(Gate voltage)", "nA/V")
    host.remove_line(source.label, trace_key=source_key)
    assert_axis(host, "right", "Signal", "nA")
    assert other_key in host.lines


@pytest.mark.parametrize("merged_plots", [[0, 1, 2, 1, 0]], indirect=True)
def test_failed_derivative_keeps_merged_samples_and_labels(merged_plots):
    window, (host, source, _other), x, y = merged_plots
    _key, line = merge(window, host, source, x_axis="Top")
    for _ in range(2):
        operation_option(source, "dy/dx").input.setChecked(True)
        _worker, finished, errors = apply_operations(source)
        assert finished == [False]
        assert len(errors) == 1 and isinstance(errors[0], OperationExecutionError)
        host.refresh_secondary_lines()
        assert_samples(source.line, x, y)
        assert_samples(line, x, y)
        assert_axis(source, "left", "Signal", "nA")
        assert_axis(host, "right", "Signal", "nA")
        assert_axis(host, "top", "Gate voltage", "V")
    operation_option(source, "dy/dx").input.setChecked(False)
    assert apply_operations(source)[1:] == ([True], [])
    assert_samples(line, x, y)
    assert_axis(host, "right", "Signal", "nA")


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "superseded"])
def test_unpublished_operations_retain_last_merged_publication(
    merged_plots, monkeypatch, outcome,
):
    window, (host, source, _other), x, y = merged_plots
    _key, line = merge(window, host, source)
    derivative = operation_option(source, "dy/dx")
    derivative.input.setChecked(True)
    assert apply_operations(source)[1:] == ([True], [])
    expected = np.gradient(y, x)
    entered, release = threading.Event(), threading.Event()
    finished, errors = [], []

    def delay_or_fail(data):
        entered.set()
        if not release.wait(10):
            raise AssertionError("test did not release operation")
        if outcome == "failed":
            raise ValueError("operation rejected")
        return data

    get_data = source.oper_widget.get_data
    monkeypatch.setattr(
        source.oper_widget, "get_data",
        lambda: [*get_data(), OperationCall("Delayed operation", delay_or_fail)],
    )
    derivative.input.setChecked(False)
    try:
        source.oper_widget.apply_but.click()
        pending = source.worker
        pending.emitter.finished.connect(finished.append)
        pending.emitter.errorOccurred.connect(errors.append)
        wait_for(entered.is_set)
        host.refresh_secondary_lines()
        assert_samples(line, x, expected)
        assert_axis(host, "right", "d(Signal)/d(Gate voltage)", "nA/V")
        if outcome == "cancelled":
            pending.cancel()
        elif outcome == "superseded":
            monkeypatch.setattr(source.oper_widget, "get_data", get_data)
            # Submit a replacement before the older operation finishes.
            assert source.load_data()
            wait_for(lambda: not source.worker.running)
            expected = y
            assert_samples(line, x, expected)
            assert_axis(host, "right", "Signal", "nA")
        release.set()
        wait_for(lambda: bool(finished) and not pending.running)
        source.monitor.stop()
        assert finished == [outcome == "superseded"]
        assert len(errors) == (1 if outcome == "failed" else 0)
        host.refresh_secondary_lines()
        assert_samples(source.line, x, expected)
        assert_samples(line, x, expected)
        label = "Signal" if outcome == "superseded" else "d(Signal)/d(Gate voltage)"
        unit = "nA" if outcome == "superseded" else "nA/V"
        assert_axis(source, "left", label, unit)
        assert_axis(host, "right", label, unit)
    finally:
        release.set()


@pytest.mark.parametrize("close_target", [False, True], ids=["remove-trace", "close-target"])
def test_merged_source_connections_are_released(merged_plots, close_target):
    window, (host, source, _other), x, y = merged_plots
    key, line = merge(window, host, source)
    assert source._merged_trace_users == 1
    if close_target:
        host.close()
    else:
        host.remove_line(source.label, trace_key=key)
    line.disconnect_source_updates()  # cleanup remains idempotent
    assert source._merged_trace_users == 0
    operation_option(source, "dy/dx").input.setChecked(True)
    assert apply_operations(source)[1:] == ([True], [])
    assert_samples(source.line, x, np.gradient(y, x))
    assert_axis(source, "left", "d(Signal)/d(Gate voltage)", "nA/V")
    assert_samples(line, x, y)  # removed trace no longer receives signals
    assert line._source_update_signal is None
    assert line._source_compatibility_signal is None
    if not close_target:
        assert_axis(host, "right", "", "")


def test_closed_source_retains_publication_connection_until_trace_removal(merged_plots):
    window, (host, source, _other), x, y = merged_plots
    key, line = merge(window, host, source)
    source.close()
    assert source._closed and source._merged_trace_users == 1
    operation_option(source, "dy/dx").input.setChecked(True)
    assert apply_operations(source)[1:] == ([True], [])
    assert_samples(line, x, np.gradient(y, x))
    assert_axis(host, "right", "d(Signal)/d(Gate voltage)", "nA/V")
    host.remove_line(source.label, trace_key=key)
    assert source._merged_trace_users == 0
    assert line._source_update_signal is None
    assert_axis(host, "right", "", "")
