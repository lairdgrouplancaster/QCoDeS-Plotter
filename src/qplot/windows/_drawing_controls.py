"""Compact trace drawing controls with wrapping and colour descriptions."""

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._plot_appearance import compact_spinbox_size_hint

TRACE_COLOR_PALETTE = (
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
    "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#000000", "#ffffff",
)
LINE_STYLES = (
    ("Solid", QtCore.Qt.PenStyle.SolidLine),
    ("Dash", QtCore.Qt.PenStyle.DashLine),
    ("Dot", QtCore.Qt.PenStyle.DotLine),
    ("Dash Dot", QtCore.Qt.PenStyle.DashDotLine),
)


def colour_swatch_icon(colour, width=38):
    pixmap = QtGui.QPixmap(width, 16)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setPen(QtGui.QPen(QtGui.QColor("#444444")))
    painter.setBrush(QtGui.QBrush(QtGui.QColor(colour)))
    painter.drawRoundedRect(1, 1, width - 2, 14, 2, 2)
    painter.end()
    return QtGui.QIcon(pixmap)


def line_style_icon(style, palette, width=54):
    pixmap = QtGui.QPixmap(width, 16)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(palette.color(QtGui.QPalette.ColorRole.Text))
    pen.setWidthF(2.0)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setStyle(dict(LINE_STYLES).get(style, QtCore.Qt.PenStyle.SolidLine))
    painter.setPen(pen)
    painter.drawLine(4, pixmap.height() // 2, pixmap.width() - 4, pixmap.height() // 2)
    painter.end()
    return QtGui.QIcon(pixmap)


class DrawingSizeSpinBox(qtw.QDoubleSpinBox):
    def sizeHint(self):
        return compact_spinbox_size_hint(self, super().sizeHint())

    def minimumSizeHint(self):
        return self.sizeHint()


class IconPreviewCombo(qtw.QComboBox):
    """Show the selected preview with descriptive tooltips and menu entries."""

    def __init__(self, name, *, preview_renderer=None):
        super().__init__()
        self._preview_renderer = preview_renderer
        self.setAccessibleName(name)
        self.setIconSize(QtCore.QSize(26, 16))
        self.setStyleSheet("QComboBox { padding: 0 2px; }")
        self.setSizePolicy(qtw.QSizePolicy.Policy.Fixed, qtw.QSizePolicy.Policy.Fixed)
        self.currentIndexChanged.connect(self.refresh_tooltip)

    def refresh_tooltip(self):
        value = self.currentText() or "Mixed"
        self.setToolTip(f"{self.accessibleName()}: {value}")
        self.setAccessibleDescription(value)

    def sizeHint(self):
        option = qtw.QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = ""
        contents = QtCore.QSize(max(self.iconSize().width(), self.fontMetrics().horizontalAdvance("Mixed")),
                               max(self.iconSize().height(), self.fontMetrics().height()))
        return self.style().sizeFromContents(qtw.QStyle.ContentsType.CT_ComboBox, option, contents, self)

    def minimumSizeHint(self):
        return self.sizeHint()

    def showPopup(self):
        super().showPopup()
        view = self.view()
        popup = view.window()
        width = view.sizeHintForColumn(self.modelColumn()) + popup.width() - view.viewport().width()
        popup.setMinimumWidth(width)
        popup.resize(width, popup.height())
        available = self.screen().availableGeometry()
        if popup.frameGeometry().right() > available.right():
            popup.move(max(available.left(), available.right() - popup.width() + 1), popup.y())

    def paintEvent(self, event):
        option = qtw.QStyleOptionComboBox()
        self.initStyleOption(option)
        if self.currentIndex() < 0:
            option.currentText = self.placeholderText()
            option.currentIcon = QtGui.QIcon()
            option.palette.setBrush(QtGui.QPalette.ColorRole.ButtonText,
                                    self.palette().brush(QtGui.QPalette.ColorRole.PlaceholderText))
        elif not option.currentIcon.isNull():
            option.currentText = ""
            if self._preview_renderer is not None:
                contents = self.style().subControlRect(
                    qtw.QStyle.ComplexControl.CC_ComboBox, option,
                    qtw.QStyle.SubControl.SC_ComboBoxEditField, self,
                )
                width = max(10, contents.width() - 4)
                option.iconSize = QtCore.QSize(width, self.iconSize().height())
                option.currentIcon = self._preview_renderer(self.currentData(), width)
        painter = qtw.QStylePainter(self)
        painter.drawComplexControl(qtw.QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(qtw.QStyle.ControlElement.CE_ComboBoxLabel, option)


class ColourSwatchCombo(IconPreviewCombo):
    def refresh_tooltip(self):
        colour = QtGui.QColor(str(self.currentData() or ""))
        value = colour.name() if colour.isValid() else self.currentText() or "Mixed"
        self.setToolTip(f"{self.accessibleName()}: {value}\nChoose a preset colour or Custom.")
        self.setAccessibleDescription(value)


class _WrappingLayout(qtw.QLayout):
    """Lay controls out at their natural sizes, wrapping when necessary."""

    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), apply=False)

    def sizeHint(self):
        sizes = [item.sizeHint().expandedTo(item.minimumSize()) for item in self._items]
        return QtCore.QSize(sum(size.width() for size in sizes) + self.spacing() * max(0, len(sizes) - 1),
                           max((size.height() for size in sizes), default=0))

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def _arrange(self, rect, *, apply):
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self._items:
            size = item.sizeHint().expandedTo(item.minimumSize())
            if row_height and x + size.width() > rect.right() + 1:
                x = rect.x()
                y += row_height + self.spacing()
                row_height = 0
            if apply:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), size))
            x += size.width() + self.spacing()
            row_height = max(row_height, size.height())
        return y + row_height - rect.y()


def drawing_controls_row(*controls):
    row = qtw.QWidget()
    layout = _WrappingLayout(row)
    for control in controls:
        layout.addWidget(control)
    return row
