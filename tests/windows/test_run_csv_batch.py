"""Batch CSV requests retain their selected runs through dialogs and workers."""

import csv
import threading
from pathlib import Path

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling import csv_export
from qplot.windows import _run_csv_export as batch
from tests.windows.test_plot_integration import build_synthetic_database, wait_for
from tests.windows.test_run_menu_source_lifecycle import _run_menu
from tests.windows.test_typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

typed_run_window = _typed_run_window


def _select_runs(window, runs):
    window.RunList.clearSelection()
    window.RunList.setCurrentItem(window.RunList._item_for_guid(runs[0][1]))
    for _run_id, guid, _offset in runs:
        window.RunList._item_for_guid(guid).setSelected(True)
    assert set(window.RunList.selected_run_ids()) == {run[0] for run in runs}


def _accept_batch(monkeypatch, folder, *, parameter=None, before_accept=None):
    def accept(dialog):
        choices = [dialog.parameters.itemData(index)
                   for index in range(dialog.parameters.count())]
        assert None in choices
        assert ("signal",) in choices
        assert ("reference",) in choices
        if parameter is not None:
            dialog.parameters.setCurrentIndex(choices.index((parameter,)))
        if before_accept is not None:
            before_accept()
        return qtw.QDialog.DialogCode.Accepted

    monkeypatch.setattr(batch.BatchCsvDialog, "exec", accept)
    monkeypatch.setattr(qtw.QFileDialog, "getExistingDirectory", lambda *_args: str(folder))


@pytest.mark.parametrize("parameter", [None, "signal"], ids=["all", "common-parameter"])
def test_selected_runs_export_separate_csvs_with_selected_parameters(
        typed_run_window, tmp_path, monkeypatch, parameter):
    window, runs = typed_run_window
    _select_runs(window, runs)
    folder = tmp_path / "exports"
    folder.mkdir()
    _accept_batch(monkeypatch, folder, parameter=parameter)

    window.export_selected_runs_csv()
    manager = window._export_task_manager
    wait_for(lambda: not manager.tasks)
    assert len(manager.history) == len(runs)
    assert all(task.error is None and task.stage.published for task in manager.history)
    assert len(list(folder.glob("*.csv"))) == len(runs)
    by_run = {task.result.run_id: task for task in manager.history}
    for run_id, _guid, offset in runs:
        task = by_run[run_id]
        with Path(task.destination.filename).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 5
        signal = "signal" if parameter else "signal.signal"
        gate = "gate" if parameter else "signal.gate"
        assert [float(row[gate]) for row in rows] == [-3, -1, 2, 4, 9]
        assert [float(row[signal]) for row in rows] == [offset + x for x in (-3, -1, 2, 4, 9)]
        if parameter:
            assert set(rows[0]) == {"signal", "gate"}
        else:
            assert [float(row["reference.reference"]) for row in rows] == [
                offset + x * x for x in (-3, -1, 2, 4, 9)
            ]
    assert not list(folder.glob(".*.csv"))


@pytest.mark.parametrize("cancel_at", ["review", "folder", "replacement"])
def test_batch_cancel_before_start_does_not_capture_data_or_change_outputs(
        typed_run_window, tmp_path, monkeypatch, cancel_at):
    window, runs = typed_run_window
    _select_runs(window, runs)
    folder = tmp_path / "exports"
    folder.mkdir()
    targets = []
    for run_id, guid, _offset in runs:
        key = window._current_dataset_key(guid)
        suggestion = window._default_run_csv_export_filename(
            key, ("signal", "reference"), run_id=run_id,
        )
        target = folder / Path(suggestion).name
        target.write_bytes(b"previous approved export")
        targets.append(target)

    def must_not_read(*_args, **_kwargs):
        raise AssertionError("Cancelled approval must not read measurement data")

    monkeypatch.setattr(csv_export, "export_raw_csv", must_not_read)
    _accept_batch(monkeypatch, folder)
    if cancel_at == "review":
        monkeypatch.setattr(batch.BatchCsvDialog, "exec", lambda _dialog: qtw.QDialog.DialogCode.Rejected)
    elif cancel_at == "folder":
        monkeypatch.setattr(qtw.QFileDialog, "getExistingDirectory", lambda *_args: "")
    else:
        replies = iter((qtw.QMessageBox.StandardButton.Yes, qtw.QMessageBox.StandardButton.No))
        monkeypatch.setattr(qtw.QMessageBox, "question", lambda *_args: next(replies))

    window.export_selected_runs_csv()
    assert not getattr(window, "_export_task_manager", None)
    assert all(target.read_bytes() == b"previous approved export" for target in targets)
    assert set(folder.iterdir()) == set(targets)


def test_batch_dialog_selection_changes_do_not_retarget_captured_runs(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    _select_runs(window, runs)
    folder = tmp_path / "exports"
    folder.mkdir()
    _accept_batch(monkeypatch, folder, before_accept=lambda: _select_runs(window, runs[:1]))
    window.export_selected_runs_csv()
    manager = window._export_task_manager
    wait_for(lambda: not manager.tasks)
    assert {task.result.guid for task in manager.history} == {run[1] for run in runs}
    assert all(task.stage.published for task in manager.history)


def test_switching_database_cancels_active_and_queued_batch_outputs(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    replacement = tmp_path / "other.db"
    build_synthetic_database(replacement)
    _select_runs(window, runs)
    folder = tmp_path / "exports"
    folder.mkdir()
    _accept_batch(monkeypatch, folder)
    started, release = threading.Event(), threading.Event()
    original = csv_export.export_raw_csv

    def blocked_export(filename, **kwargs):
        original_progress = kwargs["progress"]

        def progress(phase, completed, total):
            original_progress(phase, completed, total)
            if phase == "Writing CSV" and completed == 0:
                started.set()
                assert release.wait(10)

        return original(filename, **dict(kwargs, progress=progress))

    monkeypatch.setattr(csv_export, "export_raw_csv", blocked_export)
    window.export_selected_runs_csv()
    manager = window._export_task_manager
    try:
        wait_for(started.is_set)
        assert len(manager.tasks) == len(runs)
        assert window.load_file(str(replacement))
        assert all(task.cancelled for task in manager.history)
        release.set()
        wait_for(lambda: not manager.tasks and not window._database_load_active)
        assert all(not task.stage.published for task in manager.history)
        assert not list(folder.iterdir())
    finally:
        release.set()
        wait_for(lambda: not manager.tasks)


def test_batch_context_menu_keeps_selected_group_when_clicked_row_is_not_current(
        typed_run_window, monkeypatch):
    window, runs = typed_run_window
    _select_runs(window, runs)
    requested = []
    monkeypatch.setattr(window, "export_selected_runs_csv",
                        lambda: requested.append(set(window.RunList.selected_run_ids())))

    def invoke(menu, _state):
        assert len(window.RunList.selected_run_ids()) == 2
        menu.actions()[0].trigger()
        menu.close()

    _run_menu(window, runs[1][1], invoke)
    assert requested == [{run[0] for run in runs}]


@pytest.mark.parametrize("change", ["selection", "close-database", "blocked-clear"])
def test_retired_batch_context_menu_rejects_queued_action(
        typed_run_window, monkeypatch, change):
    window, runs = typed_run_window
    _select_runs(window, runs)
    requested = []
    monkeypatch.setattr(window, "export_selected_runs_csv", lambda: requested.append(True))

    def retire(menu, state):
        action = menu.actions()[0]
        if change == "selection":
            _select_runs(window, runs[:1])
        elif change == "close-database":
            window.close_current_database()
        else:
            blocker = QtCore.QSignalBlocker(window.RunList)
            window.RunList.clear()
            del blocker
        state["visible_after_retirement"] = menu.isVisible()
        action.trigger()
        menu.close()

    state = _run_menu(window, runs[1][1], retire)
    assert not state["visible_after_retirement"]
    assert not requested


def test_csv_button_uses_controls_with_multiple_selected_runs(typed_run_window, monkeypatch):
    window, runs = typed_run_window
    _select_runs(window, runs)
    captured = []
    monkeypatch.setattr(window, "_run_csv_export_request", lambda: ("key", 2, runs[0][0]))
    monkeypatch.setattr(window, "_export_measurement_csv", lambda *args, **kwargs: captured.append((args, kwargs)))
    monkeypatch.setattr(window, "export_selected_runs_csv", lambda: pytest.fail("Button switched to table selection"))
    window.exportRunCsv()
    assert captured == [(("key", 2), {"run_id": runs[0][0]})]
