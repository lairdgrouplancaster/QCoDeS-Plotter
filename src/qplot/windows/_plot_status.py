"""Compact, persistent feedback in a plot's title area or menu bar."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from ._status import StatusLabel


class PlotStatusWidget(qtw.QWidget):
    """Use the available header space for a message or current-stage progress."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("plotToolbarStatus")
        self.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        self.message = ""
        self._progress_active = False
        self._preferred_width = 320

        self.status_label = StatusLabel(self)
        self.status_label.setObjectName("plotStatusMessage")
        self.status_label.setAccessibleName("Plot status")

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

        self.stack = qtw.QStackedLayout()
        # QStackedLayout fills its geometry without applying content margins.
        # An outer layout reserves real space beside both status views.
        layout = qtw.QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 16, 0)
        layout.addLayout(self.stack)
        # Long status text must not enlarge the toolbar or hide its controls.
        self.stack.setSizeConstraint(qtw.QLayout.SizeConstraint.SetNoConstraint)
        layout.setSizeConstraint(qtw.QLayout.SizeConstraint.SetNoConstraint)
        self.stack.addWidget(self.status_label)
        self.stack.addWidget(self.progress_panel)

    def minimumSizeHint(self):
        return QtCore.QSize(0, self.stack.minimumSize().height())

    def sizeHint(self):
        return QtCore.QSize(self._preferred_width, self.stack.sizeHint().height())

    def set_available_width(self, width: int) -> None:
        # QMenuBar positions corner widgets using sizeHint before constraining
        # their geometry. Keep both in agreement so menus cannot overlap.
        if width != self._preferred_width:
            self._preferred_width = width
            self.setFixedWidth(width)
            self.updateGeometry()

    def show_message(self, message: str) -> None:
        self.message = message
        self.status_label.show_message(message)
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
                # Restore the message with its original timestamp.
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
