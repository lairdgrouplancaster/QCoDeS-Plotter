"""Shared fixtures and assertions for numeric sample preservation."""

from contextlib import contextmanager

from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.complex_line_data import database_state
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'array_plot',
]


@contextmanager
def array_plot(tmp_path, monkeypatch, records, *, shape=None, heatmap=False):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "exact_arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("exact arrays", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    if heatmap:
        measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "x") if heatmap else ("x",),
    )
    if shape is not None:
        measurement.set_shapes({"signal": shape})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for record in records:
                saver.add_result(*record.items())
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
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        wait_for(lambda: not plot.worker.running)
        plot.monitor.stop()
        assert errors == []
        yield window, plot
        assert errors == []
    finally:
        close_main_window(window)
        assert database_state(path) == protected
