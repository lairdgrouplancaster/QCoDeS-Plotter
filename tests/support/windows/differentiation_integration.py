"""Shared fixtures and assertions for differentiation integration."""

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

from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'apply_operations',
    'exact_gradient',
    'integer_array_plot',
    'operation_option',
]


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
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=show)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        # Each Apply is observed independently. Disable the accepted interval,
        # since worker completion would otherwise rearm a merely stopped timer.
        plot.spinBox.setValue(0)
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


def operation_option(plot, name):
    return next(
        plot.oper_widget.list_options.item(index)
        for index in range(plot.oper_widget.list_options.count())
        if plot.oper_widget.list_options.item(index).label == name
    )


def apply_operations(plot):
    # This helper is also imported by other integration fixtures. Disable live
    # polling here so every caller observes only its explicit Apply submission.
    plot.spinBox.setValue(0)
    plot.monitor.stop()
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
            wait_for(lambda: bool(submitted))
        worker = plot.worker
        assert worker is not previous
        assert submitted == [worker]
        wait_for(lambda: bool(finished) and not worker.running)
    assert not plot.monitor.isActive()
    assert not [warning for warning in emitted if issubclass(warning.category, RuntimeWarning)]
    return worker, finished, errors
