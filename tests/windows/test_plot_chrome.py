"""Status stays clear of menus and native title controls as windows resize."""

import pytest
from PyQt6 import QtCore, QtTest, QtWidgets

from qplot.configuration.themes import dark, light
from qplot.windows._plot_chrome import _MacTitleArea, _MenuStatus
from qplot.windows._plot_status import PlotStatusWidget


@pytest.mark.parametrize("width", [460, 800, 1200])
def test_menu_status_reserves_all_menu_titles(qapplication, width):
    window = QtWidgets.QMainWindow()
    menu = window.menuBar()
    menu.setNativeMenuBar(False)
    for name in ("File", "Edit", "Window", "Options", "View", "Help"):
        menu.addMenu(name)
    status = PlotStatusWidget(window)
    controller = _MenuStatus(window, status)
    window.resize(width, 400)
    window.show()
    try:
        for busy in (False, True):
            if busy:
                status.start_progress()
            else:
                status.show_message("Loaded data " * 100)
            qapplication.processEvents()
            controller._layout()
            QtTest.QTest.qWait(20)
            assert menu.cornerWidget() is status
            assert status.isVisible()
            for action in menu.actions():
                assert not menu.actionGeometry(action).intersects(status.geometry())
            assert status.geometry().right() <= menu.width()
            assert menu.height() < 50
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize("theme", [light, dark])
def test_mac_status_avoids_centred_title_and_reserves_header(qapplication, theme):
    window = QtWidgets.QMainWindow()
    window.setStyleSheet(theme.main)
    window.setWindowTitle("Plot 12")
    central = QtWidgets.QLabel("Plot area")
    window.setCentralWidget(central)
    status = PlotStatusWidget(window)
    header = _MacTitleArea(window, status)
    window.show()
    try:
        for width in (460, 800, 1200):
            window.resize(width, 400)
            qapplication.processEvents()
            assert status.isVisibleTo(window)
            assert status.x() > window.width() // 2
            assert status.geometry().right() < window.width()
            assert central.y() >= header.height()
            assert window.contentsMargins().top() == header.height()
            assert window.windowTitle() == "Plot 12"
            assert header.palette().window() == qapplication.palette().window()
            assert status.stage_label.palette().windowText() == qapplication.palette().windowText()
            assert not window.windowFlags() & QtCore.Qt.WindowType.FramelessWindowHint
            assert window.windowFlags() & QtCore.Qt.WindowType.WindowTitleHint
    finally:
        window.close()
        window.deleteLater()
