"""Plot identities remain usable when windows close or replace their source."""

from types import SimpleNamespace

from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_title import assign_plot_title, inherit_plot_title


def test_shared_title_counts_runs_by_database_and_lists_every_source(qapplication):
    window = qtw.QMainWindow()
    try:
        window._plot_source_info = ("first/data.db", 42, "conductance")
        source = SimpleNamespace(_plot_source_info=("first/data.db", 42, "current"))
        window.lines = {"second": SimpleNamespace(from_win=source)}
        number = assign_plot_title(window)
        prefix = f"Plot {number} — "
        assert window.windowTitle() == prefix + "Run 42, conductance + current"
        source._plot_source_info = ("first/data.db", 47, "conductance")
        assign_plot_title(window)
        assert window.windowTitle() == prefix + "conductance, runs 42 and 47"
        source._plot_source_info = ("second/data.db", 42, "conductance")
        assign_plot_title(window)
        assert window.windowTitle() == prefix + "2 measurements from 2 runs"
        assert window._plot_sources_tooltip.splitlines() == [
            "first/data.db — Run 42, conductance",
            "second/data.db — Run 42, conductance",
        ]
    finally:
        window.deleteLater()


def test_visible_numbers_are_stable_and_hidden_sources_do_not_consume_them(qapplication):
    hidden, first, second, third = [qtw.QMainWindow() for _ in range(4)]
    hidden.visible = False
    try:
        assert assign_plot_title(hidden) is None
        first_number = assign_plot_title(first)
        second_number = assign_plot_title(second)
        assert second_number == first_number + 1
        assert first.windowTitle() == f"Plot {first_number}"
        first.close()
        assert assign_plot_title(second) == second_number
        assert second.windowTitle() == f"Plot {second_number}"
        assert assign_plot_title(third) == second_number + 1
        assert hidden.windowTitle() == ""
    finally:
        for window in (hidden, first, second, third):
            window.deleteLater()


def test_replacing_a_primary_source_preserves_the_window_identity(qapplication):
    source, replacement, next_window = [qtw.QMainWindow() for _ in range(3)]
    try:
        original_number = assign_plot_title(source)
        replacement_number = assign_plot_title(replacement)
        inherit_plot_title(source, replacement)
        source.close()
        assert replacement.windowTitle() == f"Plot {original_number}"
        assert assign_plot_title(replacement) == original_number
        assert assign_plot_title(next_window) > replacement_number
    finally:
        for window in (source, replacement, next_window):
            window.deleteLater()
