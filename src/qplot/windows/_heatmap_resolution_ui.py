"""Qt controls for an already accepted heatmap resolution report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.tools.heatmap_resolution import (
    HeatmapResolutionReport,
    resolution_details,
    resolution_summary,
)
from qplot.windows._small_dialogs import close_default

if TYPE_CHECKING:
    from .plot2d import plot2d


class HeatmapResolutionUi(QtCore.QObject):
    def __init__(self, window: plot2d) -> None:
        super().__init__(window)
        self.window = window
        self._report: HeatmapResolutionReport | None = None
        self._revision = object()
        self._dialog: qtw.QDialog | None = None
        viewport = window.widget.viewport() if hasattr(window.widget, "viewport") else None
        self._parent = viewport if isinstance(viewport, qtw.QWidget) else window.widget
        self._parent.installEventFilter(self)
        self.button = qtw.QToolButton(self._parent)
        self.button.setObjectName("heatmapDownsampleWarningButton")
        style = window.style()
        if style is not None:
            self.button.setIcon(style.standardIcon(qtw.QStyle.StandardPixmap.SP_MessageBoxWarning))
        self.button.setIconSize(QtCore.QSize(18, 18))
        self.button.setAutoRaise(False)
        self.button.setFixedSize(28, 28)
        self.button.setText("")
        self.button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.button.setToolTip("This heatmap is downsampled. Click for details.")
        self.button.setAccessibleName("Show heatmap resolution")
        self.button.clicked.connect(lambda _checked=False: window.show_heatmap_downsample_dialog())
        self.button.hide()

    def _position(self) -> None:
        margin = 8
        self.button.move(max(margin, self._parent.width() - self.button.width() - margin),
                         max(margin, self._parent.height() - self.button.height() - margin))
        self.button.raise_()

    def eventFilter(self, source: QtCore.QObject | None, event: QtCore.QEvent | None) -> bool:
        if source is self._parent and event is not None and event.type() == QtCore.QEvent.Type.Resize:
            self._position()
        return super().eventFilter(source, event)

    def sync(self, report: HeatmapResolutionReport | None) -> None:
        self._report = report
        revision = self._revision = object()
        self.button.setVisible(report is not None)
        if self._revision is not revision:
            return
        if report is not None:
            self._position()
        if self._revision is not revision:
            return
        dialog = self._dialog
        if dialog is None:
            return
        if report is None:
            dialog.reject()
            return
        summary = dialog.findChild(qtw.QLabel, "heatmapResolutionSummary")
        details = dialog.findChild(qtw.QPlainTextEdit, "heatmapResolutionDetails")
        if summary is not None:
            summary.setText(resolution_summary(report))
        if self._revision is not revision or self._dialog is not dialog:
            return
        if details is not None:
            details.setPlainText(resolution_details(report))

    def show(self) -> None:
        if self._report is None:
            return
        dialog = self._dialog
        if dialog is None:
            dialog = self.new_dialog()
            dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _forget_dialog(self, dialog: qtw.QDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None

    def new_dialog(self) -> qtw.QDialog:
        """Create a titled warning dialog for a downsampled heatmap."""
        report = self._report
        if report is None:
            raise RuntimeError("No displayed heatmap resolution report.")
        dialog = qtw.QDialog(self.window)
        self._dialog = dialog
        dialog.finished.connect(lambda: self._forget_dialog(dialog))
        dialog.destroyed.connect(lambda: self._forget_dialog(dialog))
        dialog.setWindowTitle("Heatmap displayed at reduced resolution")

        layout = qtw.QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        content = qtw.QHBoxLayout()
        content.setSpacing(12)
        icon_label = qtw.QLabel(dialog)
        style = dialog.style()
        assert style is not None
        icon = style.standardIcon(
            qtw.QStyle.StandardPixmap.SP_MessageBoxWarning
            )
        icon_size = style.pixelMetric(qtw.QStyle.PixelMetric.PM_MessageBoxIconSize)
        icon_label.setPixmap(icon.pixmap(icon_size, icon_size))
        icon_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        content.addWidget(icon_label)

        message_label = qtw.QLabel(resolution_summary(report), dialog)
        message_label.setObjectName("heatmapResolutionSummary")
        message_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        content.addWidget(message_label, 1)
        layout.addLayout(content)

        details_button = qtw.QToolButton(dialog)
        details_button.setObjectName("heatmapResolutionDetailsButton")
        details_button.setText("Details")
        details_button.setCheckable(True)
        details_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        details_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        layout.addWidget(details_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        details = qtw.QPlainTextEdit(resolution_details(report), dialog)
        details.setObjectName("heatmapResolutionDetails")
        details.setAccessibleName("Heatmap resolution details")
        details.setReadOnly(True)
        details.setFixedHeight(details.fontMetrics().lineSpacing() * 10)
        details.hide()
        layout.addWidget(details)

        def toggle_details(expanded: bool) -> None:
            details.setVisible(expanded)
            details_button.setArrowType(
                QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
                )
            layout.activate()
            dialog.resize(dialog.width(), dialog.sizeHint().height())

        details_button.toggled.connect(toggle_details)

        button_box = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Close,
            dialog,
            )
        preferences_button = qtw.QPushButton("Adjust heatmap limits…", dialog)
        button_box.addButton(
            preferences_button, qtw.QDialogButtonBox.ButtonRole.ActionRole
            )
        preferences_button.setObjectName("openHeatmapLimitPreferencesButton")
        preferences_button.clicked.connect(
            lambda: self._open_preferences(dialog)
            )
        button_box.rejected.connect(dialog.close)
        close_default(button_box)
        layout.addWidget(button_box)
        dialog.resize(520, dialog.sizeHint().height())
        return dialog
    def _open_preferences(self, dialog: qtw.QDialog) -> None:
        """Close the alert, then open the preference that controls this limit."""
        dialog.accept()
        QtCore.QTimer.singleShot(
            0,
            lambda: self.window.show_preferences_dialog(initial_tab="Performance"),
            )

