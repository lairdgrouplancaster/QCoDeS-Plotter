from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._run_formatting import run_tooltip_text
from .preview import DraggablePreviewImageLabel


MEASUREMENT_PREVIEW_SIZE = 22
MEASUREMENT_PREVIEW_SPACING = 3


class RunPreviewCell(qtw.QWidget):
    plotRequested = QtCore.pyqtSignal(str, str)
    exportRequested = QtCore.pyqtSignal(str, str)

    def __init__(self, guid, count, parent=None, icon_size=MEASUREMENT_PREVIEW_SIZE):
        super().__init__(parent)
        self.guid = guid
        self.placeholder_count = max(0, int(count or 0))
        self.icon_size = int(icon_size)

        self.content_layout = qtw.QHBoxLayout()
        self.content_layout.setContentsMargins(2, 0, 2, 0)
        self.content_layout.setSpacing(MEASUREMENT_PREVIEW_SPACING)
        self.content_layout.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft
            | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
        self.setLayout(self.content_layout)
        self.setFixedHeight(self.icon_size + 6)
        self.show_placeholders()


    def show_placeholders(self, count=None):
        self._clear_layout()
        placeholder_count = self.placeholder_count if count is None else max(0, int(count))
        for _ in range(placeholder_count):
            self.content_layout.addWidget(self._placeholder_label())
        self.content_layout.addStretch()


    def show_previews(self, previews):
        self._clear_layout()

        preview_count = 0
        for preview in previews or []:
            image = preview.get("image")
            if image is None:
                continue

            label = DraggablePreviewImageLabel(
                self.guid,
                preview.get("parameter", ""),
                preview.get("axes") or [],
                )
            label.setObjectName("measurementPreviewImage")
            label.setFixedSize(self.icon_size, self.icon_size)
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(preview.get("title", ""))
            label.setPixmap(
                QtGui.QPixmap.fromImage(image).scaled(
                    self.icon_size,
                    self.icon_size,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                    )
                )
            label.plotRequested.connect(self._emit_plot_requested)
            label.exportRequested.connect(self._emit_export_requested)
            self.content_layout.addWidget(label)
            preview_count += 1

        for _ in range(max(0, self.placeholder_count - preview_count)):
            self.content_layout.addWidget(self._placeholder_label())
        self.content_layout.addStretch()


    def _placeholder_label(self):
        label = qtw.QLabel()
        label.setObjectName("measurementPreviewPlaceholder")
        label.setFixedSize(self.icon_size, self.icon_size)
        label.setFrameShape(qtw.QFrame.Shape.Box)
        label.setFrameShadow(qtw.QFrame.Shadow.Plain)
        label.setLineWidth(1)
        return label


    def _emit_plot_requested(self, parameter):
        self.plotRequested.emit(self.guid, parameter)


    def _emit_export_requested(self, parameter):
        self.exportRequested.emit(self.guid, parameter)


    def _clear_layout(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


class EqualsAlignedDelegate(qtw.QStyledItemDelegate):
    """
    Paints setpoint counts with equals signs aligned.
    """

    right_text_alignment = (
        QtCore.Qt.AlignmentFlag.AlignLeft
        | QtCore.Qt.AlignmentFlag.AlignVCenter
        )

    def paint(self, painter, option, index):
        text = index.data(QtCore.Qt.ItemDataRole.DisplayRole)
        if text is None or text == "":
            super().paint(painter, option, index)
            return

        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = str(text)
        sections = self._display_sections(text)
        if sections is None:
            super().paint(painter, option, index)
            return
        left, right = sections

        opt.text = ""

        widget = opt.widget
        style = widget.style() if widget else qtw.QApplication.style()
        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(
            qtw.QStyle.SubElement.SE_ItemViewItemText,
            opt,
            widget
            ).adjusted(2, 0, -2, 0)
        metrics = opt.fontMetrics
        equals_text = " = "
        equals_width = metrics.horizontalAdvance(equals_text)
        max_right_width = self._max_right_width(index, metrics)
        if right is None and max_right_width <= 0:
            painter.save()
            painter.setFont(opt.font)
            painter.setPen(self._text_color(opt))
            painter.drawText(
                text_rect,
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(left, QtCore.Qt.TextElideMode.ElideLeft, text_rect.width())
                )
            painter.restore()
            return

        equals_left = max(
            text_rect.left(),
            text_rect.right() - max_right_width - equals_width
            )

        left_rect = QtCore.QRect(
            text_rect.left(),
            text_rect.top(),
            max(0, equals_left - text_rect.left()),
            text_rect.height()
            )
        equals_rect = QtCore.QRect(
            equals_left,
            text_rect.top(),
            equals_width,
            text_rect.height()
            )
        right_rect = QtCore.QRect(
            equals_left + equals_width,
            text_rect.top(),
            max(0, text_rect.right() - equals_left - equals_width + 1),
            text_rect.height()
            )

        painter.save()
        painter.setFont(opt.font)
        painter.setPen(self._text_color(opt))
        painter.drawText(
            left_rect,
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(left, QtCore.Qt.TextElideMode.ElideLeft, left_rect.width())
            )
        if right is not None:
            painter.drawText(equals_rect, QtCore.Qt.AlignmentFlag.AlignCenter, equals_text)
            self._draw_right_text(painter, right_rect, right, metrics)
        painter.restore()


    def _display_sections(self, text):
        if " = " in text:
            return str(text).split(" = ", 1)
        if self._is_count_text(text):
            return str(text), None
        return None


    def _is_count_text(self, text):
        try:
            int(str(text).replace(",", ""))
        except (TypeError, ValueError):
            return False
        return True


    def _draw_right_text(self, painter, right_rect, right, metrics):
        painter.drawText(
            right_rect,
            self.right_text_alignment,
            metrics.elidedText(right, QtCore.Qt.TextElideMode.ElideRight, right_rect.width())
            )


    def _max_right_width(self, index, metrics):
        view = self.parent()
        if not isinstance(view, qtw.QTreeWidget):
            return 0

        max_width = 0
        column = index.column()
        for row in range(view.topLevelItemCount()):
            item = view.topLevelItem(row)
            if item is None:
                continue

            sections = self._display_sections(item.text(column))
            if sections is None:
                continue

            _, right = sections
            if right is None:
                continue

            max_width = max(max_width, metrics.horizontalAdvance(right))
        return max_width


    def _text_color(self, option):
        return option.palette.color(QtGui.QPalette.ColorRole.Text)


class SortableTreeWidgetItem(qtw.QTreeWidgetItem):
    """
    QTreeWidgetItem with an overridden comparator that sorts numerical values
    as numbers instead of sorting them alphabetically.
    """
    def __init__(self, strings):
        super().__init__(strings)
        self.run_metadata = {}
        self._guid = ""

    def __lt__(self, other: qtw.QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn()
        value1 = self.data(col, QtCore.Qt.ItemDataRole.UserRole)
        value2 = other.data(col, QtCore.Qt.ItemDataRole.UserRole)
        if value1 is not None and value2 is not None:
            try:
                return float(value1) < float(value2)
            except (TypeError, ValueError):
                pass

        text1 = self.text(col)
        text2 = other.text(col)
        try:
            return float(text1) < float(text2)
        except ValueError:
            return text1 < text2

    @property
    def guid(self):
        return self._guid


    def set_guid(self, guid):
        self._guid = guid


    def update_tooltip(self):
        tooltip = run_tooltip_text(self.run_metadata)
        for col in range(self.columnCount()):
            self.setToolTip(col, tooltip)
