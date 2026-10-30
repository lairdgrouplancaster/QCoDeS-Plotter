"""Rounded-equal intervals must not select an exactly uniform derivative."""

from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.tools.plot_tools import differentiate
from qplot.windows import MainWindow
from tests._window_lifecycle import close_main_window
from tests.test_derivative_numeric_ranges import _source_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def _fraction(value):
    if isinstance(value, (int, np.integer)):
        return Fraction(int(value))
    return Fraction(*value.as_integer_ratio())


def _middle_derivative(x, y):
    """Differentiate the exact Lagrange polynomial, independent of spacing."""
    x, y = [list(map(_fraction, values)) for values in (x, y)]
    derivative = Fraction()
    for index in range(3):
        left, right = [other for other in range(3) if other != index]
        derivative += y[index] * (2 * x[1] - x[left] - x[right]) / (
            (x[index] - x[left]) * (x[index] - x[right])
        )
    return float(derivative)


COORDINATES = [
    np.array([-1., 2.**-54, 1.]),
    np.array([-1., 2.**-54, 1.], dtype=np.float32),
    np.array([-2**62, 1, 2**62], dtype=np.int64),
    np.array([0, 2**63, 2**64 - 1], dtype=np.uint64),
    np.array([-1, 2.**-54, 1], dtype=object),
    np.array([np.int64(-2**62), np.float64(1), np.uint64(2**62)], dtype=object),
    np.array([-1, Decimal("1e-17"), 1], dtype=object),
    np.array([-1, Fraction(1, 10**17), 1], dtype=object),
]


def _operate(x, y, surface):
    data = {"x": x, "y": y, "z": None}
    if surface != "line":
        grid = np.tile(y, (2, 1))
        data = ({"x": x, "y": np.arange(2), "z": grid} if surface == "x"
                else {"x": np.arange(2), "y": x, "z": grid.T})
    result = differentiate("x" if surface == "line" else surface, data)
    return (result["y"] if surface == "line" else result["z"][0]
            if surface == "x" else result["z"][:, 0])


@pytest.mark.parametrize("coordinates", COORDINATES)
@pytest.mark.parametrize("dtype", [np.float64, np.int64, object])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("surface", ["line", "x", "y"])
def test_rounded_uniform_intervals_retain_nonuniform_residual(
        coordinates, dtype, reverse, surface):
    x, y = coordinates.copy(), np.array([1, 0, 1], dtype=dtype)
    if reverse:
        x, y = x[::-1], y[::-1]
    expected = _middle_derivative(x, y)
    assert expected != 0
    actual = _operate(x, y, surface)
    assert actual[1] == expected
    np.testing.assert_array_equal(x, coordinates[::-1] if reverse else coordinates)
    np.testing.assert_array_equal(y, np.array([1, 0, 1], dtype=dtype))


@pytest.mark.parametrize("coordinates", COORDINATES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("surface", ["line", "x", "y"])
def test_rounded_uniform_intervals_do_not_bridge_missing_centre(coordinates, reverse, surface):
    x, y = coordinates.copy(), np.array([0., np.nan, 2.])
    if reverse:
        x, y = x[::-1], y[::-1]
    assert np.all(np.isnan(_operate(x, y, surface)))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("surface", ["line", "x", "y"])
@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.int64, np.uint64, object])
def test_exact_uniform_intervals_still_ignore_missing_centre(reverse, surface, dtype):
    x, y = np.array([0, 1, 2], dtype=dtype), np.array([0., np.nan, 2.])
    if reverse:
        x, y = x[::-1], y[::-1]
    np.testing.assert_array_equal(_operate(x, y, surface), [np.nan, 1., np.nan])


def test_randomized_rounded_uniform_intervals_match_exact_polynomial():
    rng = np.random.default_rng(472026)
    for _ in range(256):
        scale = np.ldexp(1., int(rng.integers(-400, 401)))
        middle = np.ldexp(scale, -int(rng.integers(54, 64)))
        if rng.integers(2):
            middle = -middle
        x = np.array([-scale, middle, scale])
        amplitude = np.ldexp(1., int(rng.integers(-400, 401)))
        y = np.array([amplitude, 0., amplitude])
        if rng.integers(2):
            x, y = x[::-1], y[::-1]
        assert _operate(x, y, "line")[1] == _middle_derivative(x, y)


@pytest.mark.parametrize("cancel_at", [4, 6])
def test_uniform_interval_validation_remains_cancellable(cancel_at):
    x = np.arange(150_001, dtype=float)
    y = np.sin(x)
    original_x, original_y = x.copy(), y.copy()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= cancel_at

    with pytest.raises(InterruptedError, match="cancelled"):
        differentiate("x", {"x": x, "y": y, "z": None}, cancelled_callback=cancelled)
    np.testing.assert_array_equal(x, original_x)
    np.testing.assert_array_equal(y, original_y)


@pytest.mark.parametrize("missing_index", [65535, 65536, 65537])
def test_rounded_uniform_validation_checks_chunk_boundaries(missing_index):
    x = np.arange(131_075, dtype=float) - missing_index
    y = np.sin(x)
    x[missing_index] = 2.**-54
    y[missing_index] = np.nan
    assert np.all(np.diff(x) == 1.)
    actual = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    assert np.isnan(actual[missing_index])


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_uniform_coordinates_cross_float_certification_boundary(reverse):
    values = np.arange(131_075, dtype=np.int64) + (2**53 - 100_000)
    x = values.astype(object)
    # Every other integral coordinate is even and exactly representable on
    # both sides of 2**53. Odd coordinates remain exact NumPy integers.
    x[::2] = values[::2].astype(float)
    x[1::2] = list(values[1::2])
    y = np.arange(len(x), dtype=float)
    y[100_001] = np.nan
    if reverse:
        x, y = x[::-1], y[::-1]
    actual = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    centre = len(x) - 1 - 100_001 if reverse else 100_001
    assert actual[centre] == 1.


@pytest.mark.parametrize("missing_index", [65535, 65536, 65537])
@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.longdouble])
def test_mixed_binary_coordinates_keep_exact_nonintegral_intervals(missing_index, dtype):
    x = (np.arange(131_075, dtype=float) - missing_index) / 8
    x = x.astype(object)
    x[::2] = [dtype(value) for value in x[::2]]
    x[missing_index] = dtype(2.**-58)
    y = np.ones(len(x))
    y[missing_index] = np.nan
    actual = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    assert np.isnan(actual[missing_index])


@pytest.mark.parametrize("surface", ["line", "x", "y"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("extended", [False, True])
def test_exact_uniform_object_intervals_may_exceed_binary64(surface, reverse, extended):
    if extended and np.finfo(np.longdouble).maxexp <= np.finfo(float).maxexp:
        pytest.skip("Platform has no extended floating exponent range")
    scalar = np.longdouble if extended else Decimal
    x = np.array([scalar("-1e400"), 0, scalar("1e400")], dtype=object)
    if reverse:
        x = x[::-1]
    with np.errstate(over="ignore", invalid="ignore"):
        np.testing.assert_array_equal(_operate(x, x.copy(), surface), np.ones(3))


@contextmanager
def _missing_heatmap(tmp_path, monkeypatch, *, uniform, reverse):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "rounded-uniform.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Rounded intervals", "synthetic")
    x = np.array([-1., 0. if uniform else 2.**-54, 1.])
    grid = np.array([[0., np.nan, 2.], [1., 0., 1.]])
    measurement = Measurement(exp=experiment)
    for name in ("x", "slow"):
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("slow", "x"))
    try:
        with measurement.run(write_in_background=False) as saver:
            for row, values in enumerate(grid):
                saver.add_result(("x", x[::-1] if reverse else x),
                                 ("slow", np.full(3, row, dtype=float)),
                                 ("signal", values[::-1] if reverse else values))
            guid = saver.dataset.guid
    finally:
        experiment.conn.close()
    protected = _source_state(path)
    errors = []
    window = MainWindow()
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda: len(window.windows) == 1)
        plot = window.windows[-1]
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        np.testing.assert_array_equal(plot.axis_data["x"], x)
        np.testing.assert_array_equal(plot.dataGrid, grid)
        assert errors == []
        yield plot, x, grid, errors
    finally:
        close_main_window(window)
        assert _source_state(path) == protected


@pytest.mark.parametrize("uniform", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_missing_heatmap_operations_and_csv(tmp_path, monkeypatch, uniform, reverse):
    with _missing_heatmap(tmp_path, monkeypatch, uniform=uniform, reverse=reverse) as (
            plot, x, grid, errors):
        option = operation_option(plot, "dz/dx")
        option.input.setChecked(True)
        _, finished, raised = apply_operations(plot)
        assert finished == [True] and raised == errors == []
        expected = np.array([[np.nan, 1. if uniform else np.nan, np.nan],
                             [-1., _middle_derivative(x, grid[1]), 1.]])
        np.testing.assert_array_equal(plot.dataGrid, expected)
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "derivative.csv", ",")
        np.testing.assert_array_equal([float(row[-1]) for row in rows[1:]], expected.ravel())
        option.input.setChecked(False)
        _, finished, raised = apply_operations(plot)
        assert finished == [True] and raised == errors == []
        np.testing.assert_array_equal(plot.dataGrid, grid)
