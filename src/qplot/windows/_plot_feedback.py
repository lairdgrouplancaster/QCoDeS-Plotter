from typing import TYPE_CHECKING, Any

from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QKeySequence

from qplot.diagnostics import log_user_error

from ._commands import CommandSpec, configure_action

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
            shortcut: _Shortcut | CommandSpec,
            status_tip: str | None = None,
            ) -> None:
        """
        Registers a QAction shortcut on the plot window.

        """
        if isinstance(shortcut, CommandSpec):
            configure_action(action, shortcut, add_to=self)
            return

        configure_action(
            action,
            CommandSpec(
                "",
                action.text(),
                status_tip or "",
                shortcut,
                ),
            add_to=self,
            )
