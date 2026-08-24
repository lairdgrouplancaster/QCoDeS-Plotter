from unittest.mock import patch

from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._window_controls import (
    _WINDOW_LIST_ENTRY_PROPERTY,
    add_application_quit_action,
    add_standard_window_controls,
)


class FakeWindow:
    def __init__(
            self,
            *,
            label="",
            operation_kind=None,
            active=False,
            minimized=False,
            visible=True,
            ):
        self.label = label
        self.operation_kind = operation_kind
        self.active = active
        self.minimized = minimized
        self.visible = visible
        self.show_normal_calls = 0
        self.show_calls = 0
        self.raise_calls = 0
        self.activate_calls = 0

    def isActiveWindow(self):
        return self.active

    def isMinimized(self):
        return self.minimized

    def isVisible(self):
        return self.visible

    def showNormal(self):
        self.show_normal_calls += 1

    def show(self):
        self.show_calls += 1

    def raise_(self):
        self.raise_calls += 1

    def activateWindow(self):
        self.activate_calls += 1


def _window_list_actions(menu):
    return [
        action for action in menu.actions()
        if action.property(_WINDOW_LIST_ENTRY_PROPERTY)
        ]


def test_application_quit_action_is_shared_between_window_menus():
    main_window = qtw.QMainWindow()
    plot_window = qtw.QMainWindow()
    main_menu = qtw.QMenu(main_window)
    plot_menu = qtw.QMenu(plot_window)
    quit_requests = []
    main_window.quit_application = lambda: quit_requests.append(True)

    try:
        with patch(
                "qplot.windows._window_controls.main_window_for",
                return_value=main_window,
                ):
            main_action = add_application_quit_action(
                main_window,
                main_menu,
                main_window.quit_application,
                )
            plot_action = add_application_quit_action(
                plot_window,
                plot_menu,
                lambda: None,
                )

        assert plot_action is main_action
        assert main_menu.actions() == [main_action]
        assert plot_menu.actions() == [main_action]
        main_action.trigger()
        assert quit_requests == [True]
    finally:
        plot_window.deleteLater()
        main_window.deleteLater()


def test_application_quit_shortcut_works_while_modal_dialog_is_open():
    main_window = qtw.QMainWindow()
    dialog = qtw.QDialog(main_window)
    menu = qtw.QMenu(main_window)
    quit_requests = []
    main_window.quit_application = lambda: quit_requests.append(True)

    try:
        with patch(
                "qplot.windows._window_controls.main_window_for",
                return_value=main_window,
                ):
            quit_action = add_application_quit_action(
                main_window,
                menu,
                main_window.quit_application,
                )

        main_window.show()
        QtCore.QTimer.singleShot(
            0,
            lambda: QtTest.QTest.keySequence(dialog, quit_action.shortcut()),
            )
        QtCore.QTimer.singleShot(100, dialog.reject)
        dialog.exec()

        assert quit_requests == [True]
    finally:
        dialog.close()
        main_window.close()
        dialog.deleteLater()
        main_window.deleteLater()


def test_window_menu_lists_qplot_windows_and_focuses_selected_window():
    menu_owner = qtw.QMainWindow()
    main_window = FakeWindow(active=True)
    one_dimensional = FakeWindow(label="ID:42 signal", operation_kind="plot1d")
    two_dimensional = FakeWindow(
        label="ID:57 conductance",
        operation_kind="plot2d",
        minimized=True,
        )
    cut = FakeWindow(
        label="ID:57 conductance [cut 2]",
        operation_kind="sweeper",
        )
    main_window.windows = [one_dimensional, two_dimensional, cut]

    try:
        with patch(
                "qplot.windows._window_controls.main_window_for",
                return_value=main_window,
                ):
            menu = add_standard_window_controls(menu_owner)
            menu.aboutToShow.emit()

        actions = _window_list_actions(menu)
        assert [action.text() for action in actions] == [
            "qPlot",
            "1D — ID:42 signal",
            "2D — ID:57 conductance",
            "Cut — ID:57 conductance [cut 2]",
        ]
        assert actions[0].isChecked()

        actions[2].trigger()
        assert two_dimensional.show_normal_calls == 1
        assert two_dimensional.show_calls == 0
        assert two_dimensional.raise_calls == 1
        assert two_dimensional.activate_calls == 1
    finally:
        menu_owner.deleteLater()


def test_window_menu_refreshes_entries_and_disambiguates_duplicate_labels():
    menu_owner = qtw.QMainWindow()
    main_window = FakeWindow()
    first_plot = FakeWindow(label="ID:42 signal", operation_kind="plot1d")
    second_plot = FakeWindow(label="ID:42 signal", operation_kind="plot1d")
    main_window.windows = [first_plot, second_plot]

    try:
        with patch(
                "qplot.windows._window_controls.main_window_for",
                return_value=main_window,
                ):
            menu = add_standard_window_controls(menu_owner)
            menu.aboutToShow.emit()
            assert [action.text() for action in _window_list_actions(menu)] == [
                "qPlot",
                "1D — ID:42 signal (1)",
                "1D — ID:42 signal (2)",
            ]

            main_window.windows = [second_plot]
            menu.aboutToShow.emit()

        assert [action.text() for action in _window_list_actions(menu)] == [
            "qPlot",
            "1D — ID:42 signal",
        ]
    finally:
        menu_owner.deleteLater()
