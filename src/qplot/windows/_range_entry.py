"""Exact numeric editing and compact presentation of axis limits."""

from math import floor, isfinite, log10

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


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
