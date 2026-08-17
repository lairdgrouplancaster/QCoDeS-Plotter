from unittest.mock import patch

from PyQt6 import QtWidgets as qtw

from qplot.windows._window_controls import (
    _WINDOW_LIST_ENTRY_PROPERTY,
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
