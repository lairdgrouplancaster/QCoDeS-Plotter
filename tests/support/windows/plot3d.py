"""Shared fixtures and assertions for plot3d."""

import itertools

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows._plotWin import plotWidget
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    configure_temp_qplot,
)

__all__ = [
    'build_mixed_volume',
    'loaded',
    'main',
]


def build_volume(path, array=False, *, gate_points=None):
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("3D", "synthetic")
    measurement = Measurement(exp=experiment)
    for name in ("field", "bias", "gate"):
        measurement.register_custom_parameter(name, unit="V", paramtype="array" if array else "numeric")
    measurement.register_custom_parameter("signal", unit="A", setpoints=("field", "bias", "gate"),
                                          paramtype="array" if array else "numeric")
    try:
        with measurement.run(write_in_background=False) as saver:
            gates = [-2., 0., 5.] if gate_points is None else gate_points
            points = np.asarray(list(itertools.product([4., 1., 2.5], [0., 3.], gates)))
            signal = 100 * points[:, 0] + 10 * points[:, 1] + points[:, 2]**2
            if array:
                saver.add_result(*zip(("field", "bias", "gate", "signal"), (*points.T, signal), strict=True))
            else:
                for point, value in zip(points, signal, strict=True):
                    saver.add_result(*zip(("field", "bias", "gate", "signal"), (*point, value), strict=True))
        guid = saver.dataset.guid
        saver.dataset.conn.close()
        return guid
    finally:
        experiment.conn.close()


@pytest.fixture
def main(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(plotWidget, "show_error", lambda self, *args: errors.append(args))
    window = MainWindow()
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    window.startupDatabaseTimer.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    window.close_database(status=False)
    window.autoPlotBox.setChecked(False)
    yield window, errors
    close_main_window(window)


def loaded(plot):
    return hasattr(plot, "worker") and not plot.worker.running and plot.worker.result is not None


def build_mixed_volume(path):
    runs = {3: (build_volume(path), "signal")}
    experiment = load_or_create_experiment("mixed", "synthetic")
    try:
        for dimension in (1, 2):
            measurement = Measurement(exp=experiment)
            axes = ("gate",) if dimension == 1 else ("bias", "gate")
            for axis in axes:
                measurement.register_custom_parameter(axis, unit="V")
            name = f"signal{dimension}"
            measurement.register_custom_parameter(name, unit="A", setpoints=axes)
            with measurement.run(write_in_background=False) as saver:
                for point in itertools.product(range(3), repeat=dimension):
                    saver.add_result(*zip(axes, point, strict=True), (name, sum(point)))
            runs[dimension] = (saver.dataset.guid, name)
    finally:
        experiment.conn.close()
    return runs
