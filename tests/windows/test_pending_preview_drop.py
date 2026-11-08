import numpy as np
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from qplot.windows._dragdrop import (
    make_run_preview_mime,
    run_preview_payloads_from_mime,
)
from qplot.windows._widgets._preview_selection import active_preview_labels
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _configure_temp_qplot,
    _database_artifact_state,
    _wait_for,
)


@pytest.mark.parametrize("separate_run", [False, True])
def test_pending_thumbnail_drop_loads_unopened_line_source(tmp_path, monkeypatch, separate_run):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "pending-lines.db"
    initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("pending-lines", sample_name="sample")
    x = ManualParameter("x")
    a = ManualParameter("a")
    b = ManualParameter("b")
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(x)
    measurement.register_parameter(a, setpoints=(x,))
    measurement.register_parameter(b, setpoints=(x,))
    with measurement.run() as saver:
        for value in range(3):
            saver.add_result((x, value), (a, value + 10), (b, value + 20))
        guid = saver.dataset.guid
    source_guid = guid
    if separate_run:
        with measurement.run() as saver:
            for value in range(3):
                saver.add_result((x, value), (a, value + 10), (b, value + 20))
            source_guid = saver.dataset.guid
    saver.dataset.conn.close()
    experiment.conn.close()

    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    try:
        window.close_database(status=False)
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active and window._selected_run_guid)
        window.open_run_preview_plot(guid, "a")
        _wait_for(lambda: bool(window.windows))
        target = window.windows[-1]
        _wait_for(lambda: not getattr(target.worker, "running", False))
        assert len(window.windows) == 1
        target.show()
        qtw.QApplication.processEvents()
        mime = make_run_preview_mime(source_guid, "b", axes_pending=True)
        viewport = target.widget.viewport()
        position = viewport.rect().center()
        enter = QtGui.QDragEnterEvent(position, QtCore.Qt.DropAction.CopyAction, mime,
                                    QtCore.Qt.MouseButton.LeftButton,
                                    QtCore.Qt.KeyboardModifier.NoModifier)
        qtw.QApplication.sendEvent(viewport, enter)
        assert enter.isAccepted()
        drop = QtGui.QDropEvent(QtCore.QPointF(position), QtCore.Qt.DropAction.CopyAction, mime,
                               QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.KeyboardModifier.NoModifier)
        qtw.QApplication.sendEvent(viewport, drop)
        assert drop.isAccepted()
        _wait_for(lambda: len(target.lines) == 2)
        secondary = next(line for key, line in target.lines.items()
                         if getattr(key, "parameter_name", None) == "b")
        _wait_for(lambda: secondary.getData()[1] is not None and len(secondary.getData()[1]) == 3)
        np.testing.assert_array_equal(secondary.getData()[1], [20, 21, 22])
    finally:
        close_main_window(window)


def test_selected_thumbnail_group_adds_every_unopened_run_to_one_plot(tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "selected-lines.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("selected-lines", sample_name="sample")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("a", setpoints=("x",))
    measurement.register_custom_parameter("b", setpoints=("x",))
    guids = []
    for index in range(19):
        with measurement.run(write_in_background=False) as saver:
            for x in range(3):
                saver.add_result(("x", x), ("a", x), ("b", x + 10 * index))
        guids.append(saver.dataset.guid)
    saver.dataset.conn.close()
    experiment.conn.close()
    before = _database_artifact_state(path)
    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.update_many({"user_preference.confirm_close": False,
                               "user_preference.confirm_close_all": False})
    try:
        window.close_database(status=False)
        window.autoPlotBox.setChecked(False)
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active and window._selected_run_guid)
        window.show()
        window.open_run_preview_plot(guids[0], "a")
        _wait_for(lambda: len(window.windows) == 1)
        target = window.windows[0]
        _wait_for(lambda: not target.worker.running)
        labels = active_preview_labels(window.RunList)
        for label in labels:
            label.set_selected(label.parameter == "b")
        source = next(label for label in labels if label._selected)
        captured = []

        class Drag:
            def __init__(self, _source):
                pass

            def setMimeData(self, mime):
                self.mime = mime
                captured.extend(run_preview_payloads_from_mime(mime))

            def setPixmap(self, _pixmap):
                pass

            def setHotSpot(self, _point):
                pass

            def exec(self, _action):
                viewport = target.widget.viewport()
                position = viewport.rect().center()
                enter = QtGui.QDragEnterEvent(position, QtCore.Qt.DropAction.CopyAction, self.mime,
                                              QtCore.Qt.MouseButton.LeftButton,
                                              QtCore.Qt.KeyboardModifier.NoModifier)
                qtw.QApplication.sendEvent(viewport, enter)
                assert enter.isAccepted()
                drop = QtGui.QDropEvent(QtCore.QPointF(position), QtCore.Qt.DropAction.CopyAction, self.mime,
                                       QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
                qtw.QApplication.sendEvent(viewport, drop)
                assert drop.isAccepted()

        monkeypatch.setattr(QtGui, "QDrag", Drag)
        source._start_drag()
        assert len(captured) == 19
        assert {preview["guid"] for preview in captured} == set(guids)
        _wait_for(lambda: len(target.lines) == 20, timeout=30)
        _wait_for(lambda: all(line.getData()[1] is not None and len(line.getData()[1]) == 3
                             for line in target.lines.values()))
        assert len(window.windows) == 1
        for key, line in target.lines.items():
            if getattr(key, "parameter_name", None) == "b":
                index = guids.index(key.dataset_key.guid)
                np.testing.assert_array_equal(line.getData()[1], np.arange(3) + 10 * index)
    finally:
        close_main_window(window)
    _assert_reader_artifact_invariant(path, before)
