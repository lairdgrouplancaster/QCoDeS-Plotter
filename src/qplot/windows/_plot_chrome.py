"""Native plot-window titles with compact, permanent status feedback."""

import sys
from typing import cast

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._plot_status import PlotStatusWidget


class _MenuStatus(QtCore.QObject):
    """Reserve the menu titles before allocating space to status/progress."""

    def __init__(self, window: qtw.QMainWindow, status: PlotStatusWidget):
        super().__init__(window)
        self.menu = cast(qtw.QMenuBar, window.menuBar())
        self.status = status
        self.menu.setCornerWidget(status, QtCore.Qt.Corner.TopRightCorner)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._layout)
        self.menu.installEventFilter(self)
        status.show()
        self._layout()

    def eventFilter(self, source, event):
        if event.type() in (
            QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show,
            QtCore.QEvent.Type.LayoutRequest, QtCore.QEvent.Type.ActionAdded,
            QtCore.QEvent.Type.ActionRemoved, QtCore.QEvent.Type.FontChange,
            QtCore.QEvent.Type.StyleChange,
        ):
            self.timer.start(0)
        return False

    def _layout(self):
        menu_right = max(
            (self.menu.actionGeometry(action).right() + 1
             for action in self.menu.actions() if action.isVisible()),
            default=0,
        )
        available = max(0, self.menu.width() - menu_right - 16)
        width = min(320, available)
        if width != self.status.sizeHint().width():
            self.status.set_available_width(width)
            self.menu.setCornerWidget(self.status, QtCore.Qt.Corner.TopRightCorner)


class _MacTitleArea(qtw.QWidget):
    """Place status beside the native title while preserving native controls."""

    def __init__(self, window: qtw.QMainWindow, status: PlotStatusWidget):
        super().__init__(window)
        self.window_owner = window
        self.status = status
        self.setObjectName("plotTitleArea")
        status.setParent(self)
        self._sync_palette()
        window.setWindowFlag(QtCore.Qt.WindowType.ExpandedClientAreaHint)
        window.setWindowFlag(QtCore.Qt.WindowType.NoTitleBarBackgroundHint)
        # Reserve the title area's height ourselves, leaving the overlay free
        # to occupy it. The native title and traffic lights remain OS-owned.
        window.setAttribute(QtCore.Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        window.winId()
        handle = window.windowHandle()
        if handle is not None:
            handle.safeAreaMarginsChanged.connect(self._layout)
        window.windowTitleChanged.connect(self._layout)
        window.installEventFilter(self)
        self._layout()
        self.show()
        status.show()

    def eventFilter(self, source, event):
        if event.type() in (
            QtCore.QEvent.Type.ApplicationPaletteChange, QtCore.QEvent.Type.StyleChange,
        ):
            self._sync_palette()
        if event.type() in (
            QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show,
            QtCore.QEvent.Type.WindowStateChange, QtCore.QEvent.Type.FontChange,
            QtCore.QEvent.Type.StyleChange,
        ):
            self._layout()
        return False

    def _sync_palette(self):
        # AppKit draws the title using the system appearance, independently of
        # qPlot's stylesheet theme. Match its background and adjacent text.
        palette = qtw.QApplication.palette()
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        colour = palette.windowText().color().name()
        for label in (self.status.elapsed_label, self.status.stage_label):
            label.setStyleSheet(f"color: {colour};")
        status_colour = "#01664d" if palette.window().color().lightness() >= 128 else "#16a085"
        self.status.status_label.setStyleSheet(f"color: {status_colour};")

    def _layout(self, *_args):
        window = self.window_owner
        handle = window.windowHandle()
        safe_top = handle.safeAreaMargins().top() if handle is not None else 0
        height = max(safe_top, self.fontMetrics().height() + 10, 28)
        margins = QtCore.QMargins(0, height, 0, 0)
        if window.contentsMargins() != margins:
            window.setContentsMargins(margins)
        self.setGeometry(0, 0, window.width(), height)
        title_half = window.fontMetrics().horizontalAdvance(window.windowTitle()) // 2
        available = max(0, window.width() // 2 - title_half - 24)
        width = min(320, available)
        self.status.setGeometry(window.width() - width, 0, width, height)
        self.raise_()

    def mousePressEvent(self, event: QtGui.QMouseEvent | None):
        if event is not None and event.button() == QtCore.Qt.MouseButton.LeftButton:
            handle = self.window_owner.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent | None):
        if event is not None and event.button() == QtCore.Qt.MouseButton.LeftButton:
            from qplot._macos_window import titlebar_double_click

            titlebar_double_click(self.window_owner)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def install_plot_chrome(window: qtw.QMainWindow, status: PlotStatusWidget) -> None:
    """Keep macOS menus in the system bar; use the in-window bar elsewhere."""
    window.__dict__["_plot_chrome"] = (
        _MacTitleArea(window, status) if sys.platform == "darwin"
        else _MenuStatus(window, status)
    )
