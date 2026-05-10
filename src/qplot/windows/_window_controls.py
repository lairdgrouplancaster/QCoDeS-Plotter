from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw
from PyQt5.QtGui import QKeySequence

from ._shortcuts import platform_key_sequences, standard_key_sequences


def set_window_shortcuts(action, shortcuts):
    """
    Applies shortcuts to an action only when the platform has any.

    """
    if shortcuts:
        action.setShortcuts(shortcuts)
    action.setShortcutContext(QtCore.Qt.WindowShortcut)


def add_standard_window_controls(window):
    """
    Adds standard window control actions to a QMainWindow menu bar.

    """
    window_menu = window.menuBar().addMenu("&Window")

    minimize_action = qtw.QAction("&Minimize", window)
    set_window_shortcuts(
        minimize_action,
        platform_key_sequences(
            mac=["Ctrl+M"],
            windows=["Alt+Space, N"],
            )
        )
    minimize_action.setStatusTip("Minimize this window")
    minimize_action.triggered.connect(window.showMinimized)
    window_menu.addAction(minimize_action)

    maximize_action = qtw.QAction("Ma&ximize / Restore", window)
    set_window_shortcuts(
        maximize_action,
        platform_key_sequences(
            windows=["Alt+Space, X", "Alt+Space, R"],
            )
        )
    maximize_action.setStatusTip("Maximize or restore this window")
    maximize_action.triggered.connect(lambda: toggle_maximized(window))
    window_menu.addAction(maximize_action)

    fullscreen_action = qtw.QAction("&Full Screen", window)
    set_window_shortcuts(
        fullscreen_action,
        standard_key_sequences(
            QKeySequence.FullScreen,
            platform_key_sequences(
                mac=["Ctrl+Meta+F"],
                windows=["F11", "Alt+Enter"],
                )
            )
        )
    fullscreen_action.setStatusTip("Enter or leave full screen")
    fullscreen_action.triggered.connect(lambda: toggle_fullscreen(window))
    window_menu.addAction(fullscreen_action)


def toggle_maximized(window):
    """
    Toggles a window between maximized and normal size.

    """
    if window.isMaximized():
        window.showNormal()
    else:
        window.showMaximized()


def toggle_fullscreen(window):
    """
    Toggles a window between full-screen and normal size.

    """
    if window.isFullScreen():
        window.showNormal()
    else:
        window.showFullScreen()
