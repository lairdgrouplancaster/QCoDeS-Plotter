from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QKeySequence

from qplot.diagnostics import log_user_error


class PlotWindowFeedbackMixin:
    """
    Status-bar, overlay, error-dialog, and shortcut helpers for plot windows.

    """

    def show_status(self, message: str, timeout: int = 5000):
        """
        Shows a short message in the plot window status bar.

        """
        if getattr(self, "visible", False):
            self.statusBar().showMessage(message, timeout)


    def show_plot_state(self, title, detail=None, kind="info"):
        """
        Shows a prominent state message inside the plot area.

        """
        overlay = self.__dict__.get("plot_state_overlay")
        if overlay is not None:
            overlay.show(title, detail=detail, kind=kind)


    def hide_plot_state(self):
        """
        Hides the plot-area state message when renderable data is available.

        """
        overlay = self.__dict__.get("plot_state_overlay")
        if overlay is not None:
            overlay.hide()


    def show_error(self, title: str, message: str, details: str = None):
        """
        Shows an error both in the status bar and, for visible windows, in a
        message box.

        """
        log_user_error(title, message, details, __name__)
        self.show_status(message, 10000)

        if not self.visible:
            return

        box = qtw.QMessageBox(qtw.QMessageBox.Icon.Warning, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec()


    def register_shortcut(self, action, shortcut, status_tip: str = None):
        """
        Registers a QAction shortcut on the plot window.

        """
        if isinstance(shortcut, (list, tuple)):
            action.setShortcuts(shortcut)
            shortcut_text = shortcut[0].toString(QKeySequence.SequenceFormat.NativeText)
        else:
            action.setShortcut(shortcut)
            shortcut_text = QKeySequence(shortcut).toString(
                QKeySequence.SequenceFormat.NativeText
                )
        action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        if hasattr(action, "setShortcutVisibleInContextMenu"):
            action.setShortcutVisibleInContextMenu(True)
        if status_tip:
            action.setStatusTip(status_tip)
            action.setToolTip(f"{status_tip} ({shortcut_text})")
        if action not in self.actions():
            self.addAction(action)
