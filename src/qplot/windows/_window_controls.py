from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw
from PyQt5.QtGui import QKeySequence

from ._shortcuts import platform_key_sequences, standard_key_sequences


CONFIRM_CLOSE_ALL_KEY = "user_preference.confirm_close_all"
CONFIRM_QUIT_KEY = "user_preference.confirm_close"
DO_NOT_ASK_AGAIN_LABEL = "Don't ask again"


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

    main_front_back_action = qtw.QAction("Main Window &Front/Back", window)
    set_window_shortcuts(main_front_back_action, [QKeySequence("Ctrl+Shift+M")])
    main_front_back_action.setStatusTip(
        "Bring the main window to front, or behind the graph windows"
        )
    main_front_back_action.triggered.connect(lambda: toggle_main_window_front_back(window))
    window_menu.addAction(main_front_back_action)

    window_menu.addSeparator()

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


def add_confirmation_options(window, menu):
    """
    Adds shared confirmation preferences to a menu.

    """
    close_all_action = add_config_checkbox_action(
        window,
        menu,
        "Confirm Before Closing All Plot Windows",
        CONFIRM_CLOSE_ALL_KEY,
        "Ask before closing every open plot window",
        )
    quit_action = add_config_checkbox_action(
        window,
        menu,
        "Confirm Before Quit",
        CONFIRM_QUIT_KEY,
        "Ask before quitting qPlot",
        )
    return close_all_action, quit_action


def add_restore_defaults_option(window, menu):
    """
    Adds a restore-default-settings action to a menu.

    """
    action = qtw.QAction("Restore Default Settings...", window)
    action.setStatusTip("Restore all qPlot settings to their defaults")
    action.triggered.connect(lambda: request_restore_defaults(window))
    menu.addAction(action)
    return action


def request_restore_defaults(window):
    """
    Requests a settings reset through the main window.

    """
    if hasattr(window, "restore_default_settings"):
        window.restore_default_settings()
        return

    main_window = main_window_for(window)
    if main_window is not None and hasattr(main_window, "restore_default_settings"):
        main_window.restore_default_settings()


def add_config_checkbox_action(window, menu, text, key, status_tip):
    """
    Adds a checkable config-backed action to a menu.

    """
    action = qtw.QAction(text, window, checkable=True)
    action.setStatusTip(status_tip)

    def sync_checked():
        action.blockSignals(True)
        action.setChecked(config_bool(window.config, key, default=True))
        action.blockSignals(False)

    sync_checked()
    action.toggled.connect(
        lambda checked: window.config.update(key, checked)
        )
    menu.aboutToShow.connect(sync_checked)
    menu.addAction(action)
    return action


def close_all_warning_enabled(config):
    """
    Returns whether closing all plot windows should ask first.

    """
    return config_bool(config, CONFIRM_CLOSE_ALL_KEY, default=True)


def ask_confirmation_with_dont_ask_again(
        window,
        title,
        message,
        config_key,
        default_button=qtw.QMessageBox.No,
        ):
    """
    Asks for confirmation and lets the user disable future prompts.

    """
    parent = window if isinstance(window, qtw.QWidget) else None
    box = qtw.QMessageBox(
        qtw.QMessageBox.Question,
        title,
        message,
        qtw.QMessageBox.Yes | qtw.QMessageBox.No,
        parent,
        )
    box.setDefaultButton(default_button)

    dont_ask_again = qtw.QCheckBox(DO_NOT_ASK_AGAIN_LABEL)
    box.setCheckBox(dont_ask_again)

    reply = box.exec_()
    if reply == qtw.QMessageBox.Yes and dont_ask_again.isChecked():
        window.config.update(config_key, False)
    return reply


def config_bool(config, key, default):
    """
    Returns a boolean config value, falling back for older config files.

    """
    try:
        return config.get(key)
    except KeyError:
        return default


def main_window_for(window):
    """
    Finds the main qPlot window for shared window actions.

    """
    app = qtw.QApplication.instance()
    if app is None:
        return window if window.__class__.__name__ == "MainWindow" else None

    for top_level in app.topLevelWidgets():
        if top_level.__class__.__name__ == "MainWindow":
            return top_level

    return window if window.__class__.__name__ == "MainWindow" else None


def toggle_main_window_front_back(window):
    """
    Brings the main qPlot window forward, or behind qPlot graph windows.

    """
    main_window = main_window_for(window)
    if main_window is None:
        return

    if main_window.isActiveWindow():
        send_main_window_behind_graphs(main_window, window)
        return

    if main_window.isMinimized():
        main_window.showNormal()
    else:
        main_window.show()

    main_window.raise_()
    main_window.activateWindow()


def send_main_window_behind_graphs(main_window, active_window):
    """
    Places graph windows above the main qPlot window without lowering qPlot
    behind unrelated applications.

    """
    app = qtw.QApplication.instance()
    if app is None:
        return

    graph_windows = [
        window for window in app.topLevelWidgets()
        if window is not main_window
        and window.isVisible()
        and hasattr(window, "_guid")
        and hasattr(window, "param")
        ]
    if not graph_windows:
        return

    for graph_window in graph_windows:
        graph_window.raise_()

    if active_window in graph_windows:
        active_window.activateWindow()
    else:
        graph_windows[-1].activateWindow()


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
