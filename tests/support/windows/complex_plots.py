"""Shared fixtures and assertions for complex plots."""

import hashlib

import numpy as np
import pytest
from qcodes.dataset import Measurement, initialise_or_create_database_at, new_experiment

from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'complex_window',
    'open_plot',
]


@pytest.fixture
def complex_window(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "complex.db"
    initialise_or_create_database_at(path, journal_mode="DELETE")
    experiment = new_experiment("Complex signals", "Synthetic")
    measurement = Measurement(exp=experiment)
    for name in ("frequency", "gate", "field"):
        measurement.register_custom_parameter(name, unit="GHz" if name == "frequency" else "V")
    for name, axes in (("S11", ("frequency",)), ("S21", ("gate", "frequency")),
                       ("S12", ("field", "gate", "frequency"))):
        measurement.register_custom_parameter(name, setpoints=axes, paramtype="complex")
    values = np.array([1 + 2j, -3 + 4j, -2 - 3j, 4 - 1j])
    with measurement.run(write_in_background=False) as saver:
        for i, frequency in enumerate(range(4, 8)):
            saver.add_result(("frequency", frequency), ("S11", values[i]))
        for gate in (0, 1):
            for i, frequency in enumerate(range(4, 8)):
                saver.add_result(("frequency", frequency), ("gate", gate), ("S21", values[i] + 10 * gate))
        for field in (0, 1):
            for gate in (0, 1):
                for i, frequency in enumerate(range(4, 8)):
                    saver.add_result(("frequency", frequency), ("gate", gate), ("field", field),
                                     ("S12", values[i] + 10 * gate + 100 * field))
    guid = saver.dataset.guid
    params = {param.name: param for param in saver.dataset.get_parameters()}
    saver.dataset.conn.close()
    experiment.conn.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.update_many({"user_preference.confirm_close": False,
                               "user_preference.confirm_close_all": False})
    window.close_database(status=False)
    window.autoPlotBox.setChecked(False)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.monitor.stop()
    try:
        yield window, guid, params, values
    finally:
        close_main_window(window)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-journal").exists()


def open_plot(window, guid, param):
    previous = len(window.windows)
    window.openPlot(guid, params=[param], show=True)
    wait_for(lambda: len(window.windows) > previous)
    plot = window.windows[-1]
    wait_for(lambda: hasattr(plot, "worker") and not plot.worker.running)
    assert plot.worker.result is not None, plot._last_error_text
    plot.monitor.stop()
    return plot
