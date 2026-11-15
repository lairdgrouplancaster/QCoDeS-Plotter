"""Shared fixtures and assertions for refresh interval precision."""

import hashlib
from contextlib import contextmanager

from PyQt6 import QtCore
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.configuration.config import config
from qplot.windows._plotWin import plotWidget
from qplot.windows._refresh_interval import (
    refresh_interval_value,
)
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import wait_for

__all__ = [
    'assert_interval',
    'live_view',
    'wait_for_plot_load',
]


@contextmanager
def live_view(tmp_path, monkeypatch, rate, *, heatmap=False):
    """View only a new current QCoDeS measurement with its writer still open."""
    monkeypatch.setattr(config, "default_path", str(tmp_path / "settings"))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings/config.json"))
    cfg = config()
    cfg.update_many({
        "user_preference.default_refresh_rate": rate,
        "user_preference.confirm_close": False,
        "user_preference.confirm_close_all": False,
    })
    path = tmp_path / "measurement.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("refresh_precision", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    if heatmap:
        measurement.register_custom_parameter("y")
    setpoints = ("y", "x") if heatmap else ("x",)
    for name in ("signal_a", "signal_b"):
        measurement.register_custom_parameter(name, setpoints=setpoints)
    window = None
    errors = []
    try:
        with measurement.run(write_in_background=False) as saver:
            for index in range(4):
                coordinates = [("x", float(index % 2))]
                if heatmap:
                    coordinates.append(("y", float(index // 2)))
                saver.add_result(*coordinates, ("signal_a", index + 1.),
                                 ("signal_b", index + 11.))
            saver.flush_data_to_database()
            before = hashlib.sha256(path.read_bytes()).digest()
            parameters = {p.name: p for p in saver.dataset.get_parameters()}
            window = MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
            with QtCore.QSignalBlocker(window.monitor):
                assert window.load_file(str(path))
                wait_for(lambda: not window._database_load_active)

            def open_plot(name, *, show=True):
                with QtCore.QSignalBlocker(window.monitor), monkeypatch.context() as launch_patch:
                    # Metadata now arrives asynchronously. Fence the new plot's
                    # timer before its first worker, including the event batches
                    # needed to wait for the window itself to appear.
                    blockers = []
                    def fence_initial_load(plot, _worker):
                        if plot not in window.windows:
                            blockers.append(QtCore.QSignalBlocker(plot.monitor))
                    launch_patch.setattr(plotWidget, "_refresh_worker_will_start",
                                         fence_initial_load, raising=False)
                    prior_plot_count = len(window.windows)
                    window.openPlot(guid=saver.dataset.guid, params=[parameters[name]], show=show)
                    wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
                    plot = window.windows[-1]
                    monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
                    wait_for_plot_load(window, plot)
                    for blocker in blockers:
                        blocker.unblock()
                assert plot._last_error_text is None
                return plot

            try:
                yield window, open_plot
            finally:
                close_main_window(window)
                window = None
                assert errors == []
                assert hashlib.sha256(path.read_bytes()).digest() == before
    finally:
        if window is not None:
            close_main_window(window)
        experiment.conn.close()


def wait_for_plot_load(window, plot):
    # Drain one finite load. A live timer can start a replacement worker in the
    # same event batch as publication, hiding the idle state from the poll.
    # Leave both timers active at their exact configured intervals and restore
    # emissions before testing the refresh controls.
    with QtCore.QSignalBlocker(window.monitor), QtCore.QSignalBlocker(plot.monitor):
        wait_for(lambda: not plot.worker.running)


def assert_interval(owner, rate):
    assert refresh_interval_value(owner.spinBox) == rate
    assert owner.monitor.isActive() == (rate > 0)
    if rate > 0:
        assert owner.monitor.interval() == max(1, round(rate * 1000))
