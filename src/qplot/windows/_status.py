"""Persistent status labels shared by run and plot windows."""

from datetime import datetime

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


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
