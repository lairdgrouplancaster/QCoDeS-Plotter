"""Selected thumbnails export only their captured measurements and sources."""

import csv
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling import csv_export
from qplot.windows import _run_csv_export as batch
from qplot.windows._widgets.preview import PreviewImageLabel
from tests._window_lifecycle import wait_for_exports
from tests.windows.test_plot_integration import build_synthetic_database, wait_for
from tests.windows.test_typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

typed_run_window = _typed_run_window


def _choose_folder(monkeypatch, folder, before_return=None):
    def choose(*_args, **_kwargs):
        if before_return is not None:
            before_return()
        return str(folder)

    monkeypatch.setattr(qtw.QFileDialog, "getExistingDirectory", choose)


def _label(scope, parameter):
    return next(label for label in scope.findChildren(PreviewImageLabel)
                if label.parameter == parameter and label.isVisible())


def _select_labels(labels):
    for index, label in enumerate(labels):
        modifier = (QtCore.Qt.KeyboardModifier.ControlModifier if index else
                    QtCore.Qt.KeyboardModifier.NoModifier)
        QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.LeftButton, modifier)
    assert all(label._selected for label in labels)


def _export_context_selection(monkeypatch, label):
    observed = []

    def choose(menu, *_args):
        actions = [action for action in menu.actions()
                   if "export selected thumbnails" in action.text().replace("&", "").lower()]
        assert len(actions) == 1
        observed.append(actions[0].text())
        actions[0].trigger()

    monkeypatch.setattr(qtw.QMenu, "exec", choose)
    QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.RightButton)
    if not observed:
        # Offscreen QTest does not synthesize the platform context-menu event.
        event = QtGui.QContextMenuEvent(
            QtGui.QContextMenuEvent.Reason.Mouse,
            label.rect().center(), label.mapToGlobal(label.rect().center()),
        )
        qtw.QApplication.sendEvent(label, event)
    assert observed


def _assert_measurement_files(window, runs, targets, folder):
    wait_for_exports(window)
    tasks = window._export_task_manager.history
    assert {(task.result.guid, task.result.parameter_names) for task in tasks} == {
        (guid, (parameter,)) for guid, parameter in targets
    }
    assert len(tasks) == len(targets)
    paths = {Path(task.destination.filename) for task in tasks}
    assert len(paths) == len(targets)
    assert set(folder.iterdir()) == paths
    offsets = {guid: offset for _run_id, guid, offset in runs}
    for task in tasks:
        parameter, = task.result.parameter_names
        with Path(task.destination.filename).open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
        assert set(reader.fieldnames) == {parameter, "gate"}
        assert [float(row["gate"]) for row in rows] == [-3, -1, 2, 4, 9]
        offset = offsets[task.result.guid]
        assert [float(row[parameter]) for row in rows] == [
            offset + (gate if parameter == "signal" else gate * gate)
            for gate in (-3, -1, 2, 4, 9)
        ]
    return paths


def test_thumbnail_context_exports_selected_measurements_across_runs(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    wait_for(lambda: all(window.RunList.run_preview_is_ready(guid)
                         for _run_id, guid, _offset in runs))
    targets = ((runs[0][1], "signal"), (runs[0][1], "reference"),
               (runs[1][1], "signal"))
    labels = [_label(window.RunList.preview_cells[guid], parameter)
              for guid, parameter in targets]
    _select_labels(labels)
    # The row's single-run target cannot substitute for the thumbnail group.
    assert len(window.RunList.selected_run_ids()) <= 1
    folder = tmp_path / "exports"
    folder.mkdir()
    _choose_folder(monkeypatch, folder)
    _export_context_selection(monkeypatch, labels[1])
    _assert_measurement_files(window, runs, targets, folder)


def test_selected_run_preview_context_exports_each_selected_measurement(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    preview = window.infoBox.preview
    window.infoBox.setCurrentWidget(preview)
    wait_for(lambda: {label.parameter for label in preview.findChildren(PreviewImageLabel)
                      if label.isVisible()} == {"signal", "reference"})
    targets = ((runs[-1][1], "signal"), (runs[-1][1], "reference"))
    labels = [_label(preview, parameter) for _guid, parameter in targets]
    _select_labels(labels)
    folder = tmp_path / "exports"
    folder.mkdir()
    _choose_folder(monkeypatch, folder)
    _export_context_selection(monkeypatch, labels[0])
    _assert_measurement_files(window, runs, targets, folder)


@pytest.mark.parametrize("cancel_at", ["folder", "second-replacement"])
def test_thumbnail_export_cancel_approves_every_file_before_starting(
        typed_run_window, tmp_path, monkeypatch, cancel_at):
    window, runs = typed_run_window
    targets = ((runs[0][1], "signal"), (runs[0][1], "reference"))
    folder = tmp_path / "exports"
    folder.mkdir()
    existing = []
    for guid, parameter in targets:
        key = window._current_dataset_key(guid)
        target = folder / Path(window._default_run_csv_export_filename(
            key, (parameter,), run_id=runs[0][0],
        )).name
        target.write_bytes(b"previous export")
        existing.append(target)

    def must_not_capture(*_args, **_kwargs):
        raise AssertionError("Cancelled thumbnail export must not capture measurements")

    monkeypatch.setattr(csv_export, "export_raw_csv", must_not_capture)
    if cancel_at == "folder":
        monkeypatch.setattr(qtw.QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: "")
    else:
        _choose_folder(monkeypatch, folder)
        replies = iter((qtw.QMessageBox.StandardButton.Yes, qtw.QMessageBox.StandardButton.No))
        monkeypatch.setattr(qtw.QMessageBox, "question", lambda *_args, **_kwargs: next(replies))
    window.export_selected_previews_csv(targets)
    assert not getattr(window, "_export_task_manager", None)
    assert set(folder.iterdir()) == set(existing)
    assert all(path.read_bytes() == b"previous export" for path in existing)


def test_thumbnail_folder_dialog_does_not_retarget_captured_measurements(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    targets = ((runs[0][1], "signal"), (runs[0][1], "reference"))
    folder = tmp_path / "exports"
    folder.mkdir()

    def change_selection():
        window.RunList.setCurrentItem(window.RunList._item_for_guid(runs[1][1]))
        window.measurementBox.setText("1")

    _choose_folder(monkeypatch, folder, change_selection)
    window.export_selected_previews_csv(targets)
    _assert_measurement_files(window, runs, targets, folder)


@pytest.mark.parametrize("change", ["close", "switch"])
def test_thumbnail_folder_dialog_source_change_aborts_before_any_export(
        typed_run_window, tmp_path, monkeypatch, change):
    window, runs = typed_run_window
    targets = ((runs[0][1], "signal"), (runs[1][1], "reference"))
    folder = tmp_path / "exports"
    folder.mkdir()
    other = tmp_path / "other.db"
    if change == "switch":
        build_synthetic_database(other)

    def change_source():
        if change == "switch":
            assert window.load_file(str(other))
        else:
            window.close_current_database()

    _choose_folder(monkeypatch, folder, change_source)
    window.export_selected_previews_csv(targets)
    assert not getattr(window, "_export_task_manager", None)
    assert not list(folder.iterdir())
    if change == "switch":
        wait_for(lambda: not window._database_load_active)


def test_thumbnail_filename_collisions_keep_both_measurements(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    targets = ((runs[0][1], "signal"), (runs[0][1], "reference"))
    folder = tmp_path / "exports"
    folder.mkdir()
    # Custom filename settings or sanitisation can map distinct parameters to
    # the same suggestion. Both exact measurements must still be published.
    monkeypatch.setattr(window, "_default_run_csv_export_filename",
                        lambda *_args, **_kwargs: str(folder / "measurement.csv"))
    monkeypatch.setattr(batch, "suggested_export_filename", lambda _name: "quantity")
    _choose_folder(monkeypatch, folder)
    window.export_selected_previews_csv(targets)
    paths = _assert_measurement_files(window, runs, targets, folder)
    assert {path.name for path in paths} == {
        "measurement_quantity.csv", "measurement_quantity_2.csv",
    }


def test_thumbnail_qcodes_filename_preference_identifies_each_measurement(
        typed_run_window, tmp_path, monkeypatch):
    window, runs = typed_run_window
    guid = runs[0][1]
    # The basic run-list view may omit this optional cached field. Supply the
    # fixture writer's captured ID to exercise the QCoDeS naming preference.
    captured_run_id = runs[0][0]
    window.RunList._item_for_guid(guid).run_metadata["captured_run_id"] = captured_run_id
    window.config.config["user_preference"]["csv_filename_format"] = "qcodes"
    targets = ((guid, "signal"), (guid, "reference"))
    folder = tmp_path / "exports"
    folder.mkdir()
    _choose_folder(monkeypatch, folder)
    window.export_selected_previews_csv(targets)
    paths = _assert_measurement_files(window, runs, targets, folder)
    assert {path.name for path in paths} == {
        f"{captured_run_id}_{guid}_{parameter}.csv"
        for _guid, parameter in targets
    }
