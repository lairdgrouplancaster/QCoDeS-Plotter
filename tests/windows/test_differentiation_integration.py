"""Differentiate real QCoDeS sweeps through qPlot's registered operations."""

import warnings
from fractions import Fraction
from unittest.mock import patch

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
from qplot.tools.plot_tools import differentiate
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def exact_gradient(coordinates, values):
    """Evaluate the first-order edges and quadratic interior in rationals."""
    def rational(value):
        return Fraction(int(value)) if isinstance(value, (int, np.integer)) else Fraction.from_float(float(value))

    x = [rational(value) for value in coordinates]
    y = [rational(value) for value in values]
    slopes = []
    for index in range(len(x)):
        if index == 0:
            slope = (y[1] - y[0]) / (x[1] - x[0])
        elif index == len(x) - 1:
            slope = (y[-1] - y[-2]) / (x[-1] - x[-2])
        else:
            before, after = x[index] - x[index - 1], x[index + 1] - x[index]
            slope = (
                -Fraction(after, before * (before + after)) * y[index - 1]
                + Fraction(after - before, before * after) * y[index]
                + Fraction(before, after * (before + after)) * y[index + 1]
            )
        slopes.append(float(slope))
    return np.asarray(slopes)


@pytest.fixture
def integer_array_plot(tmp_path, monkeypatch, request):
    coordinates, values = request.param[:2]
    show = request.param[2] if len(request.param) == 3 else False
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "integer-differentiation.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("integer differentiation", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    with measurement.run(write_in_background=False) as saver:
        saver.add_result(("x", coordinates), ("signal", values))
        guid = saver.dataset.guid
    stored = saver.dataset.get_parameter_data("signal")["signal"]
    for name, original in (("x", coordinates), ("signal", values)):
        assert stored[name].dtype == original.dtype
        np.testing.assert_array_equal(stored[name].ravel(), original)
    saver.dataset.conn.close()
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
        window.openPlot(guid=guid, show=show)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        plot.line.setDynamicRangeLimit(None)
        monkeypatch.setattr(plot, "show_error", lambda *_args: None)
        for name, original in (("x", coordinates), ("y", values)):
            np.testing.assert_array_equal(plot.axis_data[name], original)
        yield plot
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


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
    finished, errors = [], []
    submitted = []
    start_worker = plot.threadPool.start

    def observe_and_start(worker):
        # A short sweep can finish before click() returns. Connect before
        # submission so the test observes both success and error signals.
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        submitted.append(worker)
        start_worker(worker)

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", RuntimeWarning)
        with patch.object(plot.threadPool, "start", side_effect=observe_and_start):
            plot.oper_widget.apply_but.click()
        worker = plot.worker
        assert worker is not previous
        assert submitted == [worker]
        wait_for(lambda: bool(finished) and not worker.running)
    plot.monitor.stop()
    assert not [warning for warning in emitted if issubclass(warning.category, RuntimeWarning)]
    return worker, finished, errors


@pytest.mark.parametrize("integer_array_plot", [
    (np.arange(4), np.array([2**63 + n for n in (3, 2, 1, 0)], dtype=np.uint64)),
    (np.array([2**63 + n for n in range(4)], dtype=np.uint64), np.array([0, 2, 4, 6])),
    (np.array([2**53 + n for n in (7, 4, 2, 0)]), np.array([14, 8, 4, 0])),
    (np.arange(4), np.array([2**53 + n for n in (3, 2, 1, 0)])),
    (np.arange(4), np.array([2**63 - 2, 2**63 - 1, 2**63, 2**63 + 1], dtype=np.uint64)),
    (np.array([2**63 + n for n in (0, 2, 5, 9)], dtype=np.uint64),
     np.array([2**63 + n for n in (0, 3, 7, 12)], dtype=np.uint64)),
    (np.array([0., .5, 2., 5.]),
     np.array([2**63 + n for n in (0, 2, 5, 9)], dtype=np.uint64)),
    (np.array([2**63 + n for n in (0, 1, 3, 6)], dtype=np.uint64),
     np.array([0., 1.5, 4., 9.])),
    (np.arange(4), np.array([-(2**63), -(2**63) + 1, 2**63 - 2, 2**63 - 1])),
    (np.array([-(2**63), -1, 2**63 - 2, 2**63 - 1]), np.array([0, 1, 2, 3])),
    (np.array([0, 2**63, 2**64 - 2, 2**64 - 1], dtype=np.uint64),
     np.array([0, 1, 2, 3])),
    (np.array([2**63, 2**63 + 1], dtype=np.uint64),
     np.array([2**63 + 3, 2**63 + 2], dtype=np.uint64)),
], indirect=True, ids=[
    "adjacent-unsigned-measurements", "adjacent-unsigned-coordinates",
    "descending-signed-coordinates", "adjacent-signed-measurements",
    "unsigned-signed-boundary", "nonuniform-unsigned", "float-coordinates-unsigned-measurement",
    "unsigned-coordinates-float-measurement",
    "signed-measurement-boundaries", "signed-coordinate-boundaries",
    "full-unsigned-coordinate-range", "two-adjacent-unsigned-samples",
])
def test_integer_arrays_through_operations_apply(integer_array_plot):
    plot = integer_array_plot
    original = {name: values.copy() for name, values in plot.axis_data.items()}
    source = tuple(values.copy() for values in plot.line.getOriginalDataset())
    cache = {
        name: values.copy()
        for name, values in cache_parameter_data(plot.ds.cache, "signal").items()
    }
    operation_option(plot, "dy/dx").input.setChecked(True)
    worker, finished, errors = apply_operations(plot)
    assert finished == [True]
    assert errors == []
    np.testing.assert_allclose(
        plot.axis_data["y"], exact_gradient(original["x"], original["y"]),
        rtol=1e-14, atol=0,
    )
    np.testing.assert_array_equal(plot.axis_data["x"], original["x"])
    np.testing.assert_array_equal(worker.axis_data["y"], plot.axis_data["y"])
    for name, expected in cache.items():
        np.testing.assert_array_equal(cache_parameter_data(plot.ds.cache, "signal")[name], expected)

    operation_option(plot, "dy/dx").input.setChecked(False)
    assert apply_operations(plot)[1:] == ([True], [])
    for name, expected in original.items():
        np.testing.assert_array_equal(plot.axis_data[name], expected)
    for actual, expected in zip(plot.line.getOriginalDataset(), source, strict=True):
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("axis", ["x", "y"])
def test_stored_integer_heatmap_through_operations_apply(tmp_path, monkeypatch, axis):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "integer-heatmap.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("integer heatmap", sample_name="test")
    x = np.array([0, 1, 3, 6], dtype=np.int64)
    y = np.array([0, 2, 5], dtype=np.int64)
    grid = y[:, None] ** 2 + x[None, :] ** 2
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter("fast", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "fast"),
    )
    measurement.set_shapes({"signal": grid.shape})
    with measurement.run(write_in_background=False) as saver:
        for slow, row in zip(y, grid, strict=True):
            saver.add_result(("slow", int(slow)), ("fast", x), ("signal", row))
        guid = saver.dataset.guid
    stored = saver.dataset.get_parameter_data("signal")["signal"]
    np.testing.assert_array_equal(stored["fast"], np.tile(x, (len(y), 1)))
    np.testing.assert_array_equal(stored["signal"], grid)
    saver.dataset.conn.close()
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
        wait_for(lambda: hasattr(plot, "dataGrid") and not plot.worker.running)
        plot.monitor.stop()
        original_grid = plot.dataGrid.copy()
        original_axes = {name: values.copy() for name, values in plot.axis_data.items()}
        np.testing.assert_array_equal(original_grid, grid)
        operation_option(plot, f"dz/d{axis}").input.setChecked(True)
        worker, finished, errors = apply_operations(plot)
        assert finished == [True]
        assert errors == []
        expected = np.stack([
            exact_gradient(original_axes[axis], line)
            for line in (original_grid if axis == "x" else original_grid.T)
        ])
        if axis == "y":
            expected = expected.T
        np.testing.assert_allclose(plot.dataGrid, expected, rtol=1e-14, atol=0)
        np.testing.assert_array_equal(worker.dataGrid, plot.dataGrid)
        for name, values in original_axes.items():
            np.testing.assert_array_equal(plot.axis_data[name], values)
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


@pytest.mark.parametrize("axis", ["x", "y"])
def test_large_integer_heatmap_stencil_preserves_source(axis):
    x = np.array([2**63 + n for n in (0, 1, 3, 6)], dtype=np.uint64)
    y = np.array([2**53 + n for n in (5, 2, 0)], dtype=np.int64)
    grid = np.array([
        [2**63 + 100 + 7 * row + 3 * column**2 for column in range(len(x))]
        for row in range(len(y))
    ], dtype=np.uint64)
    source = {"x": x.copy(), "y": y.copy(), "z": grid.copy()}
    result = differentiate(axis, source)["z"]
    expected = np.stack([
        exact_gradient(source[axis], line)
        for line in (grid if axis == "x" else grid.T)
    ])
    if axis == "y":
        expected = expected.T
    np.testing.assert_allclose(result, expected, rtol=1e-14, atol=0)
    for name, original in (("x", x), ("y", y), ("z", grid)):
        np.testing.assert_array_equal(source[name], original)


def test_mixed_signed_unsigned_integer_cells_keep_exact_gaps():
    coordinates = np.array([2**63 - 1, 2**63, 2**63 + 2, 2**63 + 5], dtype=object)
    values = np.array([2**63 + 7, 2**63 + 5, -(2**63) + 1, -(2**63) + 4], dtype=object)
    data = {"x": coordinates, "y": values, "z": None}
    result = differentiate("x", data)["y"]
    np.testing.assert_allclose(
        result, exact_gradient(coordinates, values), rtol=1e-14, atol=0,
    )
    np.testing.assert_array_equal(data["x"], coordinates)
    np.testing.assert_array_equal(data["y"], values)


def test_large_integer_repeat_is_rejected_without_changing_source():
    coordinates = np.array([2**63, 2**63 + 1, 2**63 + 1], dtype=np.uint64)
    values = np.array([2**63 + 3, 2**63 + 2, 2**63 + 1], dtype=np.uint64)
    with pytest.raises(ValueError, match="must not repeat"):
        differentiate("x", {"x": coordinates, "y": values, "z": None})
    np.testing.assert_array_equal(coordinates, [2**63, 2**63 + 1, 2**63 + 1])
    np.testing.assert_array_equal(values, [2**63 + 3, 2**63 + 2, 2**63 + 1])


def test_large_integer_differentiation_cancellation_keeps_source():
    coordinates = np.array([2**63 + n for n in range(4)], dtype=np.uint64)
    values = np.array([2**63 + n for n in (3, 2, 1, 0)], dtype=np.uint64)
    checks = iter((False, False, True))
    with pytest.raises(InterruptedError, match="cancelled"):
        differentiate(
            "x", {"x": coordinates, "y": values, "z": None},
            cancelled_callback=lambda: next(checks),
        )
    np.testing.assert_array_equal(coordinates, [2**63 + n for n in range(4)])
    np.testing.assert_array_equal(values, [2**63 + n for n in (3, 2, 1, 0)])


@pytest.mark.parametrize("sweep_plot", [
    ([0, 1, 2, 1, 0], [0, 3, 6, 3, 0]),
], indirect=True)
@pytest.mark.parametrize("reject_derivative", [False, True], ids=["success", "error"])
def test_apply_observes_worker_completion_before_button_returns(
    sweep_plot, monkeypatch, reject_derivative,
):
    plot = sweep_plot
    operation_option(plot, "dy/dx").input.setChecked(reject_derivative)
    start_worker = plot.threadPool.start
    completed_before_return = []

    def start_and_finish(worker):
        # Keep the real threaded worker and GUI callbacks, but force completion
        # while the Apply button's clicked handler is still on the stack.
        start_worker(worker)
        wait_for(lambda: not worker.running)
        completed_before_return.append(worker)

    monkeypatch.setattr(plot.threadPool, "start", start_and_finish)
    worker, finished, errors = apply_operations(plot)

    assert completed_before_return == [worker]
    assert finished == [not reject_derivative]
    if reject_derivative:
        assert len(errors) == 1
        assert isinstance(errors[0], OperationExecutionError)
    else:
        assert errors == []


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
