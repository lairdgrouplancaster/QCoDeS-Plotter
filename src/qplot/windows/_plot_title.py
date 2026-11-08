"""Stable, session-local names for visible plot windows."""

from PyQt6 import QtWidgets as qtw


def assign_plot_title(window: qtw.QWidget) -> int | None:
    """Number a visible plot once; closing other plots never changes its name."""
    # Hidden renderers load overlay data without creating another user window.
    if not window.__dict__.get("visible", True):
        return None
    number = window.__dict__.get("_plot_window_number")
    if number is None:
        application = qtw.QApplication.instance()
        if application is None:
            raise RuntimeError("A QApplication is required to name plot windows.")
        number = application.__dict__.get("_qplot_next_plot_number", 1)
        application.__dict__["_qplot_next_plot_number"] = number + 1
        window.__dict__["_plot_window_number"] = number
    window.setWindowTitle(f"Plot {number}")
    return number


def inherit_plot_title(source: qtw.QWidget, replacement: qtw.QWidget) -> None:
    """Retain the visible window's identity when its primary source changes."""
    number = source.__dict__.get("_plot_window_number")
    if number is not None:
        replacement.__dict__["_plot_window_number"] = number
        replacement.setWindowTitle(f"Plot {number}")
