from PyQt5 import QtCore, QtGui
from PyQt5 import QtWidgets as qtw

import pyqtgraph as pg

from ._widgets import CopyableTableWidget


class PlotMarqueeMixin:
    """
    Shared marquee selection behavior for plot windows.

    Plot type subclasses can customise selection snapping, statistics, and
    context-menu entries by overriding the small hook methods near the end of
    this mixin.
    """

    def _init_marquee(self):
        """
        Create the reusable marquee graphics shown after Alt-dragging.

        """
        self.marquee = None
        self._marquee_drag_state = None

        self.marquee_highlight = qtw.QGraphicsRectItem()
        highlight_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 135))
        highlight_pen.setWidthF(3)
        highlight_pen.setCosmetic(True)
        self.marquee_highlight.setPen(highlight_pen)
        self.marquee_highlight.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self.marquee_highlight.setZValue(18)
        self.marquee_highlight.hide()
        self.marquee_highlight.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.plot.addItem(self.marquee_highlight)

        self.marquee_outline = qtw.QGraphicsRectItem()
        pen = QtGui.QPen(QtGui.QColor(65, 65, 65, 220))
        pen.setWidthF(1.2)
        pen.setCosmetic(True)
        pen.setStyle(QtCore.Qt.DashLine)
        self.marquee_outline.setPen(pen)
        self.marquee_outline.setBrush(QtGui.QBrush(QtGui.QColor(40, 40, 40, 24)))
        self.marquee_outline.setZValue(19)
        self.marquee_outline.hide()
        self.marquee_outline.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.plot.addItem(self.marquee_outline)

        self.marquee_handles = pg.ScatterPlotItem(
            symbol="s",
            size=6,
            pen=pg.mkPen((20, 20, 20, 230), width=1, cosmetic=True),
            brush=pg.mkBrush(245, 245, 245, 230),
            )
        self.marquee_handles.setZValue(20)
        self.marquee_handles.hide()
        self.marquee_handles.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.plot.addItem(self.marquee_handles)

    def is_marquee_dragging(self):
        return self._marquee_drag_state is not None

    def current_marquee_drag_mode(self):
        if self._marquee_drag_state is None:
            return None

        return self._marquee_drag_state["mode"]

    def begin_marquee_drag(self, start, mode=None):
        """
        Start creating or resizing a marquee in plot coordinates.

        """
        start = QtCore.QPointF(start)
        if mode is None:
            mode = "new"

        handle_offset = QtCore.QPointF()
        if mode != "new" and self.marquee is not None:
            handle_point = self._marquee_handle_points_for_rect(self.marquee)[mode]
            handle_offset = start - handle_point

        self._marquee_drag_state = {
            "anchor": QtCore.QPointF(start),
            "handle_offset": QtCore.QPointF(handle_offset),
            "mode": mode,
            "rect": QtCore.QRectF(self.marquee) if self.marquee is not None else None,
            }

        if mode == "new":
            self.set_marquee_rect(QtCore.QRectF(start, start))

    def drag_marquee_to(self, point, modifiers=QtCore.Qt.NoModifier):
        """
        Update the marquee during an active drag.

        """
        point = QtCore.QPointF(point)
        state = self._marquee_drag_state
        if state is None:
            return

        if state["mode"] == "new" or state["rect"] is None:
            rect = QtCore.QRectF(state["anchor"], point)
        else:
            rect = QtCore.QRectF(state["rect"])
            offset = state.get("handle_offset", QtCore.QPointF())
            point = QtCore.QPointF(
                point.x() - offset.x(),
                point.y() - offset.y(),
                )
            self._resize_marquee_rect(rect, state["mode"], point, modifiers)

        self.set_marquee_rect(rect)

    def finish_marquee_drag(self):
        self._marquee_drag_state = None

    def marquee_contains_scene_pos(self, scene_pos):
        """
        Return whether a scene position is inside the current marquee.

        """
        if self.marquee is None:
            return False

        point = self.plot.vb.mapSceneToView(scene_pos)
        return self.marquee.normalized().contains(point)

    def open_marquee_context_menu(self, scene_pos, global_pos=None):
        """
        Open the marquee context menu when a right-click lands inside it.

        """
        if not self.marquee_contains_scene_pos(scene_pos):
            return False

        menu = self._new_marquee_context_menu()
        if global_pos is None:
            global_pos = QtGui.QCursor.pos()
        elif isinstance(global_pos, QtCore.QPointF):
            global_pos = global_pos.toPoint()

        menu.exec_(global_pos)
        return True

    def _new_marquee_context_menu(self):
        menu = qtw.QMenu()
        if hasattr(menu, "setToolTipsVisible"):
            menu.setToolTipsVisible(True)

        self._add_marquee_context_action(
            menu,
            "Zoom",
            lambda: self.zoom_marquee("xy"),
            )
        self._add_marquee_context_action(
            menu,
            "Zoom X",
            lambda: self.zoom_marquee("x"),
            )
        self._add_marquee_context_action(
            menu,
            "Zoom Y",
            lambda: self.zoom_marquee("y"),
            )
        self._add_marquee_color_context_action(menu)
        self._add_marquee_stats_context_action(menu)
        return menu

    def _add_marquee_context_action(self, menu, text, callback):
        action = menu.addAction(text)
        action.triggered.connect(
            lambda _checked=False, callback=callback: self._execute_marquee_action(
                callback
                )
            )
        return action

    def _add_marquee_color_context_action(self, menu):
        return None

    def _add_marquee_stats_context_action(self, menu):
        stats_text = self._marquee_stats_text()
        action = self._add_marquee_context_action(
            menu,
            "Stats...",
            lambda stats_text=stats_text: self.show_marquee_stats_dialog(stats_text),
            )
        if stats_text is None:
            action.setEnabled(False)
            action.setToolTip("No data points inside the marquee.")
        else:
            action.setToolTip("Show statistics for the marquee selection.")
            action.setStatusTip("Show statistics for the marquee selection.")
        return action

    def _execute_marquee_action(self, callback):
        if callback():
            self.clear_marquee()

    def zoom_marquee(self, axes):
        rect = self.marquee.normalized() if self.marquee is not None else None
        if rect is None:
            return False

        if "x" in axes:
            self.vb.setXRange(rect.left(), rect.right(), padding=0)
        if "y" in axes:
            self.vb.setYRange(rect.top(), rect.bottom(), padding=0)
        return True

    def _marquee_stats_text(self):
        return None

    def show_marquee_stats_dialog(self, stats_text=None):
        if stats_text is None:
            stats_text = self._marquee_stats_text()
        if stats_text is None:
            return False

        dialog = self._new_marquee_stats_dialog(stats_text)
        self._marquee_stats_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True

    def _new_marquee_stats_dialog(self, stats_text):
        dialog = qtw.QDialog(self)
        dialog.setWindowTitle("Marquee stats")

        layout = qtw.QVBoxLayout(dialog)
        stats_table = self._new_marquee_stats_table(stats_text)
        layout.addWidget(stats_table)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.Close)
        copy_button = buttons.addButton("Copy", qtw.QDialogButtonBox.ActionRole)

        def copy_stats(_checked=False):
            self.copy_marquee_stats_to_clipboard(stats_text)

        copy_button.clicked.connect(copy_stats)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)

        return dialog

    def _new_marquee_stats_table(self, stats_text):
        rows = self._marquee_stats_table_rows(stats_text)
        stats_table = CopyableTableWidget()
        stats_table.setObjectName("detailsTable")
        stats_table.setColumnCount(2)
        stats_table.setHorizontalHeaderLabels(["Field", "Value"])
        stats_table.setRowCount(len(rows))
        stats_table.setAlternatingRowColors(True)
        stats_table.setEditTriggers(qtw.QAbstractItemView.NoEditTriggers)
        stats_table.setSelectionBehavior(qtw.QAbstractItemView.SelectRows)
        stats_table.setSelectionMode(qtw.QAbstractItemView.ExtendedSelection)
        stats_table.setTextElideMode(QtCore.Qt.ElideNone)
        stats_table.setWordWrap(False)
        stats_table.verticalHeader().hide()
        stats_table.verticalHeader().setMinimumSectionSize(16)
        stats_table.verticalHeader().setDefaultSectionSize(20)
        stats_table.horizontalHeader().setFixedHeight(22)
        stats_table.horizontalHeader().setStretchLastSection(True)
        stats_table.horizontalHeader().setSectionResizeMode(
            0,
            qtw.QHeaderView.ResizeToContents
            )
        stats_table.horizontalHeader().setSectionResizeMode(
            1,
            qtw.QHeaderView.Stretch
            )
        stats_table.setMinimumWidth(280)
        stats_table.setMinimumHeight(170)

        for row, (field, value) in enumerate(rows):
            field_item = qtw.QTableWidgetItem(field)
            value_item = qtw.QTableWidgetItem(value)
            stats_table.setItem(row, 0, field_item)
            stats_table.setItem(row, 1, value_item)

        return stats_table

    def _marquee_stats_table_rows(self, stats_text):
        rows = []
        for line in stats_text.splitlines():
            line = line.strip()
            if not line:
                continue

            if ":" in line:
                field, value = line.split(":", 1)
                rows.append((field.strip(), value.strip()))
            else:
                rows.append(("Selection", line))

        return rows

    def _format_marquee_stats_text(self, size_text, values, rect=None):
        if rect is None and self.__dict__.get("marquee") is not None:
            rect = self.marquee.normalized()

        lines = [size_text]
        if rect is not None:
            lines.extend((
                f"X range: {self.formatNum(rect.left())} to {self.formatNum(rect.right())}",
                f"Y range: {self.formatNum(rect.top())} to {self.formatNum(rect.bottom())}",
                ))

        lines.extend((
            f"Average: {self.formatNum(float(values.mean()))}",
            f"Standard deviation: {self.formatNum(float(values.std()))}",
            f"Max: {self.formatNum(float(values.max()))}",
            f"Min: {self.formatNum(float(values.min()))}",
            ))

        return "\n".join(lines)

    def copy_marquee_stats_to_clipboard(self, stats_text=None):
        if stats_text is None:
            stats_text = self._marquee_stats_text()
        if stats_text is None:
            return False

        clipboard = qtw.QApplication.clipboard()
        if clipboard is None:
            return False

        clipboard.setText(stats_text)
        return True

    def _resize_marquee_rect(self, rect, handle, point, modifiers=QtCore.Qt.NoModifier):
        symmetric = bool(modifiers & QtCore.Qt.AltModifier)
        asymmetric = bool(modifiers & QtCore.Qt.ShiftModifier) and not symmetric
        original = QtCore.QRectF(rect)
        anchor = self._marquee_handle_points_for_rect(original)[handle]

        if "w" in handle:
            rect.setLeft(point.x())
        if "e" in handle:
            rect.setRight(point.x())
        if "s" in handle:
            rect.setTop(point.y())
        if "n" in handle:
            rect.setBottom(point.y())

        dx = point.x() - anchor.x()
        dy = point.y() - anchor.y()

        if ("w" in handle or "e" in handle) and (symmetric or asymmetric):
            offset = -dx if symmetric else dx if asymmetric else None
            if "w" in handle:
                rect.setRight(original.right() + offset)
            else:
                rect.setLeft(original.left() + offset)

        if ("n" in handle or "s" in handle) and (symmetric or asymmetric):
            offset = -dy if symmetric else dy if asymmetric else None
            if "s" in handle:
                rect.setBottom(original.bottom() + offset)
            else:
                rect.setTop(original.top() + offset)

        if asymmetric:
            self._snap_translated_marquee_rect(rect, original, handle)

    def _snap_translated_marquee_rect(self, rect, original, handle):
        snapped = self._snap_marquee_rect(QtCore.QRectF(rect).normalized())
        if snapped is None:
            return

        adjusted = QtCore.QRectF(snapped)
        if "w" in handle or "e" in handle:
            width = original.width()
            if "w" in handle:
                adjusted.setLeft(snapped.left())
                adjusted.setRight(snapped.left() + width)
            else:
                adjusted.setRight(snapped.right())
                adjusted.setLeft(snapped.right() - width)

        if "n" in handle or "s" in handle:
            height = original.height()
            if "s" in handle:
                adjusted.setTop(snapped.top())
                adjusted.setBottom(snapped.top() + height)
            else:
                adjusted.setBottom(snapped.bottom())
                adjusted.setTop(snapped.bottom() - height)

        rect.setRect(adjusted.left(), adjusted.top(), adjusted.width(), adjusted.height())

    def set_marquee_rect(self, rect):
        """
        Snap, store, and draw the marquee rectangle.

        """
        rect = self._snap_marquee_rect(rect.normalized())
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            self.clear_marquee()
            return

        self.marquee = QtCore.QRectF(rect)
        self.marquee_highlight.setRect(rect)
        self.marquee_outline.setRect(rect)
        self.marquee_highlight.show()
        self.marquee_outline.show()
        self._update_marquee_handles()

    def clear_marquee(self):
        self.marquee = None
        self.marquee_highlight.hide()
        self.marquee_outline.hide()
        self.marquee_handles.hide()

    def _snap_marquee_rect(self, rect):
        return rect

    def marquee_drag_mode_at(self, scene_pos):
        """
        Return the resize handle under a scene position, if there is one.

        """
        if self.marquee is None or not self.marquee_handles.isVisible():
            return None

        threshold = 8
        for handle, point in self._marquee_handle_points().items():
            handle_scene_pos = self.plot.vb.mapViewToScene(point)
            distance = (
                (handle_scene_pos.x() - scene_pos.x()) ** 2
                + (handle_scene_pos.y() - scene_pos.y()) ** 2
                )
            if distance <= threshold ** 2:
                return handle

        return None

    def marquee_cursor_shape_at(self, scene_pos, modifiers=QtCore.Qt.NoModifier):
        mode = self.current_marquee_drag_mode()
        if mode == "new":
            return QtCore.Qt.CrossCursor
        if mode is not None:
            return self._marquee_cursor_shape_for_handle(mode)

        handle = self.marquee_drag_mode_at(scene_pos)
        if handle is not None:
            return self._marquee_cursor_shape_for_handle(handle)
        if modifiers & QtCore.Qt.AltModifier:
            return QtCore.Qt.CrossCursor

        return None

    def _marquee_cursor_shape_for_handle(self, handle):
        if handle in ("e", "w"):
            return QtCore.Qt.SizeHorCursor
        if handle in ("n", "s"):
            return QtCore.Qt.SizeVerCursor
        if handle in ("nw", "se"):
            return QtCore.Qt.SizeFDiagCursor
        if handle in ("ne", "sw"):
            return QtCore.Qt.SizeBDiagCursor

        return QtCore.Qt.CrossCursor

    def _update_marquee_handles(self):
        points = list(self._marquee_handle_points().values())
        self.marquee_handles.setData(
            [point.x() for point in points],
            [point.y() for point in points],
            )
        self.marquee_handles.show()

    def _marquee_handle_points(self):
        return self._marquee_handle_points_for_rect(self.marquee)

    def _marquee_handle_points_for_rect(self, rect):
        centre_x = rect.center().x()
        centre_y = rect.center().y()
        return {
            "nw": QtCore.QPointF(rect.left(), rect.bottom()),
            "n": QtCore.QPointF(centre_x, rect.bottom()),
            "ne": QtCore.QPointF(rect.right(), rect.bottom()),
            "e": QtCore.QPointF(rect.right(), centre_y),
            "se": QtCore.QPointF(rect.right(), rect.top()),
            "s": QtCore.QPointF(centre_x, rect.top()),
            "sw": QtCore.QPointF(rect.left(), rect.top()),
            "w": QtCore.QPointF(rect.left(), centre_y),
            }
