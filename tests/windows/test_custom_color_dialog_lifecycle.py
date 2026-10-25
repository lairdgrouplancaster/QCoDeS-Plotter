"""A cut's real custom-color dialog belongs to that cut's lifetime."""

import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

from qplot.windows._widgets.dropbox import picker_1d
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _build_two_heatmap_database,
    _configure_temp_qplot,
    _database_artifact_state,
    _wait_for,
)


@pytest.fixture
def color_cut(tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "custom-color.db"
    _run_id, guid, _primary, _secondary = _build_two_heatmap_database(path)
    protected = {path: _database_artifact_state(path)}
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    window = MainWindow()
    monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
    service = None
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active)
        service = window._trusted_read_service
        window.open_run_preview_plot(guid, "signal_a")
        _wait_for(lambda: len(window.windows) == 1)
        heatmap = window.windows[0]
        _wait_for(lambda: "dataGrid" in heatmap.__dict__ and not heatmap.worker.running)
        heatmap.spinBox.setValue(0)
        heatmap.z_index = [1, 1]
        heatmap.openSweep("h")
        cut = window.windows[-1]
        cut.spinBox.setValue(0)
        yield window, heatmap, cut, protected
    finally:
        close_main_window(window)
        assert service is None or service.closed
        for database, original in protected.items():
            _assert_reader_artifact_invariant(database, original)
        assert not errors


def _choose_custom_color(cut, callback):
    """Run the actual modal, with bounded responders even on assertion failure."""
    box = cut.axes_dock.findChild(picker_1d).color_box
    assert box.isEnabled()
    index = box.findText("Custom")
    assert index >= 0
    state = {}
    poll = QtCore.QTimer()
    timeout = QtCore.QTimer()

    def progress():
        dialog = qtw.QApplication.activeModalWidget()
        if not isinstance(dialog, qtw.QColorDialog):
            return
        state["dialog"] = dialog
        try:
            callback(dialog, state)
        except Exception as error:
            state["error"] = error
            poll.stop()
            dialog.reject()

    def abort():
        state["timed_out"] = True
        dialog = qtw.QApplication.activeModalWidget()
        if isinstance(dialog, qtw.QDialog):
            dialog.reject()

    poll.setInterval(10)
    poll.timeout.connect(progress)
    timeout.setSingleShot(True)
    timeout.timeout.connect(abort)
    try:
        poll.start()
        timeout.start(30_000)
        box.setCurrentIndex(index)
        box.activated.emit(index)
    finally:
        poll.stop()
        timeout.stop()
    assert not state.get("timed_out"), "Custom color dialog did not finish"
    if "error" in state:
        raise state["error"]
    assert "dialog" in state
    assert sip.isdeleted(state["dialog"]) or not state["dialog"].isVisible()
    return state


@pytest.mark.parametrize("accept", [False, True], ids=["cancel", "accept"])
def test_custom_color_accept_and_cancel_keep_live_cut_samples(color_cut, accept):
    window, heatmap, cut, _protected = color_cut
    _wait_for(lambda: "axis_data" in cut.__dict__ and not cut.worker.running)
    before = tuple(values.copy() for values in cut.line.getOriginalDataset())
    previous_color = cut.line.opts["pen"].color().name()

    def choose(dialog, _state):
        dialog.setCurrentColor(QtGui.QColor("#123456"))
        (dialog.accept if accept else dialog.reject)()

    _choose_custom_color(cut, choose)
    expected = "#123456" if accept else previous_color
    assert cut in window.windows and cut.isVisible()
    assert cut.line.opts["pen"].color().name() == expected
    assert heatmap.sweep_lines[cut.sweep_id].pen.color().name() == expected
    for actual, original in zip(cut.line.getOriginalDataset(), before, strict=True):
        np.testing.assert_array_equal(actual, original)


@pytest.mark.parametrize("pending", [False, True], ids=["loaded", "pending"])
@pytest.mark.parametrize("close_all", [False, True], ids=["cut", "all"])
def test_cut_close_rejects_its_custom_color_dialog(color_cut, pending, close_all):
    window, heatmap, cut, _protected = color_cut
    if not pending:
        _wait_for(lambda: "axis_data" in cut.__dict__ and not cut.worker.running)
    else:
        assert "axis_data" not in cut.__dict__
    previous_color = cut.line.opts["pen"].color().name()

    def close_owner(dialog, state):
        dialog.setCurrentColor(QtGui.QColor("#123456"))
        (window.closeAllPlotsButton.click if close_all else cut.close)()
        state["visible_after_close"] = dialog.isVisible()
        # On the broken implementation the orphan would wait forever. Finish
        # its real loop while preserving the observation made after closure.
        if dialog.isVisible():
            dialog.accept()

    state = _choose_custom_color(cut, close_owner)
    assert not state["visible_after_close"]
    assert cut not in window.windows and not cut.isVisible()
    assert cut.line.opts["pen"].color().name() == previous_color
    assert cut.sweep_id not in heatmap.sweep_lines
    if close_all:
        assert not window.windows


@pytest.mark.skipif(os.name == "nt", reason="Windows locks the open SQLite main file against replacement.")
def test_automatic_database_replacement_rejects_custom_color_dialog(color_cut, tmp_path):
    window, heatmap, cut, protected = color_cut
    _wait_for(lambda: "axis_data" in cut.__dict__ and not cut.worker.running)
    previous_color = cut.line.opts["pen"].color().name()
    path = Path(heatmap._dataset_key.database_path)
    replacement = tmp_path / "replacement.db"
    _build_two_heatmap_database(replacement)
    replacement_state = _database_artifact_state(replacement)
    retired = tmp_path / "retired.db"
    window.spinBox.setValue(.1)
    assert window.monitor.isActive()

    def replace_source(dialog, state):
        if not state.get("replaced"):
            # Simulate an independent writer replacing only test-owned files.
            # The application's ordinary refresh timer detects this while the
            # user's real color picker keeps the Qt event loop running.
            path.rename(retired)
            protected[retired] = protected.pop(path)
            replacement.rename(path)
            protected[path] = replacement_state
            state["replaced"] = True
            dialog.setCurrentColor(QtGui.QColor("#123456"))
        elif not window.windows:
            state["visible_after_close"] = dialog.isVisible()
            dialog.accept()

    state = _choose_custom_color(cut, replace_source)
    assert state["replaced"]
    _wait_for(lambda: not window.windows and not window._database_load_active)
    assert not state.get("visible_after_close", False)
    assert cut._closed and heatmap._closed
    assert cut.line.opts["pen"].color().name() == previous_color
    assert not window.dataset_holder
