"""Database export menu, approval and asynchronous publication."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from PyQt6 import QtGui, QtWidgets
from qcodes.dataset import (
    DataSetType,
    Measurement,
    initialise_or_create_database_at,
    load_by_id,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.datahandling.file_identity import database_instance
from qplot.windows import _database_export
from tests.support.windows.export_tasks import export_window as export_window
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    trusted_database_artifact_state,
    wait_for,
)


def prepare(window, tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY, result_table_name TEXT)")
        connection.execute("CREATE TABLE results_197(value REAL)")
        connection.execute("INSERT INTO runs VALUES (197, 'results_197')")
    window._loaded_database_instance = database_instance(source)
    window.fileTextbox.setText(str(source))
    window.RunList.addRuns({197: {"guid": "export-run"}})
    window._sync_database_generation_controls()
    monkeypatch.setattr(window, "_generation_gate_allows_action", lambda **_: True)
    target = tmp_path / "copy.db"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    return source, target


def test_menu_exports_standalone_database(export_window, tmp_path, monkeypatch):
    window, errors = export_window
    source, target = prepare(window, tmp_path, monkeypatch)
    before = source.read_bytes()
    action = window.findChild(QtGui.QAction, "exportDatabaseCopyAction")
    assert action.text().replace("&", "") == "Save a copy of the database…"
    assert action.isEnabled()
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
    prompts = []
    monkeypatch.setattr(_database_export, "confirm_action", lambda *args, **kwargs: prompts.append(kwargs) or False)
    _database_export.export_database(window)
    assert not errors and prompts == [{"affirmative": "Choose another name…"}]
    assert path.read_bytes() == content
    assert not getattr(window, "_export_task_manager", None)


def test_occupied_copy_destination_can_be_corrected(export_window, tmp_path, monkeypatch):
    window, errors = export_window
    _, target = prepare(window, tmp_path, monkeypatch)
    target.write_bytes(b"existing file")
    retry_target = tmp_path / "another-copy.db"
    choices = iter((target, retry_target))
    suggestions = []

    def choose(_owner, _title, suggested, *_args, **_kwargs):
        suggestions.append(suggested)
        return str(next(choices)), ""

    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", choose)
    monkeypatch.setattr(_database_export, "confirm_action", lambda *a, **kw: True)
    _database_export.export_database(window)
    wait_for(lambda: not window._export_task_manager.tasks)
    assert suggestions[1] == str(target)
    assert target.read_bytes() == b"existing file"
    assert retry_target.read_bytes().startswith(b"SQLite format 3")
    assert not errors


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
        window._database_loader.invalidate(cancel=False)
        release.set()
        wait_for(lambda: task.done)
        assert task.cancelled
        assert not target.exists()
        assert not list(tmp_path.glob(".copy.db.*"))
        assert not errors
    finally:
        release.set()
        wait_for(lambda: task.done)


def test_selected_database_action_captures_only_run_selection(export_window, tmp_path, monkeypatch):
    window, errors = export_window
    _, target = prepare(window, tmp_path, monkeypatch)
    monkeypatch.setattr(window.RunList, "selected_run_ids", lambda: [197])
    window._sync_database_generation_controls()
    captured = []
    def write(filename, **kwargs):
        captured.append(kwargs["run_ids"])
        Path(filename).write_bytes(b"test copy")
    monkeypatch.setattr(_database_export, "export_database_copy", write)
    action = window.findChild(QtGui.QAction, "exportSelectedRunsDatabaseAction")
    assert action.isEnabled()
    action.trigger()
    wait_for(lambda: not window._export_task_manager.tasks)
    assert captured == [(197,)]
    assert target.exists()
    assert not errors


def test_selected_database_empty_selection_does_not_export(export_window, monkeypatch):
    window, _ = export_window
    monkeypatch.setattr(window.RunList, "selected_run_ids", lambda: [])
    monkeypatch.setattr(_database_export, "start_export_task", lambda **_: pytest.fail("Unexpected export"))
    _database_export.export_database(window, selected=True)


def record_export_runs(source, storage):
    initialise_or_create_database_at(str(source), journal_mode="DELETE")
    guids = {}
    with closing(connect(str(source))) as writer:
        experiment = load_or_create_experiment("export", "sample", conn=writer)
        for index, kind in enumerate(storage, start=1):
            measurement = Measurement(exp=experiment, name=kind)
            measurement.register_custom_parameter("field", unit="T")
            measurement.register_custom_parameter("flux", unit="Wb", setpoints=("field",))
            dataset_class = DataSetType.DataSetInMem if kind == "netcdf-only" else DataSetType.DataSet
            with measurement.run(dataset_class=dataset_class, write_in_background=False) as saver:
                for field in (-2, 0, 5):
                    saver.add_result(("field", field), ("flux", 10 * index + field))
            dataset = saver.dataset
            assert dataset.run_id == index
            guids[index] = dataset.guid
            if kind in {"netcdf-only", "embedded-with-copy"}:
                dataset.export("netcdf", path=source.parent)
                assert Path(dataset.export_info.export_paths["nc"]).is_file()
    return guids


@pytest.mark.parametrize(("storage", "selected", "refused_run"), [
    pytest.param(("embedded", "netcdf-only"), None, 2, id="all-includes-netcdf"),
    pytest.param(("embedded", "netcdf-only"), (1, 2), 2, id="selection-includes-netcdf"),
    pytest.param(("embedded", "netcdf-only"), (2,), 2, id="netcdf-only-selection"),
    pytest.param(("embedded", "netcdf-only"), (1,), None, id="unselected-netcdf"),
    pytest.param(("embedded-with-copy",), None, None, id="all-with-optional-copy"),
    pytest.param(("embedded-with-copy",), (1,), None, id="selected-with-optional-copy"),
])
def test_database_menu_exports_only_embedded_measurements(
    export_window, tmp_path, monkeypatch, storage, selected, refused_run,
):
    window, errors = export_window
    originals = tmp_path / "originals"
    originals.mkdir()
    source = originals / "measurements.db"
    guids = record_export_runs(source, storage)
    before = trusted_database_artifact_state(source)
    assert window.load_file(str(source))
    wait_for(lambda: not window._database_load_active and window.RunList.topLevelItemCount() == len(guids))
    action = window.exportDatabaseAction
    if selected is not None:
        window.RunList.clearSelection()
        for run_id in selected:
            window.RunList._item_for_guid(guids[run_id]).setSelected(True)
        assert set(window.RunList.selected_run_ids()) == set(selected)
        action = window.findChild(QtGui.QAction, "exportSelectedRunsDatabaseAction")

    target = tmp_path / "copy.db"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert action.isEnabled()
    action.trigger()
    task = window._export_task_manager.history[-1]
    wait_for(lambda: task.done)
    assert not task.cancelled
    assert_database_artifacts_unchanged_except_trusted_shm(source, before)
    assert not list(tmp_path.glob(".copy.db.*"))
    if refused_run is not None:
        assert f"Run {refused_run} stores measurement data in separate files" in str(task.error)
        assert not target.exists()
        return

    assert task.error is None and not errors
    assert not list(tmp_path.glob("copy.db-*"))
    service = window._trusted_read_service
    window.close_database()
    if service is not None:
        wait_for(lambda: service.closed)
    # Exercise the recipient's copy with both the source DB and the NetCDF paths offline.
    originals.rename(tmp_path / "offline")
    expected_ids = sorted(guids if selected is None else selected)
    with closing(connect(str(target))) as copy:
        assert copy.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall() == [
            (run_id,) for run_id in expected_ids
        ]
        for run_id in expected_ids:
            dataset = load_by_id(run_id, conn=copy)
            assert dataset.guid == guids[run_id]
            values = dataset.get_parameter_data()["flux"]
            assert values["field"].tolist() == [-2, 0, 5]
            assert values["flux"].tolist() == [10 * run_id - 2, 10 * run_id, 10 * run_id + 5]
