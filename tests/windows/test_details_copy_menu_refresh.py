"""Detail menu copies remain valid while live refresh replaces their Qt items."""

import hashlib
import sys
import traceback
from contextlib import closing
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtTest, sip
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter
from qcodes.station import Station

from qplot.diagnostics import configure_logging
from qplot.windows._widgets.details_tables import CopyableTableWidget, infoTree
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows._trusted_derived_wal_ui import _tree_item_for_path
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def _protected_artifacts(database):
    artifacts = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            artifacts[suffix] = None
            continue
        stat = path.stat()
        artifacts[suffix] = (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return artifacts


@pytest.fixture
def details_window(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    database = tmp_path / "menu-refresh.db"
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    with closing(connect(str(database))) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        experiment = load_or_create_experiment("live copy", "unicode", conn=writer)
        note = ManualParameter("operator", initial_value="Snapshot α & <sample> 🧪")
        station = Station(note)
        measurement = Measurement(exp=experiment, station=station, name="Run α & <sample> 🧪")
        measurement.register_custom_parameter("drive")
        measurement.register_custom_parameter("signal", setpoints=("drive",))
        with measurement.run(write_in_background=False) as saver:
            for drive in (-3., 0., 4.):
                saver.add_result(("drive", drive), ("signal", 7 * drive + 1))
            saver.dataset.add_metadata("operator_note", "Metadata α & <sample> 🧪")
        state = {"protected": _protected_artifacts(database), "objects": []}
        window = MainWindow()
        service = None
        monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
        try:
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            window.spinBox.setValue(0)
            window.show()
            assert window.load_file(str(database))
            wait_for(lambda: not window._database_load_active and not window._database_detail_active)
            service = window._trusted_read_service
            assert service is not None
            window.RunList.setCurrentItem(window.RunList._item_for_guid(saver.dataset.guid))
            bridge = window._trusted_derived_bridge
            wait_for(lambda: bridge.coordinator is not None
                     and not bridge.coordinator.active
                     and not bridge.coordinator.snapshot().pending_count
                     and not bridge.background_active()
                     and bridge._selected_detail_publication is not None
                     and bridge._detail_display_guid == saver.dataset.guid)
            yield window, saver.dataset, database, state
        finally:
            close_main_window(window)
            assert service is None or service.closed
            assert _protected_artifacts(database) == state["protected"]
            assert all(sip.isdeleted(obj) for obj in state["objects"])
            assert errors == []


def _row(window, section):
    tree = getattr(window.infoBox, section)
    window.infoBox.setCurrentWidget(tree)
    if section == "overview":
        row = next(row for row in range(tree.rowCount()) if tree.item(row, 0).text() == "Name")
        item = tree.item(row, 1)
        tree.scrollToItem(item)
        qtw.QApplication.processEvents()
        return tree, item, "Name\tRun α & <sample> 🧪"
    if section == "metadata":
        path = "/Metadata/operator_note"
        expected = "operator_note\tMetadata α & <sample> 🧪"
    elif section == "raw":
        path = "/Raw/Run/name"
        expected = "name\tRun α & <sample> 🧪"
    else:
        for parent_path in (
            "/Snapshot/station", "/Snapshot/station/parameters",
            "/Snapshot/station/parameters/operator",
        ):
            parent = _tree_item_for_path(tree, parent_path)
            parent.setExpanded(True)
            wait_for(lambda parent=parent: parent.childCount() > 0)
        path = "/Snapshot/station/parameters/operator/value"
        expected = "value\tSnapshot α & <sample> 🧪"
    item = _tree_item_for_path(tree, path)
    assert f"{item.text(0)}\t{item.text(1)}" == expected
    tree.scrollToItem(item)
    qtw.QApplication.processEvents()
    return tree, item, expected


@pytest.mark.parametrize("section,copy_action,action", [
    pytest.param(section, label, action, id=f"{section}-{identifier}-{action}")
    for section in ("metadata", "raw", "snapshot", "overview")
    for identifier, label in (
        (("cell", "Copy Cell"), ("selection", "Copy Selection"))
        if section == "overview" else
        (("row", "Copy Row"), ("value", "Copy Value"), ("selection", "Copy Selection"))
    )
    for action in ("live_copy", "copy", "live_cancel", "live_key")
    if identifier != "row" or action != "live_key"
])
def test_menu_copies_capture_clicked_content_across_live_refresh(
    details_window, qapplication, section, action, copy_action,
):
    window, dataset, database, fixture_state = details_window
    tree, item, expected = _row(window, section)
    if copy_action in ("Copy Value", "Copy Cell"):
        expected = expected.split("\t", 1)[1]
    window.activateWindow()
    tree.setFocus()
    qapplication.processEvents()
    qapplication.clipboard().setText("untouched clipboard")
    state = {}
    poll = QtCore.QTimer()
    deadline = QtCore.QTimer()
    poll.setInterval(20)
    deadline.setSingleShot(True)
    if action.startswith("live_"):
        window.spinBox.setValue(.1)
        assert window.monitor.isActive()

    def progress():
        try:
            menu = qapplication.activePopupWidget()
            if not isinstance(menu, qtw.QMenu):
                return
            if "menu" not in state:
                state["menu"] = menu
                fixture_state["objects"].append(menu)
                state["copy"] = next(a for a in menu.actions() if a.text() == copy_action)
                if action.startswith("live_"):
                    # Only the fixture's QCoDeS owner writes. The real monitor
                    # refreshes inside QMenu.exec and replaces the old items.
                    dataset.add_metadata("operator_note", "Updated β & <sample> 🧪")
                    fixture_state["protected"] = _protected_artifacts(database)
                    return
            if action.startswith("live_") and not sip.isdeleted(item):
                return
            state["item_deleted"] = sip.isdeleted(item)
            assert _protected_artifacts(database) == fixture_state["protected"]
            poll.stop()
            if action == "live_cancel":
                QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Escape)
            elif action == "live_key":
                QtTest.QTest.keySequence(menu, state["copy"].shortcut())
                QtTest.QTest.keyRelease(menu, QtCore.Qt.Key.Key_Control)
                QtTest.QTest.keyRelease(menu, QtCore.Qt.Key.Key_Shift)
                menu.close()
            else:
                QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                    pos=menu.actionGeometry(state["copy"]).center())
        except BaseException:
            state["callback_error"] = traceback.format_exc()
            poll.stop()
            menu = qapplication.activePopupWidget()
            if menu is not None:
                menu.close()

    def expire():
        state["timed_out"] = True
        menu = qapplication.activePopupWidget()
        if menu is not None:
            menu.close()

    poll.timeout.connect(progress)
    deadline.timeout.connect(expire)
    poll.start()
    deadline.start(20_000)
    try:
        tree.openCopyMenu(tree.visualItemRect(item).center())
    finally:
        poll.stop()
        deadline.stop()
        window.monitor.stop()
    assert not state.get("timed_out"), "Live metadata refresh did not replace the menu's row"
    assert "callback_error" not in state, state.get("callback_error")
    assert state["item_deleted"] is action.startswith("live_")
    assert qapplication.clipboard().text() == (
        "untouched clipboard" if action == "live_cancel" else expected
    )
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert sip.isdeleted(state["menu"])


@pytest.mark.parametrize("widget_class", [infoTree, CopyableTableWidget])
def test_copy_menu_cleanup_handles_owner_deletion(qapplication, widget_class):
    tree = widget_class()
    if isinstance(tree, infoTree):
        item = qtw.QTreeWidgetItem(["key", "value α 🧪"])
        tree.addTopLevelItem(item)
    else:
        tree.setRowCount(1)
        tree.setColumnCount(1)
        item = qtw.QTableWidgetItem("value α 🧪")
        tree.setItem(0, 0, item)
    tree.show()
    qapplication.processEvents()
    state = {}
    watchdog = QtCore.QTimer()
    watchdog.setSingleShot(True)

    def delete_owner():
        menu = qapplication.activePopupWidget()
        state["menu"] = menu
        tree.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def expire():
        state["timed_out"] = True
        menu = qapplication.activePopupWidget()
        if menu is not None:
            menu.close()

    watchdog.timeout.connect(expire)
    watchdog.start(20_000)
    QtCore.QTimer.singleShot(0, delete_owner)
    try:
        tree.openCopyMenu(tree.visualItemRect(item).center())
    finally:
        watchdog.stop()
        if not sip.isdeleted(tree):
            tree.deleteLater()
            qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert not state.get("timed_out")
    assert isinstance(state["menu"], qtw.QMenu)
    assert sip.isdeleted(state["menu"])
    assert sip.isdeleted(tree)
