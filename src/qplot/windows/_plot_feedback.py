from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QKeySequence

from qplot.diagnostics import log_user_error

if TYPE_CHECKING:
    class _PlotWindowFeedbackBase(qtw.QMainWindow):
        visible: bool
else:
    class _PlotWindowFeedbackBase:
        pass


_Shortcut = str | QKeySequence | list[QKeySequence] | tuple[QKeySequence, ...]


class PlotWindowFeedbackMixin(_PlotWindowFeedbackBase):
    """
    Status-bar, overlay, error-dialog, and shortcut helpers for plot windows.

    """

    def show_status(self, message: str, timeout: int = 5000) -> None:
        """
        Shows a short message in the plot window status bar.

        """
        if self.visible:
            status_bar = self.statusBar()
            if status_bar is not None:
                status_bar.showMessage(message, timeout)


    def show_plot_state(
            self,
            title: object,
            detail: object | None = None,
            kind: str = "info",
            ) -> None:
        """
        Shows a prominent state message inside the plot area.

        """
        overlay: Any | None = self.__dict__.get("plot_state_overlay")
        if overlay is not None:
            overlay.show(title, detail=detail, kind=kind)


    def hide_plot_state(self) -> None:
        """
        Hides the plot-area state message when renderable data is available.

        """
        overlay: Any | None = self.__dict__.get("plot_state_overlay")
        if overlay is not None:
            overlay.hide()


    def show_error(
            self,
            title: str,
            message: str,
            details: str | None = None,
            ) -> None:
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


    def register_shortcut(
            self,
            action: QtGui.QAction,
            shortcut: _Shortcut,
            status_tip: str | None = None,
            ) -> None:
        """
        Registers a QAction shortcut on the plot window.

        """
        if isinstance(shortcut, (list, tuple)):
            action.setShortcuts(list(shortcut))
            if shortcut:
                shortcut_text = shortcut[0].toString(
                    QKeySequence.SequenceFormat.NativeText
                    )
            else:
                shortcut_text = ""
        else:
            action.setShortcut(shortcut)
            if isinstance(shortcut, QKeySequence):
                shortcut_text = shortcut.toString(
                    QKeySequence.SequenceFormat.NativeText
                    )
            else:
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
