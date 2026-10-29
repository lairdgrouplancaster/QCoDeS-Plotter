"""Opening a copy menu must preserve an existing table selection."""

import traceback

import pytest
from PyQt6 import QtCore, QtTest, sip
from PyQt6 import QtWidgets as qtw

from qplot.windows._widgets.details_tables import CopyableTableWidget
from tests.windows.test_details_copy_menu_refresh import (
    details_window as details_window,
)


def _choose_menu_action(widget, position, command, application, owned):
    state = {}
    timeout = QtCore.QTimer()
    timeout.setSingleShot(True)

    def activate():
        try:
            menu = application.activePopupWidget()
            assert isinstance(menu, qtw.QMenu)
            owned.append(menu)
            if command == "cancel":
                QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Escape)
            else:
                action = next(action for action in menu.actions() if action.text() == command)
                QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                    pos=menu.actionGeometry(action).center())
        except BaseException:
            state["error"] = traceback.format_exc()
            menu = application.activePopupWidget()
            if menu is not None:
                menu.close()

    def expire():
        state["timeout"] = True
        menu = application.activePopupWidget()
        if menu is not None:
            menu.close()

    timeout.timeout.connect(expire)
    timeout.start(20_000)
    QtCore.QTimer.singleShot(0, activate)
    try:
        widget.openCopyMenu(position)
    finally:
        timeout.stop()
    assert not state.get("timeout")
    assert "error" not in state, state.get("error")
    application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert all(sip.isdeleted(menu) for menu in owned)


@pytest.mark.parametrize("target", ["first", "second", "outside"])
@pytest.mark.parametrize("command", ["Copy Selection", "Copy Cell", "cancel"])
def test_overview_context_menu_preserves_selected_rows(
    details_window, qapplication, target, command,
):
    window, dataset, _database, state = details_window
    table = window.infoBox.overview
    window.infoBox.setCurrentWidget(table)
    window.activateWindow()
    table.setFocus()
    qapplication.processEvents()
    fields = {table.item(row, 0).text(): row for row in range(table.rowCount())}
    first, second, outside = (fields[name] for name in ("Sample", "GUID", "Name"))
    table.scrollToItem(table.item(second, 1))
    qapplication.processEvents()
    QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
        pos=table.visualItemRect(table.item(first, 1)).center())
    QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.ControlModifier,
        pos=table.visualItemRect(table.item(second, 1)).center())
    assert {item.row() for item in table.selectedItems()} == {first, second}
    expected_selection = f"Sample\tunicode\nGUID\t{dataset.guid}"
    QtTest.QTest.keySequence(table, table.copy_selection_action.shortcut())
    assert qapplication.clipboard().text() == expected_selection
    QtTest.QTest.keyRelease(table, QtCore.Qt.Key.Key_Control)
    QtTest.QTest.keyRelease(table, QtCore.Qt.Key.Key_Shift)
    assert not window.monitor.isActive()

    clicked_row = {"first": first, "second": second, "outside": outside}[target]
    clicked = table.item(clicked_row, 1)
    _choose_menu_action(table, table.visualItemRect(clicked).center(), command,
                        qapplication, state["objects"])

    expected_rows = {outside} if target == "outside" else {first, second}
    assert {item.row() for item in table.selectedItems()} == expected_rows
    if command == "Copy Cell":
        expected = {"first": "unicode", "second": dataset.guid,
                    "outside": "Run α & <sample> 🧪"}[target]
    elif command == "cancel" or target != "outside":
        expected = expected_selection
    else:
        expected = "Name\tRun α & <sample> 🧪"
    assert qapplication.clipboard().text() == expected
    # With the popup gone, ordinary shortcut actions follow the new current
    # selection instead of retaining the completed menu's copied text.
    table.clearSelection()
    table.setCurrentItem(table.item(outside, 1))
    table.setRangeSelected(qtw.QTableWidgetSelectionRange(outside, 0, outside, 1), True)
    assert len(table.selectedItems()) == 2
    window.activateWindow()
    table.setFocus()
    qapplication.processEvents()
    QtTest.QTest.keySequence(table, table.copy_selection_action.shortcut())
    QtTest.QTest.keyRelease(table, QtCore.Qt.Key.Key_Control)
    QtTest.QTest.keyRelease(table, QtCore.Qt.Key.Key_Shift)
    assert qapplication.clipboard().text() == "Name\tRun α & <sample> 🧪"


def test_copy_menu_preserves_multicell_tsv_and_exact_unicode(qapplication):
    table = CopyableTableWidget()
    table.setRowCount(2)
    table.setColumnCount(3)
    values = [["α\tβ", "line one\nline two", ""], ["温度 🧪", "<tag>&", "final"]]
    for row, cells in enumerate(values):
        for column, value in enumerate(cells):
            table.setItem(row, column, qtw.QTableWidgetItem(value))
    table.show()
    table.activateWindow()
    table.setFocus()
    qapplication.processEvents()
    table.setCurrentItem(table.item(0, 0))
    table.setRangeSelected(qtw.QTableWidgetSelectionRange(0, 0, 1, 2), True)
    expected = "α\tβ\tline one\nline two\t\n温度 🧪\t<tag>&\tfinal"
    QtTest.QTest.keySequence(table, table.copy_selection_action.shortcut())
    assert qapplication.clipboard().text() == expected
    QtTest.QTest.keyRelease(table, QtCore.Qt.Key.Key_Control)
    QtTest.QTest.keyRelease(table, QtCore.Qt.Key.Key_Shift)
    owned = []
    try:
        _choose_menu_action(table, table.visualItemRect(table.item(1, 2)).center(),
                            "Copy Selection", qapplication, owned)
        assert len(table.selectedItems()) == 6
        assert qapplication.clipboard().text() == expected
    finally:
        table.close()
        table.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert sip.isdeleted(table)
    assert all(sip.isdeleted(menu) for menu in owned)
