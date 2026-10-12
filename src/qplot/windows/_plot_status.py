"""Compact, persistent feedback for the refresh toolbar."""

from datetime import datetime

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


class _StatusLabel(qtw.QLabel):
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


class PlotStatusWidget(qtw.QWidget):
    """Use the available toolbar space for a message or current-stage progress."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("plotToolbarStatus")
        self.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        self.message = ""
        self._progress_active = False

        self.status_label = _StatusLabel(self)
        self.status_label.setObjectName("plotStatusMessage")
        self.status_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status_label.setAccessibleName("Plot status")
        self.status_label.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred)

        self.progress_panel = qtw.QWidget(self)
        row = qtw.QHBoxLayout(self.progress_panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.progress_bar = qtw.QProgressBar(self.progress_panel)
        self.progress_bar.setAccessibleName("Plot loading progress for the current stage")
        self.progress_bar.setMinimumWidth(40)
        self.progress_bar.setMaximumWidth(240)
        self.progress_bar.setFormat("%p%")
        self.elapsed_label = qtw.QLabel(self.progress_panel)
        self.elapsed_label.setAccessibleName("Elapsed plot loading time")
        self.stage_label = qtw.QLabel(self.progress_panel)
        row.addStretch()
        row.addWidget(self.progress_bar, 1)
        row.addWidget(self.elapsed_label)
        row.addWidget(self.stage_label)

        self.stack = qtw.QStackedLayout(self)
        self.stack.setContentsMargins(12, 0, 4, 0)
        # Long status text must not enlarge the toolbar or hide its controls.
        self.stack.setSizeConstraint(qtw.QLayout.SizeConstraint.SetNoConstraint)
        self.stack.addWidget(self.status_label)
        self.stack.addWidget(self.progress_panel)

    def minimumSizeHint(self):
        return QtCore.QSize(0, self.stack.minimumSize().height())

    def sizeHint(self):
        return QtCore.QSize(320, self.stack.sizeHint().height())

    def show_message(self, message: str) -> None:
        self.message = message
        self.status_label.setText(message)
        if message:
            created_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            self.status_label.setToolTip(f"{message}\nCreated: {created_at}")
        else:
            self.status_label.setToolTip("")
        self.stack.setCurrentWidget(self.status_label)

    def start_progress(self) -> None:
        # Never restore an earlier operation's message when this load ends.
        self.message = ""
        self._progress_active = True
        self.show_progress()

    def show_progress(self) -> None:
        self.stack.setCurrentWidget(self.progress_panel)

    def finish_progress(self) -> None:
        if self._progress_active:
            self._progress_active = False
            if self.message:
                # Restore the message with its original creation timestamp.
                self.stack.setCurrentWidget(self.status_label)
            else:
                self.show_message("Plot load complete.")

    def set_progress_details(self, progress, context: str) -> None:
        self.stage_label.setText(f"Stage {progress.stage}/2")
        details = f"{context}\n{progress.title}\n"
        if progress.total is not None and progress.total > 0:
            details += f"{progress.completed:,} / {progress.total:,} ({progress.unit})\n"
        details += (
            "Progress is for the current stage. Scan progress covers the captured "
            "source range, including partial array records."
        )
        self.progress_panel.setToolTip(details)
        self.progress_bar.setAccessibleDescription(details)
