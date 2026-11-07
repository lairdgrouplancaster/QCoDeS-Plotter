"""Keep timer precision independent of the refresh control's display precision."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from ._config_persistence import set_widget_value_without_signals


class RefreshIntervalSpinBox(qtw.QDoubleSpinBox):
    """Fit the longest interval without the native spin box's surplus width."""

    heightChanged = QtCore.pyqtSignal(int)

    def __init__(
        self, parent=None, *,
        refresh_description="How often this plot checks for new measurement data.",
    ):
        super().__init__(parent)
        self.setObjectName("refreshIntervalSpin")
        self.setAccessibleName("Refresh interval in seconds")
        self.setRange(0.0, 10_000.0)
        self.setSingleStep(0.1)
        self.setDecimals(1)
        self.setSuffix(" s")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self._refresh_tooltip = (
            refresh_description + "\nSet to 0 to disable automatic refresh."
        )
        self.setToolTip(self._refresh_tooltip)
        self.setLocale(QtCore.QLocale(
            QtCore.QLocale.Language.English, QtCore.QLocale.Country.UnitedKingdom,
        ))
        self.setGroupSeparatorShown(True)
        self.setSizePolicy(qtw.QSizePolicy.Policy.Fixed, qtw.QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        size = super().sizeHint()
        option = qtw.QStyleOptionSpinBox()
        self.initStyleOption(option)
        option.rect = QtCore.QRect(QtCore.QPoint(), size)
        edit_rect = self._editor_rect(option)
        # QDoubleSpinBox constructs its editor and inherits an active style;
        # Qt's generic widget stubs permit None for both accessors.
        editor = cast(qtw.QLineEdit, self.lineEdit())
        style = cast(qtw.QStyle, editor.style())
        frame = qtw.QStyleOptionFrame()
        frame.initFrom(editor)
        frame.state |= qtw.QStyle.StateFlag.State_Sunken
        if editor.hasFrame():
            frame.lineWidth = style.pixelMetric(
                qtw.QStyle.PixelMetric.PM_DefaultFrameWidth, frame, editor
            )
        frame.rect = QtCore.QRect(QtCore.QPoint(), edit_rect.size())
        contents = style.subElementRect(
            qtw.QStyle.SubElement.SE_LineEditContents, frame, editor
        )
        text_width = max(
            editor.fontMetrics().horizontalAdvance(
                self.prefix() + self.textFromValue(value) + self.suffix()
            )
            for value in (self.minimum(), self.maximum())
        )
        margins = editor.textMargins()
        size.setWidth(
            size.width() - contents.width() + text_width
            + margins.left() + margins.right() + 4
        )
        return size

    def minimumSizeHint(self):
        return self.sizeHint()

    def _editor_rect(self, option):
        style = cast(qtw.QStyle, self.style())
        rect = style.subControlRect(
            qtw.QStyle.ComplexControl.CC_SpinBox, option,
            qtw.QStyle.SubControl.SC_SpinBoxEditField, self,
        )
        # Native styles may stack the buttons or place them side by side.
        # Fit up to the first button rather than reserving a fixed strip.
        buttons = [
            style.subControlRect(qtw.QStyle.ComplexControl.CC_SpinBox, option, control, self)
            for control in (
                qtw.QStyle.SubControl.SC_SpinBoxUp,
                qtw.QStyle.SubControl.SC_SpinBoxDown,
            )
        ]
        buttons = [button for button in buttons if not button.isEmpty()]
        if buttons:
            rect.setRight(min(button.left() for button in buttons) - 1)
        return rect

    def _fit_editor(self):
        option = qtw.QStyleOptionSpinBox()
        self.initStyleOption(option)
        cast(qtw.QLineEdit, self.lineEdit()).setGeometry(self._editor_rect(option))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_editor()
        self.heightChanged.emit(event.size().height())

    def showEvent(self, event):
        super().showEvent(event)
        self.setFixedWidth(self.sizeHint().width())
        self._fit_editor()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QtCore.QEvent.Type.FontChange,
            QtCore.QEvent.Type.ApplicationFontChange,
            QtCore.QEvent.Type.StyleChange,
        ):
            self.setFixedWidth(self.sizeHint().width())
            self._fit_editor()


@dataclass
class _RefreshIntervalState:
    """One control's accepted precision, displayed value, and pending edit."""

    interval: float | None = None
    displayed: float | None = None
    text_edited: bool = False


def _refresh_state(spin_box: qtw.QDoubleSpinBox) -> _RefreshIntervalState:
    # Some callers supply ordinary Qt spin boxes, so state belongs to the
    # specific control rather than requiring the custom widget subclass.
    state = spin_box.__dict__.get("_qplot_refresh_state")
    if state is None:
        state = _RefreshIntervalState()
        spin_box.__dict__["_qplot_refresh_state"] = state
    return state


def refresh_interval_value(spin_box: qtw.QDoubleSpinBox) -> float:
    """Return the accepted interval unless the control has actually changed."""
    displayed = float(spin_box.value())
    state = _refresh_state(spin_box)
    if displayed == state.displayed and state.interval is not None:
        return state.interval
    return displayed


def remember_refresh_interval(spin_box: qtw.QDoubleSpinBox, interval: float) -> None:
    """Record a successful settings load or explicit edit without rounding it."""
    state = _refresh_state(spin_box)
    state.interval = float(interval)
    state.displayed = float(spin_box.value())
    state.text_edited = False
    tooltip = getattr(
        spin_box, "_refresh_tooltip",
        "Automatic refresh interval in seconds.\n"
        "Set to 0 to disable automatic refresh.",
    )
    if state.displayed != interval:
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
        state = _refresh_state(spin_box)
        if (
            interval == state.displayed
            and interval != state.interval
            and not state.text_edited
        ):
            # Qt emits the displayed value on an unedited Return as well.
            return
        callback(interval)

    spin_box.valueChanged.connect(value_changed)
    editor = spin_box.lineEdit()
    if editor is None:
        return

    def mark_edited(_text: str) -> None:
        _refresh_state(spin_box).text_edited = True

    def finish_edit() -> None:
        state = _refresh_state(spin_box)
        if not state.text_edited:
            return
        displayed = float(spin_box.value())
        if refresh_interval_value(spin_box) != displayed:
            spin_box.valueChanged.emit(displayed)
        state.text_edited = False

    editor.textEdited.connect(mark_edited)
    spin_box.editingFinished.connect(finish_edit)
