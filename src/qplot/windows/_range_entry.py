"""Shared input-format selector for axis and color-scale ranges."""

from collections.abc import Callable
from math import floor, isfinite, log10

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


class AutoLimitsButton(qtw.QToolButton):
    """Use the same fit-to-data symbol and tooltip behaviour in range editors."""

    def __init__(self, refresh_tooltip: Callable[[], None], parent: qtw.QWidget):
        super().__init__(parent)
        self._refresh_tooltip = refresh_tooltip
        self.setFixedSize(26, 24)
        self.setAutoRaise(True)

    def event(self, event: QtCore.QEvent | None) -> bool:
        if event is not None and event.type() == QtCore.QEvent.Type.ToolTip:
            self._refresh_tooltip()
        return super().event(event)

    def paintEvent(self, event: QtGui.QPaintEvent | None) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        group = (QtGui.QPalette.ColorGroup.Active if self.isEnabled()
                 else QtGui.QPalette.ColorGroup.Disabled)
        painter.setPen(QtGui.QPen(self.palette().color(group, QtGui.QPalette.ColorRole.ButtonText), 1.4))
        # QRect's integer centre rounds down on this even-sized button.
        painter.translate(QtCore.QRectF(self.rect()).center())
        for x1, y1, x2, y2 in (
            (-8, -5, -8, 5), (8, -5, 8, 5), (-5, 0, 5, 0),
            (-5, 0, -2, -3), (-5, 0, -2, 3),
            (5, 0, 2, -3), (5, 0, 2, 3),
        ):
            painter.drawLine(QtCore.QLineF(x1, y1, x2, y2))
        painter.end()


def layout_range_fields(layout, entry, manual, first, second, first_label, second_label, auto_limits):
    """Arrange the common format, limits and summary rows for both editors."""
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setHorizontalSpacing(6)
    layout.setVerticalSpacing(3)
    for field, label in ((first, first_label), (second, second_label)):
        field.setMinimumWidth(field.fontMetrics().horizontalAdvance("0.888888888888888888") + 12)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignBottom)
    layout.addWidget(entry, 0, 1, 1, 2,
                     QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop)
    layout.setRowMinimumHeight(0, entry.sizeHint().height() + 10)
    layout.addWidget(first_label, 1, 1)
    layout.addWidget(second_label, 1, 2)
    layout.addWidget(manual, 2, 0)
    layout.addWidget(first, 2, 1)
    layout.addWidget(second, 2, 2)
    layout.addWidget(auto_limits, 2, 3)
    entry.summary.setContentsMargins(0, 0, 0, 6)
    layout.addWidget(entry.summary, 3, 1, 1, 3)
    layout.setColumnStretch(1, 1)
    layout.setColumnStretch(2, 1)


def _center_span(lower: float, upper: float) -> tuple[float, float]:
    span = upper - lower
    # Avoid overflowing the sum of large, same-sign endpoints. Using the sum
    # near zero also avoids halving subnormal endpoints individually.
    center = (lower + upper) / 2 if lower <= 0 <= upper else lower + span / 2
    return center, span


def _compact_range_numbers(values: tuple[float, float]) -> tuple[str, str]:
    """Use six significant figures, retaining visibly distinct nearby limits."""
    def format_value(value, precision):
        if not isfinite(value):
            return f"{value:.{precision}g}"
        if value == 0:
            return "0"
        exponent = floor(log10(abs(value)))
        if -6 <= exponent < 6:
            text = f"{value:.{max(0, precision - 1 - exponent)}f}"
            return text.rstrip("0").rstrip(".") if "." in text else text
        return f"{value:.{precision}g}"

    for precision in range(6, 18):
        first, second = (format_value(value, precision) for value in values)
        if values[0] == values[1] or first != second:
            return first, second
    return repr(values[0]), repr(values[1])


class CompactNumberEdit(qtw.QLineEdit):
    """Paint compact values at rest; keep exact text for editing and validation.

    Presentation never replaces the QLineEdit text, so accepting an untouched
    limit, copying it or changing the other limit cannot commit rounded data.
    Drafts and selections retain the native line edit presentation.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact_source = ""
        self._compact_text = ""

    def set_compact_text(self, text: str) -> None:
        self._compact_source = self.text()
        self._compact_text = text
        self.update()

    def presentation_text(self) -> str:
        if not self.hasFocus() and not self.hasSelectedText() and self.text() == self._compact_source:
            return self._compact_text
        return self.text()

    def paintEvent(self, event: QtGui.QPaintEvent | None) -> None:
        text = self.presentation_text()
        if text == self.text():
            super().paintEvent(event)
            return
        option = qtw.QStyleOptionFrame()
        self.initStyleOption(option)
        painter = qtw.QStylePainter(self)
        painter.drawPrimitive(qtw.QStyle.PrimitiveElement.PE_PanelLineEdit, option)
        style = self.style()
        assert style is not None
        rect = style.subElementRect(qtw.QStyle.SubElement.SE_LineEditContents, option, self)
        margins = self.textMargins()
        rect.adjust(margins.left() + 2, margins.top(), -margins.right() - 2, -margins.bottom())
        group = QtGui.QPalette.ColorGroup.Active if self.isEnabled() else QtGui.QPalette.ColorGroup.Disabled
        painter.setPen(self.palette().color(group, QtGui.QPalette.ColorRole.Text))
        painter.setClipRect(rect)
        painter.drawText(rect, self.alignment() | QtCore.Qt.AlignmentFlag.AlignVCenter, text)


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
        self._compact_numbers = all(isinstance(field, CompactNumberEdit) for field in self._fields)
        self._labels = first_label, second_label
        self._manual_styles = {
            widget: widget.styleSheet() for widget in (*self._fields, *self._labels)
        }
        self._automatic: bool | None = None
        self._accessible_prefix = accessible_prefix
        self._limits = (0.0, 1.0)
        self._displayed_values = self._limits
        self.center_span = False
        self._error = ""
        self._dirty = False
        for field in self._fields:
            field.textEdited.connect(self._draft_edited)
        self.summary = qtw.QLabel(parent)
        self.summary.setObjectName("rangeEntrySummary")
        if not self._compact_numbers:
            palette = self.summary.palette()
            palette.setColor(
                QtGui.QPalette.ColorRole.WindowText,
                palette.color(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.WindowText),
            )
            self.summary.setPalette(palette)
        self.summary.setWordWrap(True)
        font = self.summary.font()
        if not self._compact_numbers:
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

    def sizeHint(self) -> QtCore.QSize:
        size = super().sizeHint()
        # Equal stretch needs enough room for the longer label in both halves.
        # Derive this from the current font/style so it also follows text scaling.
        size.setWidth(2 * max(self.min_max_button.sizeHint().width(),
                             self.center_span_button.sizeHint().width()))
        return size

    def minimumSizeHint(self) -> QtCore.QSize:
        return self.sizeHint()

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
        """Keep automatic values visibly editable."""
        if self._automatic == automatic:
            return
        self._automatic = automatic
        for widget, original_style in self._manual_styles.items():
            widget.setStyleSheet(original_style)

    def _draft_edited(self, _text: str) -> None:
        self._dirty = True

    def show_error(self, message: str) -> None:
        self._error = message
        self._dirty = True
        self.summary.setText(message)
        self.summary.setAccessibleName(message)
        for field in self._fields:
            field.setStyleSheet(self._manual_styles[field] + "QLineEdit { border: 2px solid #c2410c; }")

    def clear_error(self) -> None:
        self._error = ""
        self._dirty = False
        for field in self._fields:
            field.setStyleSheet(self._manual_styles[field])
        self.summary.setAccessibleName("")

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
        if bool(index) == self.center_span:
            return
        try:
            if self._error:
                raise ValueError(self._error)
            self.limits_from_fields()
        except ValueError:
            for button, checked in ((self.min_max_button, not self.center_span),
                                    (self.center_span_button, self.center_span)):
                with QtCore.QSignalBlocker(button):
                    button.setChecked(checked)
            message = self._error or self.validation_message()
            hint = " Correct the values before changing input format."
            self.show_error(message if hint in message else message + hint)
            return
        self.clear_error()
        self.center_span = bool(index)
        self.set_limits(*self._limits)

    def validation_message(self) -> str:
        if self.center_span:
            return "Enter a finite centre and a positive, finite span."
        return "Enter finite limits with minimum below maximum."

    def set_limits(self, lower: float, upper: float) -> None:
        """Synchronize the displayed representation from canonical endpoints."""
        if (self._dirty and any(field.hasFocus() for field in self._fields)) or self._error:
            return
        self._dirty = False
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
        compact_texts = _compact_range_numbers(values) if self._compact_numbers else ("", "")
        for field, label, name, value, compact_text in zip(
            self._fields, self._labels, names, values, compact_texts, strict=True,
        ):
            blocker = QtCore.QSignalBlocker(field)
            # Keep the shortest spelling that round-trips to the same float.
            field.setText(repr(value))
            if isinstance(field, CompactNumberEdit):
                field.set_compact_text(compact_text)
            field.setCursorPosition(0)
            label.setText(name)
            label.setBuddy(field)
            field.setAccessibleName(f"{self._accessible_prefix} {name.lower()}")
            blocker.unblock()
        summary_texts = (_compact_range_numbers(summary_values) if self._compact_numbers
                         else tuple(f"{value:.6g}" for value in summary_values))
        self.summary.setText("   ·   ".join(
            f"{name} {text}" for name, text in zip(summary_names, summary_texts, strict=True)
        ))
        self.summary.setToolTip("   ·   ".join(
            f"{name} {value!r}" for name, value in zip(summary_names, summary_values, strict=True)
        ))

    def restore(self) -> None:
        self.clear_error()
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
