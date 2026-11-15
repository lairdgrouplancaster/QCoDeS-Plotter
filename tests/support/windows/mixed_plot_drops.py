"""Shared fixtures and assertions for mixed plot drops."""

import sys
from types import SimpleNamespace

import pytest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from qplot.windows._dragdrop import make_run_preview_mime
from tests._window_lifecycle import close_main_window
from tests.support.windows.heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _configure_temp_qplot,
    _database_artifact_state,
    _PreviewDropEvent,
    _wait_for,
)

__all__ = [
    '_added',
    '_axis_warning_item',
    '_data_identities',
    '_drop',
    '_wait_for',
    'mixed_plot',
]


@pytest.fixture(params=["line", "heatmap"])
def mixed_plot(request, tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "mixed.db"
    initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("mixed", sample_name="test")
    runs = {}
    specs = [
        ("line", "y", ("x",), ("V",), "A"),
        ("heatmap", "z", ("y", "x"), ("A", "V"), "A"),
        ("other_line", "w", ("u",), ("K",), "Hz"),
        ("other_heatmap", "q", ("v", "u"), ("T", "K"), "V"),
        ("third_line", "r", ("t",), ("m",), "m"),
    ]
    for key, name, axes, units, unit in specs:
        coordinates = [ManualParameter(axis, unit=axis_unit) for axis, axis_unit in zip(axes, units, strict=True)]
        signal = ManualParameter(name, unit=unit)
        measurement = Measurement(exp=experiment)
        for coordinate in coordinates:
            measurement.register_parameter(coordinate)
        measurement.register_parameter(signal, setpoints=tuple(coordinates))
        with measurement.run() as saver:
            if len(axes) == 1:
                for x in range(3):
                    saver.add_result((coordinates[0], x), (signal, x))
            else:
                for y in (10, 20):
                    for x in range(3):
                        saver.add_result((coordinates[0], y), (coordinates[1], x), (signal, y + x))
            runs[key] = SimpleNamespace(guid=saver.dataset.guid, name=name, axes=axes)
    experiment.conn.close()
    artifacts = _database_artifact_state(path)
    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    exceptions = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: exceptions.append(args))
    # Surface unexpected static error dialogs instead of blocking headless runs.
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *args: exceptions.append(args))
    try:
        window.close_database(status=False)
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active and window._selected_run_guid)
        window.openPlot(guid=runs[request.param].guid)
        _wait_for(lambda: bool(window.windows))
        target = window.windows[-1]
        _wait_for(lambda: not target.worker.running and (
            hasattr(target, "dataGrid") if request.param == "heatmap" else len(target.axis_data["x"]) == 3
        ))
        yield window, target, runs
    finally:
        close_main_window(window)
        _assert_reader_artifact_invariant(path, artifacts)
        assert exceptions == []


def _drop(target, run, *, pending=False):
    event = _PreviewDropEvent(make_run_preview_mime(
        run.guid, run.name, None if pending else run.axes, axes_pending=pending,
    ))
    assert target._handle_preview_drag_drop(event)
    assert event.accepted
    assert not event.ignored


def _added(target, run):
    registry_name = "lines" if len(run.axes) == 1 else "heatmaps"
    def find():
        return next((item for key, item in target.__dict__.get(registry_name, {}).items()
                     if getattr(key, "parameter_name", None) == run.name), None)
    _wait_for(lambda: find() is not None)
    layer = find()
    _wait_for(lambda: layer.getData()[0] is not None if registry_name == "lines" else layer.geometry is not None)
    return layer


def _axis_warning_item(dialog, kind, key):
    row = next(row for row in range(dialog.table.rowCount())
               if dialog._row_entry(row) == (kind, key))
    return dialog.table.item(row, dialog._COL_WARNING)


def _data_identities(target):
    return [(kind, target._stored_trace_key(key, target.lines[key]) if kind == "trace" else key)
            for kind, key in target._plot_item_order]
