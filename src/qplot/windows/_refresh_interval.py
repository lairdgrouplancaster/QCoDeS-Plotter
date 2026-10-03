"""Keep timer precision independent of the refresh control's display precision."""

from collections.abc import Callable

from PyQt6 import QtWidgets as qtw

from ._config_persistence import set_widget_value_without_signals


def refresh_interval_value(spin_box: qtw.QDoubleSpinBox) -> float:
    """Return the accepted interval unless the control has actually changed."""
    displayed = float(spin_box.value())
    previous_display = spin_box.__dict__.get("_qplot_refresh_display")
    if displayed == previous_display:
        return float(spin_box.__dict__["_qplot_refresh_interval"])
    return displayed


def remember_refresh_interval(spin_box: qtw.QDoubleSpinBox, interval: float) -> None:
    """Record a successful settings load or explicit edit without rounding it."""
    state = spin_box.__dict__
    state["_qplot_refresh_interval"] = float(interval)
    state["_qplot_refresh_display"] = float(spin_box.value())
    state["_qplot_refresh_text_edited"] = False
    tooltip = "Refresh interval in seconds"
    if state["_qplot_refresh_display"] != interval:
        tooltip += f"\nConfigured interval: {interval!r} s"
    spin_box.setToolTip(tooltip)


def set_refresh_interval(spin_box: qtw.QDoubleSpinBox, interval: float) -> None:
    """Load the exact interval while suppressing persistence/edit callbacks."""
    set_widget_value_without_signals(spin_box, spin_box.setValue, interval)
    remember_refresh_interval(spin_box, interval)


def connect_refresh_interval_edits(
    spin_box: qtw.QDoubleSpinBox,
    callback: Callable[[float], None],
) -> None:
    """Include typing the already displayed zero as an explicit manual edit."""
    def value_changed(interval: float) -> None:
        state = spin_box.__dict__
        if (
            interval == state.get("_qplot_refresh_display")
            and interval != state.get("_qplot_refresh_interval")
            and not state.get("_qplot_refresh_text_edited", False)
        ):
            # Qt emits the displayed value on an unedited Return as well.
            return
        callback(interval)

    spin_box.valueChanged.connect(value_changed)
    editor = spin_box.lineEdit()
    if editor is None:
        return

    def mark_edited(_text: str) -> None:
        spin_box.__dict__["_qplot_refresh_text_edited"] = True

    def finish_edit() -> None:
        if not spin_box.__dict__.get("_qplot_refresh_text_edited", False):
            return
        displayed = float(spin_box.value())
        if refresh_interval_value(spin_box) != displayed:
            spin_box.valueChanged.emit(displayed)
        spin_box.__dict__["_qplot_refresh_text_edited"] = False

    editor.textEdited.connect(mark_edited)
    spin_box.editingFinished.connect(finish_edit)
