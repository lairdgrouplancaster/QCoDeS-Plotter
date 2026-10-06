"""Exact mean residuals at subnormal and mixed-record floating limits."""
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.tools.plot_tools import subtract_mean
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _errors_fixture,
)
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _errors_fixture
TINY = np.nextafter(0., 1.)


def exact_residuals(values):
    samples = [Fraction(v.item() if isinstance(v, np.generic) else v) for v in values]
    mean = sum(samples, Fraction()) / len(samples)
    result = []
    for v in samples:
        try:
            result.append(float(v - mean))
        except OverflowError:
            result.append(np.inf if v > mean else -np.inf)
    return np.array(result)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.arange(4), "y": np.array([TINY, TINY, 2*TINY, 2*TINY])},
], indirect=True)
def test_subnormal_native_mean_uses_exact_residuals(fft_plots, no_callback_errors):
    _window, (plot, _other), x, y = fft_plots
    click_control(plot, "subtractMeanCheck")
    for refresh in (False, True):
        if refresh:
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        np.testing.assert_array_equal(plot.line.getData()[1], exact_residuals(y))
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], y)
    click_control(plot, "subtractMeanCheck")
    np.testing.assert_array_equal(plot.line.getData()[1], y)


@pytest.mark.parametrize("axis", ["x", "y"])
def test_subnormal_operation_retains_missing_cells(axis):
    row = np.array([TINY, TINY, np.nan, 2*TINY, 2*TINY])
    grid = row[None, :] if axis == "x" else row[:, None]
    original = grid.copy()
    result = subtract_mean(axis, {"z": grid})["z"].ravel()
    np.testing.assert_array_equal(result[~np.isnan(row)], exact_residuals(row[~np.isnan(row)]))
    assert np.isnan(result[2])
    np.testing.assert_array_equal(grid, original)


def test_subnormal_exact_centering_is_cancellable():
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 3
    values = np.array([[TINY, TINY, 2*TINY, 2*TINY]])
    with pytest.raises(InterruptedError):
        subtract_mean("x", {"z": values}, cancelled_callback=cancel)


def test_real_mixed_record_mean_maps_overflow_through_qt_control(tmp_path, monkeypatch, no_callback_errors):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "mixed-mean.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    exp = load_or_create_experiment("mixed centering", "test")
    m = Measurement(exp=exp)
    m.register_custom_parameter("x", paramtype="array")
    m.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    maximum = np.finfo(float).max
    with m.run(write_in_background=False) as saver:
        saver.add_result(("x", np.array([0])), ("signal", np.array([1], dtype=np.int64)))
        saver.add_result(("x", np.arange(1, 4)), ("signal", np.array([-maximum, -maximum, maximum])))
    guid = saver.dataset.guid
    saver.dataset.conn.close()
    exp.conn.close()
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
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        plot.line.setDynamicRangeLimit(None)
        raw_x, raw_y = plot.line.getOriginalDataset()
        assert raw_y.dtype == object
        expected = exact_residuals(raw_y)
        click_control(plot, "subtractMeanCheck")
        for refresh in (False, True):
            if refresh:
                plot.refreshWindow(force=True)
                wait_for(lambda: not plot.worker.running)
                plot.monitor.stop()
            np.testing.assert_array_equal(plot.line.getData()[1], expected)
            np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw_y)
        # The heatmap operation uses the same exact object residual semantics.
        result = subtract_mean("x", {"z": raw_y[None, :]})["z"].ravel()
        np.testing.assert_array_equal(result, expected)
        click_control(plot, "subtractMeanCheck")
        np.testing.assert_array_equal(plot.line.getData()[1], raw_y.astype(float))
    finally:
        close_main_window(window)
        assert database_state(path) == protected
