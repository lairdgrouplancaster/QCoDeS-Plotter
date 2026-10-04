"""Real timing-helper Qt actions write only their originating plot's new log."""

import csv
import hashlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore, sip
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.configuration.config import config
from qplot.diagnostics import configure_logging
from tests.windows.test_plot_integration import wait_for


def _fingerprint(database, suffixes):
    result = {}
    for suffix in suffixes:
        path = Path(f"{database}{suffix}")
        if path.exists():
            result[suffix] = (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return result


def _rows(path):
    with path.open(newline="") as stream:
        return [[float(value) for value in row] for row in csv.reader(stream)]


@pytest.fixture
def timed_plots(tmp_path, monkeypatch, qapplication, request):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "settings"
    home.mkdir()
    monkeypatch.setattr(config, "default_path", str(home))
    monkeypatch.setattr(config, "default_file", str(home / config.config_file_name))
    configure_logging(home / "qplot.log", force=True)
    source = tmp_path / "source.db"
    initialise_or_create_database_at(str(source))
    experiment = load_or_create_experiment("timing_source", sample_name="owned")
    x, y = ManualParameter("x"), ManualParameter("y")
    signal, other = ManualParameter("signal"), ManualParameter("other")
    name = getattr(request, "param", "owned_grid")
    measurement = Measurement(exp=experiment, name=name)
    for parameter in (x, y):
        measurement.register_parameter(parameter)
    for parameter in (signal, other):
        measurement.register_parameter(parameter, setpoints=(x, y))
    with measurement.run(write_in_background=False) as saver:
        for xv in (0.0, 1.0):
            for yv in (0.0, 1.0, 2.0):
                saver.add_result(
                    (x, xv),
                    (y, yv),
                    (signal, 10 * xv + yv),
                    (other, 100 + 10 * xv + yv),
                )
        dataset = saver.dataset
    np.testing.assert_array_equal(
        dataset.get_parameter_data()["signal"]["signal"], [0, 1, 2, 10, 11, 12]
    )
    guid, run_id = dataset.guid, dataset.run_id
    experiment.conn.close()
    source_state = _fingerprint(source, ("", "-wal", "-journal"))
    # This real database occupies the former predictable CSV target. The
    # suffix does not make SQLite content an owned diagnostic output.
    protected = home / f"{run_id} owned_grid.csv"
    initialise_or_create_database_at(str(protected))
    existing = load_or_create_experiment("protected_timing", sample_name="owned")
    protected_signal = ManualParameter("protected_signal")
    protected_measurement = Measurement(exp=existing)
    protected_measurement.register_parameter(protected_signal)
    with protected_measurement.run(write_in_background=False) as saver:
        saver.add_result((protected_signal, 987.654))
    Path(f"{protected}-journal").write_bytes(b"owned rollback-journal sentinel")
    suffixes = ("", "-wal", "-shm", "-journal")
    protected_state = _fingerprint(protected, suffixes)

    spec = importlib.util.spec_from_file_location(
        "owned_time_stress", root / "scripts" / "time_stress.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    window = module.testMain()
    plots = []
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        window.close_database(status=False)
        assert window.load_file(str(source))
        wait_for(lambda: not window._database_load_active)
        window.updateSelected(guid)
        wait_for(
            lambda: (
                window._selected_run_guid == guid
                and window._database_selected_run_worker is None
            )
        )
        for parameter in ("signal", "other"):
            window.open_selected_measurement(parameter)
            plot = window.windows[-1]
            plots.append(plot)
            assert isinstance(plot, module.test2d)
            wait_for(
                lambda plot=plot: (
                    hasattr(plot, "dataGrid")
                    and not plot.worker.running
                    and hasattr(plot, "_timing_log_path")
                )
            )
            plot.monitor.stop()
        yield window, plots, home, protected
    finally:
        window.close_plot_windows(confirm=False, status=False)
        window.close()
        wait_for(lambda: window._shutdown_ready)
        files = [plot._timing_log_file for plot in plots]
        for widget in (*plots, window):
            if not sip.isdeleted(widget):
                widget.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapplication.processEvents()
        assert all(file.closed for file in files)
        assert _fingerprint(source, ("", "-wal", "-journal")) == source_state
        assert _fingerprint(protected, suffixes) == protected_state
        existing.conn.close()


def test_real_loader_timing_preserves_collision_and_keeps_each_plot_log(timed_plots):
    _window, plots, home, protected = timed_plots
    paths = [Path(plot._timing_log_path) for plot in plots]
    assert len(set(paths)) == 2
    for plot, path in zip(plots, paths, strict=True):
        assert path.parent == home
        assert path != protected
        assert path.name.startswith("qplot-timing-")
        assert _rows(path)[0][0] == 6
        assert _rows(path)[0][1] >= 0
        plot.timer.emit(plot, 0.125, 8)
        plot.timer.emit(plot, 0.25, 12)
        assert _rows(path)[1:] == [[8, 0.125], [12, 0.25]]
        file = plot._timing_log_file
        plot.close()
        assert file.closed
        assert path.exists()
        assert _rows(path)[1:] == [[8, 0.125], [12, 0.25]]
        plot.timer.emit(plot, 0.5, 16)
        assert len(_rows(path)) == 3


@pytest.mark.parametrize("timed_plots", ["folder/../../unsafe-name"], indirect=True)
def test_retained_heatmap_source_keeps_its_log_without_using_run_name(timed_plots):
    _window, (target, source), home, _protected = timed_plots
    source_path = Path(source._timing_log_path)
    target_path = Path(target._timing_log_path)
    assert source_path.parent == target_path.parent == home
    assert "unsafe-name" not in source_path.name
    assert target.add_heatmap(source)
    assert source._merged_trace_users == 1
    source.close()
    assert not source._timing_log_file.closed
    source.timer.emit(source, 0.375, 8)
    assert _rows(source_path)[-1] == [8, 0.375]
    assert len(_rows(target_path)) == 1
    target.close()
    assert target._timing_log_file.closed
    assert source_path.exists()
