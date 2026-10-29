"""Live Auto-plot publications participate in an accepted close confirmation."""

import hashlib
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.diagnostics import configure_logging
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def _protected_artifacts(database):
    artifacts = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            artifacts[suffix] = None
            continue
        metadata = path.stat()
        artifacts[suffix] = (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return artifacts


def _record_run(writer, experiment, database, offset):
    assert Path(writer.execute("PRAGMA database_list").fetchone()[2]).resolve() == database
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("voltage")
    measurement.register_custom_parameter("current", setpoints=("voltage",))
    with measurement.run(write_in_background=False) as saver:
        assert Path(
            saver.dataset.conn.execute("PRAGMA database_list").fetchone()[2]
        ).resolve() == database
        for voltage in (-3., -1., 0., 2., 9.):
            saver.add_result(("voltage", voltage), ("current", offset + voltage * 7))
    return saver.dataset.guid


@pytest.mark.parametrize("close_database", [False, True], ids=["plots", "database"])
@pytest.mark.parametrize("accept", [False, True], ids=["cancel", "accept"])
def test_close_confirmation_includes_live_auto_plot(
    tmp_path, monkeypatch, qapplication, close_database, accept,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    database = (tmp_path / "live-close.db").resolve()
    assert not database.exists()
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    callback_errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: callback_errors.append(error))

    with closing(connect(str(database))) as writer:
        experiment = load_or_create_experiment("live close", "confirmation", conn=writer)
        first_guid = _record_run(writer, experiment, database, 13.)
        window = MainWindow()
        state = {}
        poll = QtCore.QTimer()
        timeout = QtCore.QTimer()
        try:
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = True
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            assert window.load_file(str(database))
            wait_for(lambda: not window._database_load_active
                     and not window._database_detail_active, timeout=30)
            service = window._trusted_read_service
            window.openPlot(guid=first_guid)
            wait_for(lambda: len(window.windows) == 1
                     and hasattr(window.windows[0], "axis_data")
                     and not window.windows[0].worker.running, timeout=30)
            original = window.windows[0]
            original.monitor.stop()
            window.autoPlotBox.setChecked(True)
            window.spinBox.setValue(.1)
            assert window.monitor.isActive()

            def progress_dialog():
                try:
                    dialog = qapplication.activeModalWidget()
                    if not isinstance(dialog, qtw.QMessageBox):
                        return
                    if "new_guid" not in state:
                        assert "plot" in dialog.text()
                        # Simulate the independent QCoDeS acquisition while the
                        # real confirmation's nested Qt loop keeps refreshing.
                        state["new_guid"] = _record_run(writer, experiment, database, 101.)
                        state["protected"] = _protected_artifacts(database)
                        return
                    late = next((plot for plot in window.windows if plot is not original), None)
                    if late is None or not hasattr(late, "axis_data") or late.worker.running:
                        return
                    assert late.ds.guid == state["new_guid"]
                    np.testing.assert_array_equal(late.axis_data["y"], [80., 94., 101., 115., 164.])
                    assert late.isVisible()
                    state["late"] = late
                    assert _protected_artifacts(database) == state["protected"]
                    poll.stop()
                    button = qtw.QMessageBox.StandardButton.Yes if accept else qtw.QMessageBox.StandardButton.No
                    dialog.button(button).click()
                except Exception as error:
                    state["callback_error"] = error
                    poll.stop()
                    dialog = qapplication.activeModalWidget()
                    if dialog is not None:
                        dialog.reject()

            def timed_out():
                state["timed_out"] = True
                dialog = qapplication.activeModalWidget()
                if dialog is not None:
                    dialog.reject()

            poll.setInterval(25)
            poll.timeout.connect(progress_dialog)
            poll.start()
            timeout.setSingleShot(True)
            timeout.timeout.connect(timed_out)
            timeout.start(30_000)
            (window.close_current_database if close_database else window.closeAll)()
            timeout.stop()
            poll.stop()
            assert not state.get("timed_out"), "Auto-plot did not finish during the modal dialog"
            if "callback_error" in state:
                raise state["callback_error"]
            late = state["late"]

            if accept:
                assert window.windows == []
                assert original._closed and late._closed
                assert not original.isVisible() and not late.isVisible()
                assert not original.monitor.isActive() and not late.monitor.isActive()
            else:
                assert window.windows == [original, late]
                assert not original._closed and not late._closed
                np.testing.assert_array_equal(original.axis_data["y"], [-8., 6., 13., 27., 76.])
                np.testing.assert_array_equal(late.axis_data["y"], [80., 94., 101., 115., 164.])
                assert original.ds.guid == first_guid
                assert late.ds.guid == state["new_guid"]

            if accept and close_database:
                assert window.fileTextbox.text() == ""
                assert window.dataset_holder == {}
                assert window._trusted_read_service is None
                wait_for(lambda: service.closed, timeout=30)
            else:
                assert Path(window.fileTextbox.text()).resolve() == database
                assert window._trusted_read_service is service
                assert not service.closing and not service.closed
            assert _protected_artifacts(database) == state["protected"]
            assert not callback_errors
        finally:
            poll.stop()
            timeout.stop()
            close_main_window(window)
            if "protected" in state:
                assert _protected_artifacts(database) == state["protected"]
