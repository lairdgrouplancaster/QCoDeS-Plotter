"""Shared fixtures and assertions for cut color lifecycle."""

import sys
from contextlib import closing
from pathlib import Path

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)

__all__ = [
    'heatmap',
    'ready',
]


@pytest.fixture
def heatmap(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = (tmp_path / "cut-colors.db").resolve()
    assert not path.exists()
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    with closing(connect(str(path))) as connection:
        assert Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve() == path
        experiment = load_or_create_experiment("cut colors", "lifecycle", conn=connection)
        measurement = Measurement(exp=experiment)
        measurement.register_custom_parameter("slow")
        measurement.register_custom_parameter("fast")
        measurement.register_custom_parameter("signal", setpoints=("slow", "fast"))
        with measurement.run(write_in_background=False) as saver:
            for slow in (10., 20., 30.):
                for fast in (1., 2., 4., 8.):
                    saver.add_result(("slow", slow), ("fast", fast), ("signal", slow + fast))
        guid = saver.dataset.guid
    protected = database_artifact_state(path)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    window = MainWindow()
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
        wait_for(lambda: "dataGrid" in plot.__dict__ and not plot.worker.running)
        plot.monitor.stop()
        yield window, plot, errors
    finally:
        close_main_window(window)
        assert database_artifact_state(path) == protected
        assert not errors, [(kind.__name__, str(error)) for kind, error, _tb in errors]


def ready(cut):
    wait_for(lambda: "dataGrid" in cut.__dict__ and not cut.worker.running
             and cut._axis_change_transaction is None)
    cut.monitor.stop()
