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
from qplot.windows._dragdrop import make_run_preview_mime
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _configure_temp_qplot,
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
