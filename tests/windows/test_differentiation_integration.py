"""Differentiate real QCoDeS sweeps through qPlot's registered operations."""

import warnings

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling.qcodes_cache import cache_parameter_data
from qplot.tools.operation_registry import (
    OperationCall,
    OperationExecutionError,
    operation_specs_for,
)
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@pytest.fixture
def sweep_plot(tmp_path, monkeypatch, request):
    coordinates, values = request.param
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "sweep.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("differentiation", sample_name="test")
    x = ManualParameter("x", label="Position", unit="V")
    signal = ManualParameter("signal", label="Signal", unit="A")
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(x)
    measurement.register_parameter(signal, setpoints=(x,))
    with measurement.run(write_in_background=False) as datasaver:
        for coordinate, value in zip(coordinates, values, strict=True):
            datasaver.add_result((x, coordinate), (signal, value))
        dataset = datasaver.dataset
        guid = dataset.guid
    dataset.conn.close()
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
        window.openPlot(guid=guid, show=False)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        monkeypatch.setattr(plot, "show_error", lambda *_args: None)
        valid = ~np.isnan(values)
        np.testing.assert_array_equal(plot.axis_data["x"], np.asarray(coordinates)[valid])
        np.testing.assert_array_equal(plot.axis_data["y"], np.asarray(values)[valid])
        yield plot
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


def operation_option(plot, name):
    return next(
        plot.oper_widget.list_options.item(index)
        for index in range(plot.oper_widget.list_options.count())
        if plot.oper_widget.list_options.item(index).label == name
    )


def apply_operations(plot):
    previous = plot.worker
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", RuntimeWarning)
        plot.oper_widget.apply_but.click()
        worker = plot.worker
        assert worker is not previous
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        wait_for(lambda: bool(finished) and not worker.running)
    plot.monitor.stop()
    assert not [warning for warning in emitted if issubclass(warning.category, RuntimeWarning)]
    return worker, finished, errors


@pytest.mark.parametrize("sweep_plot", [
    ([0, 1, 2, 1, 0], [0, 3, 6, 3, 0]),
    ([0, 1, 3, 2, 4], [0, 3, 9, 7, 14]),
    ([3, 2, 1, 2, 3], [9, 6, 3, 7, 11]),
    ([0, 1, 1, 2], [0, 3, 4, 6]),
    ([0, 1, 2, 1, 3], [0, 3, 6, 4, 10]),
], indirect=True, ids=["reversal", "unequal-reversal", "descending-reversal", "repeat", "nonadjacent-repeat"])
@pytest.mark.parametrize("preceding_limit", [False, True], ids=["derivative-only", "atomic-pipeline"])
def test_invalid_derivative_retains_plot_and_recovers(sweep_plot, preceding_limit):
    plot = sweep_plot
    original = {axis: data.copy() for axis, data in plot.axis_data.items()}
    original_line = tuple(data.copy() for data in plot.line.getOriginalDataset())
    original_param = plot.display_param
    original_metadata = (original_param.label, original_param.unit)
    original_axis_label = plot.plot.getAxis("left").labelText
    cache = cache_parameter_data(plot.ds.cache, "signal")
    original_cache = {name: data.copy() for name, data in cache.items()}

    # A successful earlier operation must also be discarded if dy/dx fails.
    limit = operation_option(plot, "Limit Maximum")
    limit.input.setChecked(preceding_limit)
    limit.operation_row.input.setText("4")
    derivative = operation_option(plot, "dy/dx")
    derivative.input.setChecked(True)
    worker, finished, errors = apply_operations(plot)

    assert finished == [False]
    assert len(errors) == 1
    assert isinstance(errors[0], OperationExecutionError)
    assert 'Operation "dy/dx" failed' in str(errors[0])
    if np.any(np.diff(original["x"]) == 0):
        assert "must not repeat" in str(errors[0])
    else:
        assert "strictly increasing or decreasing" in str(errors[0])
        assert "reversing sweeps are not supported" in str(errors[0])
    assert (worker.display_param.label, worker.display_param.unit) == original_metadata
    assert plot.display_param is original_param
    assert (plot.display_param.label, plot.display_param.unit) == original_metadata
    assert plot.plot.getAxis("left").labelText == original_axis_label
    for axis in ("x", "y"):
        np.testing.assert_array_equal(worker.axis_data[axis], original[axis])
        np.testing.assert_array_equal(plot.axis_data[axis], original[axis])
    for actual, expected in zip(plot.line.getOriginalDataset(), original_line, strict=True):
        np.testing.assert_array_equal(actual, expected)
    for name, expected in original_cache.items():
        np.testing.assert_array_equal(cache_parameter_data(plot.ds.cache, "signal")[name], expected)

    derivative.input.setChecked(False)
    _worker, finished, errors = apply_operations(plot)
    assert finished == [True]
    assert errors == []
    np.testing.assert_array_equal(plot.axis_data["x"], original["x"])
    expected_y = np.minimum(original["y"], 4) if preceding_limit else original["y"]
    np.testing.assert_array_equal(plot.axis_data["y"], expected_y)
    assert (plot.display_param.label, plot.display_param.unit) == original_metadata

    limit.input.setChecked(False)
    _worker, finished, errors = apply_operations(plot)
    assert finished == [True]
    assert errors == []
    for axis in ("x", "y"):
        np.testing.assert_array_equal(plot.axis_data[axis], original[axis])


@pytest.mark.parametrize("sweep_plot", [
    ([0, 1, 2, 3], [0, 3, 6, 9]),
    ([3, 2, 1, 0], [9, 6, 3, 0]),
    ([0, 0.5, 2, 5], [0, 1.5, 6, 15]),
    ([5, 2, 0.5, 0], [15, 6, 1.5, 0]),
    ([1, 0], [3, 0]),
    ([0, 1, 2, 3, 4], [0, 3, float("nan"), 9, 12]),
], indirect=True, ids=["ascending", "descending", "nonuniform", "descending-nonuniform", "two-points", "missing-signal"])
def test_valid_derivative_preserves_order_and_updates_metadata(sweep_plot):
    plot = sweep_plot
    original_x = plot.axis_data["x"].copy()
    operation_option(plot, "dy/dx").input.setChecked(True)
    _worker, finished, errors = apply_operations(plot)
    assert finished == [True]
    assert errors == []
    np.testing.assert_array_equal(plot.axis_data["x"], original_x)
    np.testing.assert_allclose(plot.axis_data["y"], 3)
    assert (plot.display_param.label, plot.display_param.unit) == ("d(Signal)/d(Position)", "A/V")
    assert (plot.param.label, plot.param.unit) == ("Signal", "A")


@pytest.mark.parametrize("axis", ["x", "y"])
def test_registered_heatmap_derivative_preserves_missing_data(tmp_path, axis):
    coordinates = np.array([0.0, 0.5, 2.0, 3.0, 5.0])
    values = coordinates[:, None] + 3 * coordinates[None, :]
    values[2, 2] = np.nan
    records = [
        (slow, coordinates, row)
        for slow, row in zip(coordinates, values, strict=True)
    ]
    spec = next(spec for spec in operation_specs_for("plot2d") if spec.name == f"dz/d{axis}")
    operation = OperationCall(spec.name, spec.func, spec.derivative_axis, cooperative=True)
    with heatmap_dataset(tmp_path, records=records, planned_shape=(5, 5)) as dataset:
        worker = make_worker(dataset, operations=[operation])
        with warnings.catch_warnings(record=True) as emitted:
            warnings.simplefilter("always", RuntimeWarning)
            run_worker(worker)
        assert not [warning for warning in emitted if issubclass(warning.category, RuntimeWarning)]
        expected = np.gradient(values, coordinates, axis=1 if axis == "x" else 0)
        assert np.isnan(expected).any()
        assert np.isfinite(expected).any()
        np.testing.assert_allclose(worker.dataGrid, expected, equal_nan=True)
