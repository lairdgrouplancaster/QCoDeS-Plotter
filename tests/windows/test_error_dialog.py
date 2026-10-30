"""Error details stay readable and copyable as the dialog is resized."""

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows import main as main_window
from qplot.windows._error_dialog import ErrorDialog
from qplot.windows._plot_feedback import PlotWindowFeedbackMixin


@pytest.mark.parametrize(
    "details",
    [None, "Cannot publish /a long path/database.db:\n" + "Full error details. " * 100],
)
def test_error_dialog_can_resize_and_copy_complete_error(details):
    dialog = ErrorDialog(
        "Generation Failed", "Could not generate the test database.", details
    )
    try:
        dialog.show()
        qtw.QApplication.processEvents()
        before = dialog.size()
        text = dialog.findChild(qtw.QPlainTextEdit, "errorDetails")
        old_text_size = text.size() if text is not None else None
        dialog.resize(before.width() + 200, before.height() + 180)
        qtw.QApplication.processEvents()
        assert dialog.width() > before.width()
        assert dialog.height() > before.height()
        assert dialog.isSizeGripEnabled()
        assert dialog.maximumWidth() > dialog.width()
        assert dialog.maximumHeight() > dialog.height()
        if details:
            assert text.isVisible() and text.isReadOnly()
            assert text.toPlainText() == details
            assert text.width() > old_text_size.width()
            assert text.height() > old_text_size.height()
        else:
            assert text is None
        dialog.findChild(qtw.QPushButton, "copyErrorButton").click()
        expected = "\n\n".join(
            value
            for value in (
                "Generation Failed",
                "Could not generate the test database.",
                details,
            )
            if value
        )
        assert qtw.QApplication.clipboard().text() == expected
    finally:
        dialog.close()
        qtw.QApplication.processEvents()


@pytest.mark.parametrize("kind", ["main", "plot", "hidden-plot"])
def test_error_feedback_uses_resizable_dialog_and_preserves_status(monkeypatch, kind):
    if kind == "main":

        class Host(qtw.QMainWindow):
            show_error = main_window.MainWindow.show_error
    else:

        class Host(PlotWindowFeedbackMixin, qtw.QMainWindow):
            pass

    host = Host()
    host.visible = kind != "hidden-plot"
    statuses, dialogs = [], []
    host.show_status = lambda *args: statuses.append(args)

    def capture(dialog):
        dialogs.append(
            (dialog.windowTitle(), dialog.findChild(qtw.QPlainTextEdit).toPlainText())
        )
        return qtw.QDialog.DialogCode.Accepted

    monkeypatch.setattr(ErrorDialog, "exec", capture)
    try:
        host.show_error("Generation Failed", "Could not generate.", "Complete details")
        assert statuses == [("Could not generate.", 10_000)]
        assert dialogs == (
            [] if kind == "hidden-plot" else [("Generation Failed", "Complete details")]
        )
    finally:
        host.close()
        host.deleteLater()
        qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
