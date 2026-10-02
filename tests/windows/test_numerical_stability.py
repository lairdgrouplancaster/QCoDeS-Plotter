"""Independent real QCoDeS baseline for stable numerical operations."""

from fractions import Fraction

import numpy as np
import pyqtgraph as pg
import pytest
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.tools.plot_tools import _center_float_samples, differentiate, subtract_mean
from qplot.windows import main as main_window
from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_averaging import click_average
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for

no_callback_errors = _no_callback_errors_fixture


@pytest.fixture
def measured_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    x, y, count = request.param
    heatmap = y.ndim == 2
    path = tmp_path / "numerical.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("numerical audit", sample_name="test")
    guids = []
    try:
        for _ in range(count):
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter(
                "x", paramtype="array", label="Coordinate", unit="V"
            )
            if heatmap:
                measurement.register_custom_parameter("slow", paramtype="array")
                measurement.set_shapes({"signal": y.shape})
            measurement.register_custom_parameter(
                "signal",
                paramtype="array",
                setpoints=("slow", "x") if heatmap else ("x",),
                label="Signal",
                unit="A",
            )
            with measurement.run(write_in_background=False) as saver:
                if heatmap:
                    saver.add_result(
                        ("x", np.tile(x, (len(y), 1))),
                        ("slow", np.repeat(np.arange(len(y))[:, None], len(x), axis=1)),
                        ("signal", y),
                    )
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
            window.openPlot(guid=guid, show=True)
            host = window.windows[-1]
            wait_for(
                lambda host=host: hasattr(host, "axis_data") and not host.worker.running
            )
            host.monitor.stop()
            if heatmap:
                np.testing.assert_array_equal(host.dataGrid, y)
            else:
                host.line.setDynamicRangeLimit(None)
                np.testing.assert_array_equal(host.axis_data["y"], y)
            plots.append(host)
        yield window, plots, x, y
    finally:
        close_main_window(window)
        assert database_state(path) == protected


@pytest.mark.parametrize(
    "measured_plots",
    [
        (np.arange(3), np.full(3, 60000, dtype=np.float16), 3),
        (np.arange(3), np.full(3, 3e38, dtype=np.float32), 3),
    ],
    indirect=True,
)
def test_three_measured_curve_average(measured_plots, no_callback_errors):
    window, (host, *sources), x, y = measured_plots
    for source in sources:
        merge(window, host, source, x_axis="Bottom", y_axis="Left")
    click_average(host)
    count, average = next(iter(host.plot.avgCurves.values()))
    assert count == 3
    actual = average.getData()[1]
    expected = y.astype(np.float64)
    np.testing.assert_allclose(actual, expected, rtol=1e-12)
    for line in host.lines.values():
        original_x, original_y = line.getOriginalDataset()
        assert original_y.dtype == y.dtype
        np.testing.assert_array_equal(original_x, x)
        np.testing.assert_array_equal(original_y, y)


@pytest.mark.parametrize(
    "measured_plots", [(np.arange(4.0), 1e16 + np.arange(4.0) * 2, 1)], indirect=True
)
def test_float64_native_centering(measured_plots, no_callback_errors, tmp_path):
    _window, (host,), x, y = measured_plots
    original_csv = tmp_path / "original.csv"
    centered_csv = tmp_path / "centered.csv"
    CSVExporter(host.plot).export(str(original_csv))
    click_control(host, "subtractMeanCheck")
    samples = [Fraction(float(value)) for value in y]
    mean = sum(samples) / len(samples)
    expected = np.array([float(value - mean) for value in samples])
    actual = host.line.getData()[1]
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(host.line.getOriginalDataset()[1], y)
    CSVExporter(host.plot).export(str(centered_csv))
    assert centered_csv.read_bytes() == original_csv.read_bytes()


@pytest.mark.parametrize(
    "measured_plots",
    [(np.array([0.0, 1.0, 3.0]), np.array([1e16, 1e16 + 4, 1e16 + 12]), 1)],
    indirect=True,
)
def test_float64_offset_gradient(measured_plots, no_callback_errors):
    _window, (host,), x, y = measured_plots
    operation_option(host, "dy/dx").input.setChecked(True)
    assert apply_operations(host)[1:] == ([True], [])
    expected = np.full(3, 4.0)
    actual = host.line.getData()[1]
    np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize(
    "measured_plots",
    [
        (np.arange(4.0), 1e16 + 2 * np.arange(12.0).reshape(3, 4), 1),
    ],
    indirect=True,
)
@pytest.mark.parametrize(
    "axis,operation", [("x", "Subtract Row Mean"), ("y", "Subtract Column Mean")]
)
def test_float64_heatmap_mean_through_apply(
    measured_plots, no_callback_errors, axis, operation
):
    _window, (host,), _x, grid = measured_plots
    expected = np.empty(grid.shape)
    lines = grid if axis == "x" else grid.T
    output = expected if axis == "x" else expected.T
    for source, target in zip(lines, output, strict=True):
        exact = [Fraction(float(value)) for value in source]
        mean = sum(exact) / len(exact)
        target[:] = [float(value - mean) for value in exact]
    operation_option(host, operation).input.setChecked(True)
    assert apply_operations(host)[1:] == ([True], [])
    np.testing.assert_array_equal(host.dataGrid, expected)
    operation_option(host, operation).input.setChecked(False)
    assert apply_operations(host)[1:] == ([True], [])
    np.testing.assert_array_equal(host.dataGrid, grid)


@pytest.mark.parametrize(
    "values",
    [
        [1e308, 1e308, 1e308],
        [0.0, 1e308, 1e308],
        [1e308, -1e308, -1e308],
        [-1e308, 1e308, 1e308],
        [0.0] + [1e308, -1e308] * 8,
        [np.finfo(float).max] * 4,
    ],
)
def test_float_mean_bounded_fallback(values):
    values = np.asarray(values)
    exact = [Fraction(float(value)) for value in values]
    mean = sum(exact) / len(exact)
    expected = np.array([float(value - mean) for value in exact])
    original = values.copy()
    np.testing.assert_allclose(_center_float_samples(values), expected, rtol=2e-15)
    np.testing.assert_allclose(
        subtract_mean("x", {"z": values[None, :]})["z"][0], expected, rtol=2e-15
    )
    np.testing.assert_array_equal(values, original)


@pytest.mark.parametrize("ignore_nan", [False, True])
@pytest.mark.parametrize(
    "values",
    [
        [np.nan, 1e16, 1e16 + 2, 1e16 + 4],
        [np.inf, 1.0, np.nan],
        [-np.inf, 1.0, np.nan],
        [np.inf, -np.inf, 1.0],
        [np.nan, np.nan, np.nan],
    ],
)
def test_float_centering_nonfinite_semantics(values, ignore_nan):
    values = np.asarray(values)
    if not np.any(np.isinf(values)) and ignore_nan and np.any(np.isfinite(values)):
        finite = np.isfinite(values)
        exact = [Fraction(float(value)) for value in values[finite]]
        mean = sum(exact) / len(exact)
        expected = np.full(values.shape, np.nan)
        expected[finite] = [float(value - mean) for value in exact]
    else:
        with np.errstate(invalid="ignore"):
            expected = values - (np.nanmean(values) if ignore_nan else np.mean(values))
    np.testing.assert_array_equal(
        _center_float_samples(values, ignore_nan=ignore_nan), expected
    )


def test_float_mean_cancels_between_chunks():
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls == 2

    with pytest.raises(InterruptedError, match="cancelled"):
        subtract_mean("x", {"z": np.ones((65537, 2))}, cancelled_callback=cancelled)


def test_nonuniform_complex_gradient_retains_imaginary_samples():
    x = np.array([0, 1, 3])
    y = (4 + 3j) * x
    actual = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    np.testing.assert_allclose(actual, np.full(3, 4 + 3j))


@pytest.mark.parametrize(
    "values",
    [
        [np.finfo(float).max] * 4,
        [1e308, 1e308, -1e308],
        [np.inf, np.inf, 1.0],
    ],
)
def test_native_average_wide_and_nonfinite_cells(values, no_callback_errors):
    plot = NativePlotItem()
    widget = pg.PlotWidget(plotItem=plot)
    try:
        for value in values:
            plot.addItem(NativePlotDataItem(np.arange(2), np.full(2, value)))
        plot.ctrl.averageGroup.setChecked(True)
        _count, average = next(iter(plot.avgCurves.values()))
        if np.isfinite(values).all():
            expected = float(sum(Fraction(value) for value in values) / len(values))
        else:
            expected = np.mean(values)
        np.testing.assert_allclose(
            average.getData()[1], np.full(2, expected), rtol=2e-15
        )
    finally:
        widget.close()


def test_native_mean_at_float_limit(no_callback_errors):
    values = np.full(4, np.finfo(float).max)
    line = NativePlotDataItem(np.arange(4), values)
    line.setSubtractMeanMode(True)
    np.testing.assert_array_equal(line.getData()[1], np.zeros(4))
    np.testing.assert_array_equal(line.getOriginalDataset()[1], values)


def test_single_integer_average_retains_source_precision(no_callback_errors):
    values = np.array([2**63 + 1, 2**63 + 2], dtype=np.uint64)
    plot = NativePlotItem()
    widget = pg.PlotWidget(plotItem=plot)
    try:
        plot.addItem(NativePlotDataItem(np.arange(2), values))
        plot.ctrl.averageGroup.setChecked(True)
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 1
        assert average.getData()[1].dtype == values.dtype
        np.testing.assert_array_equal(average.getData()[1], values)
    finally:
        widget.close()
