"""Shared widgets used by plot-item appearance dialogs."""

from __future__ import annotations

from typing import Any

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


class CenteredPreviewDelegate(qtw.QStyledItemDelegate):
    """Paint an icon-only plot preview in the centre of its table cell."""

    def paint(self, painter, option, index):
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        if not isinstance(icon, QtGui.QIcon) or icon.isNull():
            super().paint(painter, option, index)
            return

        styled_option = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(styled_option, index)
        styled_option.text = ""
        styled_option.icon = QtGui.QIcon()

        widget = styled_option.widget
        style = widget.style() if widget else qtw.QApplication.style()
        if style is None:
            super().paint(painter, option, index)
            return
        style.drawControl(
            qtw.QStyle.ControlElement.CE_ItemViewItem,
            styled_option,
            painter,
            widget,
        )

        icon_size = icon.actualSize(styled_option.rect.size())
        icon_rect = QtCore.QRect(QtCore.QPoint(), icon_size)
        icon_rect.moveCenter(styled_option.rect.center())
        icon.paint(painter, icon_rect, QtCore.Qt.AlignmentFlag.AlignCenter)


class ReorderAppearanceTable(qtw.QTableWidget):
    """Three-column appearance table with internal multi-row reordering."""

    def __init__(
        self,
        dialog: Any,
        *,
        mime_type: str,
    ) -> None:
        super().__init__(0, 3, dialog)
        self.dialog = dialog
        self._reorder_mime_type = mime_type
        self._dragged_rows: list[int] = []
        self._drag_origin = QtCore.QModelIndex()
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._drop_indicator = qtw.QWidget(self.viewport())
        self._drop_indicator.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._drop_indicator.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_StyledBackground
        )
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet(
            "background-color: palette(highlight);"
        )
        self._drop_indicator.hide()

    def selectRow(self, row: int) -> None:
        """Select a whole row reliably before the table has been displayed."""

        super().selectRow(row)
        selection_model = self.selectionModel()
        model = self.model()
        if selection_model is None or model is None:
            return
        index = model.index(row, 0)
        if not index.isValid():
            return
        selection_model.select(
            index,
            QtCore.QItemSelectionModel.SelectionFlag.Select
            | QtCore.QItemSelectionModel.SelectionFlag.Rows,
        )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_origin = self.indexAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._drag_origin = QtCore.QModelIndex()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        self._update_reorder_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.viewport().unsetCursor()
        super().leaveEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if self._is_internal_reorder_drag(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if self._is_internal_reorder_drag(event):
            self._show_drop_indicator(
                self._drop_insertion_row(event.position().toPoint())
            )
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self._hide_drop_indicator()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        self._hide_drop_indicator()
        if not self._is_internal_reorder_drag(event):
            event.ignore()
            return

        source_rows = self._dragged_rows
        self._dragged_rows = []
        if not source_rows:
            event.ignore()
            return

        self.dialog._move_rows_to_position(
            source_rows,
            self._drop_insertion_row(event.position().toPoint()),
        )
        event.acceptProposedAction()

    def startDrag(self, supported_actions):
        del supported_actions
        index = self._drag_origin
        if not index.isValid():
            index = self.currentIndex()
        if not index.isValid() or index.column() == self.dialog._COL_PREVIEW:
            return

        source_rows = self.dialog._selected_rows()
        if not source_rows:
            return

        mime_data = QtCore.QMimeData()
        mime_data.setData(self._reorder_mime_type, QtCore.QByteArray())
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime_data)
        self._dragged_rows = source_rows
        try:
            drag.exec(QtCore.Qt.DropAction.MoveAction)
        finally:
            self._dragged_rows = []
            self._hide_drop_indicator()

    def _is_internal_reorder_drag(self, event: QtGui.QDropEvent) -> bool:
        return (
            event.source() is self
            and event.mimeData().hasFormat(self._reorder_mime_type)
        )

    def _drop_insertion_row(self, position: QtCore.QPoint) -> int:
        index = self.indexAt(position)
        if not index.isValid():
            return self.rowCount()
        if position.y() > self.visualRect(index).center().y():
            return index.row() + 1
        return index.row()

    def _update_reorder_cursor(self, position: QtCore.QPoint) -> None:
        index = self.indexAt(position)
        if index.isValid() and index.column() != self.dialog._COL_PREVIEW:
            self.viewport().setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
            return
        self.viewport().unsetCursor()

    def _show_drop_indicator(self, destination_row: int) -> None:
        if not self.rowCount():
            self._hide_drop_indicator()
            return

        destination_row = max(0, min(destination_row, self.rowCount()))
        if destination_row == self.rowCount():
            index = self.model().index(self.rowCount() - 1, 0)
            y_position = self.visualRect(index).bottom() + 1
        else:
            index = self.model().index(destination_row, 0)
            y_position = self.visualRect(index).top()
        self._drop_indicator.setGeometry(
            0,
            max(0, y_position - 1),
            self.viewport().width(),
            self._drop_indicator.height(),
        )
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def _hide_drop_indicator(self) -> None:
        self._drop_indicator.hide()


def configure_appearance_table(
    table: qtw.QTableWidget,
    dialog: Any,
    *,
    object_name: str,
    item_name: str,
) -> None:
    """Apply the common appearance-table layout and interaction policy."""

    table.setObjectName(object_name)
    table.setHorizontalHeaderLabels(["ID", "Preview", "Measurement"])
    table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
    table.setIconSize(QtCore.QSize(64, 22))
    table.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    table.setToolTip(f"Drag a {item_name} up or down to change its order.")
    table.setDragEnabled(True)
    table.setAcceptDrops(True)
    table.viewport().setAcceptDrops(True)
    table.setDragDropMode(qtw.QAbstractItemView.DragDropMode.DragDrop)
    table.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
    table.setDropIndicatorShown(False)
    table.setItemDelegateForColumn(
        dialog._COL_PREVIEW,
        CenteredPreviewDelegate(table),
    )

    horizontal_header = table.horizontalHeader()
    if horizontal_header is not None:
        header_font = horizontal_header.font()
        header_font.setBold(False)
        horizontal_header.setFont(header_font)
        for column in range(table.columnCount()):
            header_item = table.horizontalHeaderItem(column)
            if header_item is not None:
                item_font = header_item.font()
                item_font.setBold(False)
                header_item.setFont(item_font)
        horizontal_header.setFixedHeight(24)
        horizontal_header.setSectionResizeMode(
            dialog._COL_ID,
            qtw.QHeaderView.ResizeMode.ResizeToContents,
        )
        horizontal_header.setSectionResizeMode(
            dialog._COL_PREVIEW,
            qtw.QHeaderView.ResizeMode.ResizeToContents,
        )
        horizontal_header.setSectionResizeMode(
            dialog._COL_MEASUREMENT,
            qtw.QHeaderView.ResizeMode.Stretch,
        )

    vertical_header = table.verticalHeader()
    if vertical_header is not None:
        vertical_header.setVisible(False)
        vertical_header.setDefaultSectionSize(28)
        vertical_header.setMinimumSectionSize(24)
