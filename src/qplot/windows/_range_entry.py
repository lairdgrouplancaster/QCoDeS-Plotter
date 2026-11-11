"""Shared input-format selector for axis and color-scale ranges."""

from collections.abc import Callable
from math import isfinite

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


def _center_span(lower: float, upper: float) -> tuple[float, float]:
    span = upper - lower
    # Avoid overflowing the sum of large, same-sign endpoints. Using the sum
    # near zero also avoids halving subnormal endpoints individually.
    center = (lower + upper) / 2 if lower <= 0 <= upper else lower + span / 2
    return center, span


class RangeEntry(qtw.QWidget):
    """Re-express existing limits without feeding rounded values into the plot.

    The owner commits edits through its existing range validation. Format
    changes only update these controls; they never apply a range or change Auto.
    Each instance retains its format for the lifetime of the plot window.
    """

    def __init__(
        self,
        first: qtw.QLineEdit,
        second: qtw.QLineEdit,
        first_label: qtw.QLabel,
        second_label: qtw.QLabel,
        *,
        accessible_prefix: str,
        parent: qtw.QWidget,
    ) -> None:
        super().__init__(parent)
        self._fields = first, second
        self._labels = first_label, second_label
        self._manual_styles = {
            widget: widget.styleSheet() for widget in (*self._fields, *self._labels)
        }
        self._automatic: bool | None = None
        self._accessible_prefix = accessible_prefix
        self._limits = (0.0, 1.0)
        self._displayed_values = self._limits
        self.center_span = False
        self.summary = qtw.QLabel(parent)
        self.summary.setObjectName("rangeEntrySummary")
        palette = self.summary.palette()
        palette.setColor(
            QtGui.QPalette.ColorRole.WindowText,
            palette.color(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.WindowText),
        )
        self.summary.setPalette(palette)
        self.summary.setWordWrap(True)
        font = self.summary.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
        self.summary.setFont(font)
        self.summary.setSizePolicy(
            qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred,
        )

        layout = qtw.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.group = qtw.QButtonGroup(self)
        self.min_max_button = qtw.QPushButton("Min / Max", self)
        self.center_span_button = qtw.QPushButton("Centre / Span", self)
        self.min_max_button.setObjectName("rangeEntryMinMax")
        self.center_span_button.setObjectName("rangeEntryCentreSpan")
        for index, button in enumerate((self.min_max_button, self.center_span_button)):
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setAccessibleName(f"{accessible_prefix} {button.text()} input")
            self.group.addButton(button, index)
            layout.addWidget(button, 1)
        self.min_max_button.setChecked(True)
        self.setStyleSheet(
            "QPushButton { padding: 3px 8px; border: 1px solid palette(mid); "
            "border-radius: 0; background: palette(button); color: palette(button-text); }"
            # The right segment owns the single shared divider. Only the
            # outside corners are rounded, so both buttons read as one control.
            "QPushButton#rangeEntryMinMax { border-right: 0; "
            "border-top-left-radius: 6px; border-bottom-left-radius: 6px; }"
            "QPushButton#rangeEntryCentreSpan { "
            "border-top-right-radius: 6px; border-bottom-right-radius: 6px; }"
            "QPushButton:checked { background: palette(highlight); "
            "color: palette(highlighted-text); }"
            "QPushButton:focus { border-color: palette(text); }"
        )
        self.setToolTip("Change the input format without changing the plotted range.")
        self.center_span_button.setToolTip(
            "Centre is the arithmetic midpoint; span is maximum minus minimum. "
            "Both use data values, including on logarithmic axes."
        )
        self.group.idClicked.connect(self._change_format)
        self.set_limits(*self._limits)

    def bind_auto_mode(
        self,
        auto_radio: qtw.QRadioButton,
        start_manual: Callable[[tuple[float, float]], object],
    ) -> None:
        """Freeze automatic limits on the first real edit, preserving the draft."""
        self._auto_radio = auto_radio
        self._start_manual = start_manual
        auto_radio.toggled.connect(self.set_automatic)
        for field in self._fields:
            field.textEdited.connect(self._begin_manual_edit)
            field.setToolTip(
                (field.toolTip() + "\n" if field.toolTip() else "")
                + "Editing switches to Manual. Press Enter or leave the field to apply."
            )
        self.set_automatic(auto_radio.isChecked())

    def set_automatic(self, automatic: bool) -> None:
        """Mute automatic values without disabling keyboard or mouse editing."""
        if self._automatic == automatic:
            return
        self._automatic = automatic
        for widget, original_style in self._manual_styles.items():
            muted_style = (
                "QLineEdit { color: palette(mid); background-color: palette(window); }"
                if isinstance(widget, qtw.QLineEdit) else
                "QLabel { color: palette(mid); }"
            )
            widget.setStyleSheet(original_style + (muted_style if automatic else ""))

    def _begin_manual_edit(self, _text: str) -> None:
        if not self._auto_radio.isChecked():
            return
        drafts = [(field.text(), field.cursorPosition()) for field in self._fields]
        self._start_manual(self._limits)
        # Switching modes may synchronise controls. Keep the user's first
        # keystroke, including incomplete input such as a minus sign.
        for field, (text, cursor) in zip(self._fields, drafts, strict=True):
            with QtCore.QSignalBlocker(field):
                field.setText(text)
                field.setCursorPosition(cursor)

    def _change_format(self, index: int) -> None:
        self.center_span = bool(index)
        self.set_limits(*self._limits)

    def set_limits(self, lower: float, upper: float) -> None:
        """Synchronize the displayed representation from canonical endpoints."""
        self._limits = float(lower), float(upper)
        center, span = _center_span(*self._limits)
        if self.center_span:
            names = "Centre", "Span"
            values = center, span
            summary_names = "Minimum", "Maximum"
            summary_values = self._limits
        else:
            names = "Minimum", "Maximum"
            values = self._limits
            summary_names = "Centre", "Span"
            summary_values = center, span
        self._displayed_values = values
        for field, label, name, value in zip(
            self._fields, self._labels, names, values, strict=True,
        ):
            blocker = QtCore.QSignalBlocker(field)
            # Keep the shortest spelling that round-trips to the same float.
            field.setText(repr(value))
            field.setCursorPosition(0)
            label.setText(name)
            label.setBuddy(field)
            field.setAccessibleName(f"{self._accessible_prefix} {name.lower()}")
            blocker.unblock()
        self.summary.setText("   ·   ".join(
            f"{name} {value:.6g}" for name, value in zip(summary_names, summary_values, strict=True)
        ))
        self.summary.setToolTip("   ·   ".join(
            f"{name} {value!r}" for name, value in zip(summary_names, summary_values, strict=True)
        ))

    def restore(self) -> None:
        self.set_limits(*self._limits)

    def unchanged(self) -> bool:
        try:
            return tuple(float(field.text()) for field in self._fields) == self._displayed_values
        except ValueError:
            return False

    def limits_from_fields(self) -> tuple[float, float]:
        """Validate input and convert center/span to finite, increasing bounds."""
        if not all(field.hasAcceptableInput() for field in self._fields):
            raise ValueError("Invalid numeric input")
        first, second = (float(field.text()) for field in self._fields)
        if not all(isfinite(value) for value in (first, second)):
            raise ValueError("Range values must be finite")
        if self.unchanged():
            # An odd number of ulps between endpoints may have no exact double
            # midpoint. Accepting unchanged fields must retain both endpoints.
            return self._limits
        if self.center_span:
            if second <= 0:
                raise ValueError("Span must be positive")
            lower, upper = first - second / 2, first + second / 2
        else:
            lower, upper = first, second
        if not all(isfinite(value) for value in (lower, upper)) or lower >= upper:
            raise ValueError("Range must have finite, increasing endpoints")
        return lower, upper
