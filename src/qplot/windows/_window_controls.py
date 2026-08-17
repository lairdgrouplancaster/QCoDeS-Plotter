from collections import Counter

from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw

from ._commands import create_action
from ._config_persistence import (
    persist_config_value,
    set_widget_value_without_signals,
)

CONFIRM_CLOSE_ALL_KEY = "user_preference.confirm_close_all"
CONFIRM_QUIT_KEY = "user_preference.confirm_close"
DO_NOT_ASK_AGAIN_LABEL = "Don't ask again"

_WINDOW_LIST_ACTIONS_ATTRIBUTE = "_qplot_window_list_actions"
_WINDOW_LIST_ENTRY_PROPERTY = "qplotWindowListEntry"
_WINDOW_KIND_LABELS = {
    "plot1d": "1D",
    "plot2d": "2D",
    "sweeper": "Cut",
}

def add_standard_window_controls(window):
    """
    Adds standard window control actions to a QMainWindow menu bar.

    """
    window_menu = window.menuBar().addMenu("&Window")

    main_front_back_action = create_action("window.main_front_back", window)
    main_front_back_action.triggered.connect(lambda: toggle_main_window_front_back(window))
    window_menu.addAction(main_front_back_action)

    window_menu.addSeparator()

    minimize_action = create_action("window.minimize", window)
    minimize_action.triggered.connect(window.showMinimized)
    window_menu.addAction(minimize_action)

    maximize_action = create_action("window.maximize_restore", window)
    maximize_action.triggered.connect(lambda: toggle_maximized(window))
    window_menu.addAction(maximize_action)

    fullscreen_action = create_action("window.full_screen", window)
    fullscreen_action.triggered.connect(lambda: toggle_fullscreen(window))
    window_menu.addAction(fullscreen_action)

    # Plot windows are registered by the main window only after they have been
    # constructed. Rebuild this section immediately before display so every
    # Window menu reflects the currently open qPlot windows.
    window_menu.aboutToShow.connect(
        lambda: populate_available_window_actions(window, window_menu)
        )

    return window_menu


def populate_available_window_actions(window, window_menu):
    """Add the current qPlot windows to the bottom of a Window menu."""

    _remove_available_window_actions(window_menu)

    main_window = main_window_for(window)
    if main_window is None:
        return

    plot_windows = tuple(getattr(main_window, "windows", ()) or ())
    labels = [_plot_window_menu_label(plot_window) for plot_window in plot_windows]
    label_counts = Counter(labels)
    label_indexes = Counter()

    actions = [window_menu.addSeparator()]
    actions.append(_add_window_menu_action(window_menu, main_window, "qPlot"))
    for plot_window, label in zip(plot_windows, labels, strict=True):
        label_indexes[label] += 1
        if label_counts[label] > 1:
            label = f"{label} ({label_indexes[label]})"
        actions.append(_add_window_menu_action(window_menu, plot_window, label))

    setattr(window_menu, _WINDOW_LIST_ACTIONS_ATTRIBUTE, actions)


def _remove_available_window_actions(window_menu):
    """Remove actions generated during the previous Window-menu opening."""

    for action in getattr(window_menu, _WINDOW_LIST_ACTIONS_ATTRIBUTE, ()):
        window_menu.removeAction(action)
        action.deleteLater()
    setattr(window_menu, _WINDOW_LIST_ACTIONS_ATTRIBUTE, ())


def _plot_window_menu_label(plot_window):
    """Return a concise, recognisable label for a qPlot graph window."""

    operation_kind = getattr(plot_window, "operation_kind", None)
    kind_label = _WINDOW_KIND_LABELS.get(operation_kind)
    label = str(getattr(plot_window, "label", "")).strip()
    if not label:
        window_title = getattr(plot_window, "windowTitle", None)
        label = str(window_title()).strip() if callable(window_title) else "Plot"

    return f"{kind_label} — {label}" if kind_label is not None else label


def _add_window_menu_action(menu, target_window, text):
    """Create a checked-as-active menu action that focuses ``target_window``."""

    action = QtGui.QAction(text, menu)
    action.setCheckable(True)
    action.setChecked(target_window.isActiveWindow())
    action.setProperty(_WINDOW_LIST_ENTRY_PROPERTY, True)
    action.setStatusTip(f"Show {text}")
    action.triggered.connect(
        lambda _checked=False: focus_qplot_window(target_window)
        )
    menu.addAction(action)
    return action


def focus_qplot_window(target_window):
    """Restore, raise, and activate a qPlot window selected from the menu."""

    if target_window.isMinimized():
        target_window.showNormal()
    elif not target_window.isVisible():
        target_window.show()

    target_window.raise_()
    target_window.activateWindow()


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
    Adds a reset-all-settings action to a menu.

    """
    action = QtGui.QAction("Reset All Settings...", window)
    action.setStatusTip("Reset all qPlot settings to their defaults")
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
    action = QtGui.QAction(text, window)
    action.setCheckable(True)
    action.setStatusTip(status_tip)

    def sync_checked():
        action.blockSignals(True)
        action.setChecked(window.config.get(key))
        action.blockSignals(False)

    sync_checked()

    def persist_checked(checked):
        previous_checked = window.config.get(key)
        persist_config_value(
            window,
            window.config,
            key,
            checked,
            "the confirmation preference",
            lambda: set_widget_value_without_signals(
                action,
                action.setChecked,
                previous_checked,
                ),
            )

    action.toggled.connect(persist_checked)
    menu.aboutToShow.connect(sync_checked)
    menu.addAction(action)
    return action


def close_all_warning_enabled(config):
    """
    Returns whether closing all plot windows should ask first.

    """
    return config.get(CONFIRM_CLOSE_ALL_KEY)


def ask_confirmation_with_dont_ask_again(
        window,
        title,
        message,
        config_key,
        default_button=qtw.QMessageBox.StandardButton.No,
        ):
    """
    Asks for confirmation and lets the user disable future prompts.

    """
    parent = window if isinstance(window, qtw.QWidget) else None
    box = qtw.QMessageBox(
        qtw.QMessageBox.Icon.Question,
        title,
        message,
        qtw.QMessageBox.StandardButton.Yes | qtw.QMessageBox.StandardButton.No,
        parent,
        )
    box.setDefaultButton(default_button)

    dont_ask_again = qtw.QCheckBox(DO_NOT_ASK_AGAIN_LABEL)
    box.setCheckBox(dont_ask_again)

    reply = box.exec()
    if reply == qtw.QMessageBox.StandardButton.Yes and dont_ask_again.isChecked():
        persist_config_value(
            window,
            window.config,
            config_key,
            False,
            "the close-confirmation preference",
            )
    return reply


def main_window_for(window):
    """
    Finds the main qPlot window for shared window actions.

    """
    app = qtw.QApplication.instance()
    if app is None:
        return window if window.__class__.__name__ == "MainWindow" else None

    for top_level in qtw.QApplication.topLevelWidgets():
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
        window for window in qtw.QApplication.topLevelWidgets()
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
