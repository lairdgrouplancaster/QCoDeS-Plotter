"""Information dialogs remain usable with larger text and keyboard dismissal."""

import pytest
from PyQt6 import QtCore, QtGui, QtTest, sip
from PyQt6 import QtWidgets as qtw

from qplot._update_presentation import CHECKING_MESSAGE
from qplot.windows import _help
from qplot.windows._database_actions import DatabaseInfoDialog
from qplot.windows._desktop_integration import DesktopIntegrationDialog
from qplot.windows._error_dialog import ErrorDialog
from qplot.windows._plot_marquee import new_statistics_dialog
from qplot.windows._updates import UpdateChecker
from qplot.windows._widgets.details_tables import TrustedFullValueDialog


@pytest.mark.parametrize("key", [QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Escape])
@pytest.mark.parametrize("points", [10, 18])
@pytest.mark.parametrize("kind", ["database", "statistics", "error", "desktop", "quick", "controls", "about", "updates", "metadata"])
def test_information_dialog_layout_and_keyboard(qapplication, monkeypatch, points, kind, key):
    original = qapplication.font()
    font = QtGui.QFont(original)
    font.setPointSize(points)
    qapplication.setFont(font)
    parent = qtw.QMainWindow()
    checker = UpdateChecker(qapplication, {"version": "1.0"})
    path = "C:/" + "long measurement directory/" * 30 + "data.db"
    actions = []
    # Inspection and keyboard checks must never run desktop setup or removal.
    monkeypatch.setattr(DesktopIntegrationDialog, "_start", actions.append)
    monkeypatch.setattr(_help, "show_desktop_integration", actions.append)
    monkeypatch.setattr(_help, "version_info", lambda: {"version": "1.0", "commit": "a" * 40})
    factories = {
        "database": lambda: DatabaseInfoDialog([("Path", path), ("Runs", "100")], parent),
        "statistics": lambda: new_statistics_dialog(parent, "100 points\nAverage: 2.5\nX range: 0 to 10", "Marquee statistics"),
        "error": lambda: ErrorDialog("Load failed", "Could not load the database.", path, parent),
        "desktop": lambda: DesktopIntegrationDialog(parent),
        "quick": lambda: _help.show_quick_start(parent),
        "controls": lambda: _help.show_keyboard_shortcuts(parent),
        "about": lambda: _help.show_about(parent),
        "updates": lambda: checker._new_dialog(parent, CHECKING_MESSAGE),
        "metadata": lambda: TrustedFullValueDialog(parent),
    }
    dialog = None
    try:
        parent.show()
        dialog = factories[kind]()
        dialog.show()
        dialog.activateWindow()
        qapplication.processEvents()
        buttons = dialog.findChild(qtw.QDialogButtonBox)
        close = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
        assert close.isDefault()
        assert dialog.focusWidget() is close
        assert all(not b.isDefault() for b in buttons.buttons() if b is not close)
        for button in buttons.buttons():
            if button.isVisible():
                assert button.width() >= button.sizeHint().width()
                assert button.height() >= button.sizeHint().height()
                assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
        table = dialog.findChild(qtw.QTableWidget)
        if table is not None:
            assert table.rowHeight(0) >= table.fontMetrics().height() + 2
            assert table.horizontalHeader().height() >= table.fontMetrics().height() + 2
            # Copying a full path/value must not depend on visible elision.
            table.selectRow(0)
            table.copySelection()
            assert (path if kind == "database" else "100 points") in qapplication.clipboard().text()
        text = dialog.findChild(qtw.QPlainTextEdit)
        if text is not None:
            assert text.tabChangesFocus()
            text.setFocus()
            QtTest.QTest.keyClick(text, QtCore.Qt.Key.Key_Tab)
            assert dialog.focusWidget() is not text
        if kind == "desktop":
            assert dialog.install_button.width() == dialog.remove_button.width()
            # Return in the location field closes, never installs/removes.
            dialog.destination.setFocus()
        else:
            close.setFocus()
        finished = []
        dialog.finished.connect(finished.append)
        QtTest.QTest.keyClick(dialog.focusWidget(), key)
        accepted = kind == "error" and key == QtCore.Qt.Key.Key_Return
        expected = qtw.QDialog.DialogCode.Accepted if accepted else qtw.QDialog.DialogCode.Rejected
        assert finished == [int(expected)]
        assert not actions
    finally:
        if dialog is not None and not sip.isdeleted(dialog):
            dialog.close()
        sip.delete(parent)
        sip.delete(checker)
        qapplication.setFont(original)


def test_error_close_accepts_but_escape_cancels(qapplication):
    for key, result in ((QtCore.Qt.Key.Key_Return, 1), (QtCore.Qt.Key.Key_Escape, 0)):
        dialog = ErrorDialog("Load failed", "Could not load.")
        finished = []
        dialog.finished.connect(finished.append)
        dialog.show()
        qapplication.processEvents()
        QtTest.QTest.keyClick(dialog, key)
        assert finished == [result]
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_return_on_focused_copy_keeps_dialog_open(qapplication):
    text = "100 points\nAverage: 2.5"
    dialog = new_statistics_dialog(None, text, "Marquee statistics")
    try:
        dialog.show()
        qapplication.processEvents()
        buttons = dialog.findChild(qtw.QDialogButtonBox)
        copy = next(b for b in buttons.buttons() if b.text() == "Copy")
        copy.setFocus()
        QtTest.QTest.keyClick(copy, QtCore.Qt.Key.Key_Return)
        assert dialog.isVisible()
        assert qapplication.clipboard().text() == text
    finally:
        sip.delete(dialog)


def test_metadata_keyboard_copy_and_reopening_preserve_exact_text(qapplication):
    dialog = TrustedFullValueDialog()
    value = '<tag>\n  exact value\t\u03bcV\n'
    try:
        dialog.show_value(path="metadata.<b>literal</b>", text=value, utf8_bytes=len(value.encode()))
        qapplication.processEvents()
        assert dialog.path_label.textFormat() == QtCore.Qt.TextFormat.PlainText
        assert dialog.focusWidget() is dialog.close_button
        dialog.text_edit.setFocus()
        QtTest.QTest.keyClick(dialog.text_edit, QtCore.Qt.Key.Key_Tab)
        assert dialog.focusWidget() is not dialog.text_edit
        dialog.copy_button.setFocus()
        QtTest.QTest.keyClick(dialog.copy_button, QtCore.Qt.Key.Key_Return)
        assert qapplication.clipboard().text() == value
        assert dialog.isVisible()
        QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
        assert not dialog.isVisible()
        dialog.show_value(path="next", text="replacement", utf8_bytes=11)
        qapplication.processEvents()
        assert dialog.focusWidget() is dialog.close_button
        QtTest.QTest.keyClick(dialog.close_button, QtCore.Qt.Key.Key_Return)
        assert not dialog.isVisible()
    finally:
        sip.delete(dialog)
