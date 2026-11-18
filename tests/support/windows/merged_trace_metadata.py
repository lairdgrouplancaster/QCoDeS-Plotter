"""Shared fixtures and assertions for merged trace metadata."""

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from qplot.windows._plot_feedback import PlotWindowFeedbackMixin
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    configure_temp_qplot,
    force_snapshot_fallback,
    wait_for,
)

__all__ = [
    'assert_axis',
    'assert_samples',
    'merge',
    'merged_plots',
]


@pytest.fixture
def merged_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    # These completed runs exercise trace rendering. Open their metadata from
    # protected snapshots so setup does not depend on live-reader requests.
    force_snapshot_fallback(monkeypatch)
    setup_errors = []
    monkeypatch.setattr(
        PlotWindowFeedbackMixin, "show_error",
        lambda _plot, *error: setup_errors.append(error),
    )
    database_path = tmp_path / "merged.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("merged metadata", sample_name="test")
    gate = ManualParameter("gate", label="Gate voltage", unit="V")
    signal = ManualParameter("signal", label="Signal", unit="nA")
    coordinates = np.asarray(getattr(request, "param", [0, 1, 2, 3]), dtype=float)
    values = coordinates ** 2
    guids = []
    for index in range(3):
        measurement = Measurement(exp=experiment, name=f"line {index}")
        measurement.register_parameter(gate)
        measurement.register_parameter(signal, setpoints=(gate,))
        with measurement.run(write_in_background=False) as datasaver:
            for coordinate, value in zip(coordinates, values, strict=True):
                datasaver.add_result((gate, coordinate), (signal, value))
            dataset = datasaver.dataset
            guids.append(dataset.guid)
    # Measurement datasets share the experiment's writer connection.
    experiment.conn.close()
    protected = {
        suffix: (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
        for suffix in ("", "-wal", "-journal")
        for path in [database_path.with_name(database_path.name + suffix)]
    }
    window = main_window.MainWindow()
    monkeypatch.setattr(window, "show_error", lambda *error: setup_errors.append(error))
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        window.monitor.stop()
        assert not setup_errors
        plots = []
        for guid in guids:
            window.openPlot(guid=guid, show=True)
            assert not setup_errors
            plot, = [candidate for candidate in window.windows if candidate._dataset_key.guid == guid]
            wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
            assert not setup_errors
            plot.monitor.stop()
            monkeypatch.setattr(plot, "show_error", lambda *_args: None)
            assert not plot.ds.running
            plots.append(plot)
        yield window, plots, coordinates, values
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


def merge(window, host, source, *, x_axis="Bottom", y_axis="Right"):
    assert window.add_trace_to_plot(
        host, source._dataset_key, source.param.name, param=source.param,
    )
    key = host._window_trace_key(source)
    line = host.lines[key]
    style = host._trace_styles[key]
    style.x_axis, style.y_axis = x_axis, y_axis
    host._apply_trace_style(key, line)
    return key, line


def assert_axis(plot, side, label, unit):
    axis = plot.plot.getAxis(side)
    assert (axis.labelText, axis.labelUnits) == (label, unit)


def assert_samples(line, x, y):
    actual_x, actual_y = line.getOriginalDataset()
    np.testing.assert_allclose(actual_x, x)
    np.testing.assert_allclose(actual_y, y)
