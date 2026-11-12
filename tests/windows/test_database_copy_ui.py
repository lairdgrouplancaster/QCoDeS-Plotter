"""Database export menu, approval and asynchronous publication."""

import sqlite3
from pathlib import Path

import pytest
from PyQt6 import QtGui, QtWidgets

from qplot.datahandling.file_identity import database_instance
from qplot.windows import _database_export
from tests.windows.test_export_tasks import export_window as export_window
from tests.windows.test_plot_integration import wait_for


def prepare(window, tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO runs VALUES (197)")
    window._loaded_database_instance = database_instance(source)
    window.fileTextbox.setText(str(source))
    monkeypatch.setattr(window, "_generation_gate_allows_action", lambda **_: True)
    target = tmp_path / "copy.db"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    return source, target


def test_menu_exports_standalone_database(export_window, tmp_path, monkeypatch):
    window, errors = export_window
    source, target = prepare(window, tmp_path, monkeypatch)
    before = source.read_bytes()
    action = window.findChild(QtGui.QAction, "exportDatabaseCopyAction")
    assert action.text() == "Export Database Copy…"
    action.trigger()
    manager = window._export_task_manager
    wait_for(lambda: not manager.tasks)
    assert manager.history[-1].error is None
    assert not errors
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT run_id FROM runs").fetchall() == [(197,)]
    assert source.read_bytes() == before
    assert not list(tmp_path.glob(".copy.db.*"))
    assert manager.dialog.windowTitle() == "Exports"


@pytest.mark.parametrize("existing", ["database", "wal", "shm", "journal"])
def test_reject_existing_database_or_sidecars(export_window, tmp_path, monkeypatch, existing):
    window, errors = export_window
    source, target = prepare(window, tmp_path, monkeypatch)
    path = target if existing == "database" else Path(str(target) + "-" + existing)
    content = source.read_bytes() if existing == "database" else b"existing sidecar"
    path.write_bytes(content)
    _database_export.export_database(window)
    assert errors
    assert path.read_bytes() == content
    assert not getattr(window, "_export_task_manager", None)


def test_switch_database_before_publication_cancels(export_window, tmp_path, monkeypatch):
    window, errors = export_window
    _, target = prepare(window, tmp_path, monkeypatch)
    # Hold the queued writer while the GUI selection changes.
    import threading
    started, release = threading.Event(), threading.Event()

    def write(filename, **_kwargs):
        started.set()
        assert release.wait(5)
        Path(filename).write_bytes(b"private completed copy")

    monkeypatch.setattr(_database_export, "export_database_copy", write)
    _database_export.export_database(window)
    task = window._export_task_manager.history[-1]
    try:
        wait_for(started.is_set)
        window._database_load_generation += 1
        release.set()
        wait_for(lambda: task.done)
        assert task.cancelled
        assert not target.exists()
        assert not list(tmp_path.glob(".copy.db.*"))
        assert not errors
    finally:
        release.set()
        wait_for(lambda: task.done)
