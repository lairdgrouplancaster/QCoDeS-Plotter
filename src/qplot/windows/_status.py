"""Persistent status labels shared by run and plot windows."""

from datetime import datetime

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


class RecentDatabaseNotice(qtw.QWidget):
    """Show a history-save failure alongside the database until it is resolved or dismissed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = qtw.QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        summary = qtw.QLabel("Database opened. qPlot couldn’t save the recent-file list for next time.")
        summary.setWordWrap(True)
        layout.addWidget(summary, 0, 0)
        layout.setColumnStretch(0, 1)

        self.details_button = qtw.QPushButton("Details")
        self.details_button.setCheckable(True)
        layout.addWidget(self.details_button, 0, 1)
        dismiss = qtw.QPushButton("Dismiss")
        dismiss.clicked.connect(self.hide)
        layout.addWidget(dismiss, 0, 2)

        self.details = qtw.QLabel()
        self.details.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.details.setWordWrap(True)
        layout.addWidget(self.details, 1, 0, 1, 3)
        self.details_button.toggled.connect(self.details.setVisible)
        self.details.hide()
        self.hide()

    def report_failure(self, filename: str, error: Exception) -> None:
        first_failure = not self.details.text()
        reason = str(error)
        if isinstance(error, OSError) and (code := getattr(error, "winerror", None)) is not None:
            reason = f"[WinError {code}] {error.strerror}"
        self.details.setText(f"Settings file: {filename}\n{reason}")
        if first_failure:
            self.show()

    def clear(self) -> None:
        self.hide()
        self.details_button.setChecked(False)
        self.details.clear()


class StatusLabel(qtw.QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.setSizePolicy(
            qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred
        )
        self._tooltip_position = None

    def show_message(self, message: str) -> None:
        self.setText(message)
        if message:
            updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            self.setToolTip(f"{message}\nLast updated: {updated_at}")
        else:
            self.setToolTip("")

    def event(self, event):
        if event.type() == QtCore.QEvent.Type.ToolTip:
            self._tooltip_position = event.globalPos()
            qtw.QToolTip.showText(
                self._tooltip_position, self.toolTip(), self, self.rect()
            )
            event.accept()
            return True
        if event.type() == QtCore.QEvent.Type.ToolTipChange:
            # Qt caches the text of an open tooltip. Updating the property
            # alone leaves the old status visible until the next hover.
            if self._tooltip_position is not None and qtw.QToolTip.isVisible():
                qtw.QToolTip.showText(
                    self._tooltip_position, self.toolTip(), self, self.rect()
                )
        elif event.type() in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Hide):
            self._tooltip_position = None
        return super().event(event)

    def minimumSizeHint(self):
        return QtCore.QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        self.style().drawItemText(
            painter, self.contentsRect(),
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            self.palette(), self.isEnabled(),
            self.fontMetrics().elidedText(
                self.text(), QtCore.Qt.TextElideMode.ElideRight, self.contentsRect().width(),
            ),
            QtGui.QPalette.ColorRole.WindowText,
        )
