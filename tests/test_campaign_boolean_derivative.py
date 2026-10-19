"""Mixed Boolean NPY records remain numeric in operation derivatives."""

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
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@contextmanager
def _mixed_boolean_plot(tmp_path, monkeypatch, *, shaped, surface):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "mixed-boolean.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Mixed Boolean records", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    if surface != "line":
        measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("x",) if surface == "line" else ("slow", "x"),
    )
    if shaped:
        measurement.set_shapes({"signal": (6,) if surface == "line" else (2, 6)})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for slow in range(1 if surface == "line" else 2):
                for record_index, dtype in enumerate((bool, np.int64, float)):
                    values = np.arange(2 * record_index, 2 * record_index + 2)
                    record = {"x": values.astype(float), "signal": values.astype(dtype)}
                    if surface != "line":
                        record["slow"] = np.full(2, slow, dtype=float)
                    # The public DataSet API stores Boolean NPY arrays, while
                    # DataSaver.add_result restricts the accepted array dtypes.
                    saver.dataset.add_results([record])
            guid = saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
    window = MainWindow()
    errors = []
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
        wait_for(lambda: not plot.worker.running)
        plot.monitor.stop()
        assert errors == []
        yield plot, errors
    finally:
        close_main_window(window)
        assert database_state(path) == protected


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("surface", ["line", "x", "y"])
def test_mixed_boolean_records_differentiate_through_actual_controls(
        tmp_path, monkeypatch, shaped, surface):
    with _mixed_boolean_plot(tmp_path, monkeypatch, shaped=shaped, surface=surface) as (plot, errors):
        original = (plot.line.getOriginalDataset()[1] if surface == "line" else plot.dataGrid).copy()
        assert original.dtype.kind == "O"
        expected_raw = np.arange(6.) if surface == "line" else np.tile(np.arange(6.), (2, 1))
        np.testing.assert_array_equal(original, expected_raw)
        if surface == "line":
            np.testing.assert_array_equal(plot.line.getData()[1], expected_raw)
        option = operation_option(plot, "dy/dx" if surface == "line" else f"dz/d{surface}")
        option.input.setChecked(True)
        _worker, finished, raised = apply_operations(plot)
        assert finished == [True], raised
        assert raised == errors == []
        expected = np.zeros_like(expected_raw) if surface == "y" else np.ones_like(expected_raw)
        observed = plot.line.getOriginalDataset()[1] if surface == "line" else plot.dataGrid
        np.testing.assert_array_equal(observed, expected)
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "derivative.csv", ",")
        assert [float(row[-1]) for row in rows[1:]] == expected.ravel().tolist()
        option.input.setChecked(False)
        _worker, finished, raised = apply_operations(plot)
        assert finished == [True] and raised == errors == []
        restored = plot.line.getOriginalDataset()[1] if surface == "line" else plot.dataGrid
        np.testing.assert_array_equal(restored, original)


def _fraction(value):
    if isinstance(value, (bool, np.bool_, int, np.integer)):
        return Fraction(int(value))
    if isinstance(value, (float, np.floating)):
        return Fraction(*value.as_integer_ratio())
    return Fraction(value)


def _oracle(x, y):
    coordinates, samples = [list(map(_fraction, values)) for values in (x, y)]
    steps = [right - left for left, right in zip(coordinates[:-1], coordinates[1:], strict=True)]
    slopes = [(right - left) / step
              for left, right, step in zip(samples[:-1], samples[1:], steps, strict=True)]
    return np.array([float(slopes[0]), *[
        float((after * left + before * right) / (before + after))
        for before, after, left, right in zip(steps[:-1], steps[1:], slopes[:-1], slopes[1:], strict=True)
    ], float(slopes[-1])])


@pytest.mark.parametrize("boolean", [bool, np.bool_])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("case", [
    "signal", "coordinates", "extended_coordinates", "decimal", "extended", "missing",
])
def test_boolean_object_derivatives_keep_exact_other_samples(boolean, reverse, case):
    x = np.arange(6.)
    y = np.array([boolean(False), boolean(True), 2**60, 2**60 + 1, 0., 1.], dtype=object)
    if case == "coordinates":
        x = y = np.array([boolean(False), boolean(True), 2., 3., 2**60, 2**60 + 1], dtype=object)
    elif case == "extended_coordinates":
        last = np.longdouble(4)
        x = y = np.array([boolean(False), boolean(True), 2, 3., last,
                          np.nextafter(last, np.longdouble("inf"))], dtype=object)
    elif case == "decimal":
        y[-2:] = [Decimal("0.1"), Decimal("0.2")]
    elif case == "extended":
        y[-2:] = [np.longdouble(1), np.longdouble(1) + np.finfo(np.longdouble).eps]
    if reverse:
        x, y = x[::-1], y[::-1]
    expected = _oracle(x, y)
    if case == "missing":
        y[2] = np.nan
        expected[[1, 3]] = np.nan
    original_x, original_y = x.copy(), y.copy()
    observed = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    np.testing.assert_array_equal(observed, expected)
    np.testing.assert_array_equal(x, original_x)
    # NumPy's object-array comparison does not consider NaNs equal.
    for now, original in zip(y, original_y, strict=True):
        assert now is original or now == original or (np.isnan(float(now)) and np.isnan(float(original)))


@pytest.mark.parametrize("dtype", [bool, object])
def test_pure_boolean_derivatives_keep_direction(dtype):
    x = np.array([0., 1., 2.])
    y = np.array([True, False, True], dtype=dtype)
    np.testing.assert_array_equal(differentiate("x", {"x": x, "y": y, "z": None})["y"], [-1., 0., 1.])
    x = np.array([True, False], dtype=dtype)
    y = np.array([2., 0.])
    np.testing.assert_array_equal(differentiate("x", {"x": x, "y": y, "z": None})["y"], [2., 2.])


def test_boolean_object_classification_still_rejects_nonnumeric_cells():
    with pytest.raises((TypeError, ValueError)):
        differentiate("x", {"x": np.arange(3.),
                             "y": np.array([False, "invalid", 2.], dtype=object), "z": None})


@pytest.mark.parametrize("cancel_at", [2, 8, 18])
def test_boolean_exact_derivatives_remain_cancellable(cancel_at):
    x = np.arange(8192.)
    y = x.astype(object)
    y[0] = False
    original = y.copy()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= cancel_at

    with pytest.raises(InterruptedError, match="cancelled"):
        differentiate("x", {"x": x, "y": y, "z": None}, cancelled)
    assert calls == cancel_at
    np.testing.assert_array_equal(y, original)
