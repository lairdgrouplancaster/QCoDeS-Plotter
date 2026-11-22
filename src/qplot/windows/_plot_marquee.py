from bisect import bisect_left
from collections.abc import Callable
from math import isfinite, nextafter
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

from qplot.tools.sample_statistics import (
    finite_mean,
    finite_standard_deviation,
    finite_sum_is_zero,
)

from ._plot_host import PlotHost
from ._plot_transform_labels import native_axis_quantities, quantity_text
from ._small_dialogs import (
    close_default,
    dialog_layout,
    equal_button_widths,
    resize_dialog,
)
from ._widgets import CopyableTableWidget

_MarqueeHandle = Literal["nw", "n", "ne", "e", "se", "s", "sw", "w"]
_MarqueeDragMode = Literal["new", "move", "nw", "n", "ne", "e", "se", "s", "sw", "w"]
_MarqueeAction = Callable[[], bool]
_CellEdges = tuple[float, ...] | npt.NDArray[np.float64]


def _nearest_position(count: int, position: Callable[[int], float], value: float) -> int:
    upper = bisect_left(range(count), value, key=position)
    if upper == 0 or upper == count:
        return min(upper, count - 1)
    return upper - 1 if value - position(upper - 1) <= position(upper) - value else upper


def _moved_interval(
        edges: _CellEdges | None, low: float, span: float, delta: float,
        ) -> tuple[float, float]:
    """Return the translated lower edge and span in displayed coordinates."""
    if delta == 0:
        return low, span
    if edges is None:
        return low + delta, span

    high = low + span
    start = _nearest_position(len(edges), edges.__getitem__, low)
    stop = _nearest_position(len(edges), edges.__getitem__, high)
    width = max(1, stop - start)

    def centre(index: int) -> float:
        return float(edges[index]) / 2 + float(edges[index + width]) / 2

    # Feasible blocks keep every selected cell, including at the ends of an
    # irregular axis. Their centres are ordered, so no full scan is required.
    index = _nearest_position(len(edges) - width, centre, low / 2 + high / 2 + delta)
    left, right = float(edges[index]), float(edges[index + width])
    span = right - left
    # QRectF reconstructs its high edge by addition. Do not let rounding
    # include the next cell when the selection is used for statistics.
    return left, nextafter(span, 0.) if left + span > right else span


class _MarqueeDragState(TypedDict):
    anchor: QtCore.QPointF
    handle_offset: QtCore.QPointF
    mode: _MarqueeDragMode
    rect: QtCore.QRectF | None
    move_axis: Literal["x", "y"] | None
    move_edges: tuple[_CellEdges | None, _CellEdges | None]


if TYPE_CHECKING:
    from ._dataset_handle import DatasetKey

    class _PlotMarqueeBase(PlotHost, qtw.QMainWindow):
        _dataset_key: DatasetKey
        _marquee_drag_state: _MarqueeDragState | None
        _marquee_stats_dialog: qtw.QDialog
        marquee: QtCore.QRectF | None
        marquee_handles: Any
        marquee_highlight: qtw.QGraphicsRectItem
        marquee_outline: qtw.QGraphicsRectItem
        plot: Any
        vb: Any
        closed: QtCore.pyqtBoundSignal

        def _view_range_changed_programmatically(self) -> None: ...
else:
    class _PlotMarqueeBase(PlotHost):
        pass


class PlotMarqueeMixin(_PlotMarqueeBase):
    """
    Shared marquee selection behavior for plot windows.

    Plot type subclasses can customise selection snapping, statistics, and
    context-menu entries by overriding the small hook methods near the end of
    this mixin.
    """

    def _init_marquee(self) -> None:
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
        self.marquee_highlight.setBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        self.marquee_highlight.setZValue(18)
        self.marquee_highlight.hide()
        self.marquee_highlight.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._add_plot_overlay(self.marquee_highlight)

        self.marquee_outline = qtw.QGraphicsRectItem()
        pen = QtGui.QPen(QtGui.QColor(65, 65, 65, 220))
        pen.setWidthF(1.2)
        pen.setCosmetic(True)
        pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        self.marquee_outline.setPen(pen)
        self.marquee_outline.setBrush(QtGui.QBrush(QtGui.QColor(40, 40, 40, 24)))
        self.marquee_outline.setZValue(19)
        self.marquee_outline.hide()
        self.marquee_outline.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._add_plot_overlay(self.marquee_outline)

        self.marquee_handles = pg.ScatterPlotItem(
            symbol="s",
            size=6,
            pen=pg.mkPen((20, 20, 20, 230), width=1, cosmetic=True),
            brush=pg.mkBrush(245, 245, 245, 230),
            )
        self.marquee_handles.setZValue(20)
        self.marquee_handles.hide()
        self.marquee_handles.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._add_plot_overlay(self.marquee_handles)

    def _add_plot_overlay(self, item: Any, viewbox: Any = None) -> None:
        """Attach decorations without registering plot data or affecting bounds."""

        viewbox = viewbox if viewbox is not None else self._marquee_viewbox()
        item._qplot_ui_overlay = True
        viewbox.addItem(item, ignoreBounds=True)
        self.__dict__.setdefault("_plot_overlay_viewboxes", {})[id(item)] = viewbox

    def _marquee_viewbox(self) -> Any:
        """Return the coordinate owner for marquee graphics and navigation."""

        try:
            heatmap_viewbox = (
                getattr(self, "_primary_heatmap_viewbox", None)
                if getattr(self, "operation_kind", None) == "plot2d" else None
            )
        except RuntimeError:
            heatmap_viewbox = None
        if callable(heatmap_viewbox):
            viewbox = heatmap_viewbox()
            if viewbox is not None:
                return viewbox
        return self.__dict__.get("vb") or self.plot.vb

    def _marquee_zoom_viewbox(self, axis: Literal["x", "y"]) -> Any:
        """Return the range owner for one dimension of the selection."""

        return self._marquee_viewbox()

    def _marquee_move_edges(self) -> tuple[_CellEdges | None, _CellEdges | None]:
        """Return displayed cell boundaries for axes that snap during a move."""
        return None, None

    def _marquee_axis_assignment_changed(self) -> None:
        """Discard old coordinates and move decorations to their new owner."""

        self.finish_marquee_drag()
        if "marquee_outline" not in self.__dict__:
            return
        self.clear_marquee()
        target = self._marquee_viewbox()
        overlays = self.__dict__.get("_plot_overlay_viewboxes", {})
        for item in (self.marquee_highlight, self.marquee_outline, self.marquee_handles):
            current = overlays.get(id(item))
            if current is not target:
                if current is not None:
                    current.removeItem(item)
                self._add_plot_overlay(item, target)

    def is_marquee_dragging(self) -> bool:
        return self._marquee_drag_state is not None

    def current_marquee_drag_mode(self) -> _MarqueeDragMode | None:
        if self._marquee_drag_state is None:
            return None

        return self._marquee_drag_state["mode"]

    def begin_marquee_drag(
            self,
            start: QtCore.QPointF,
            mode: _MarqueeDragMode | None = None,
            ) -> None:
        """
        Start creating or resizing a marquee in plot coordinates.

        """
        start = QtCore.QPointF(start)
        if mode is None:
            mode = "new"

        handle_offset = QtCore.QPointF()
        if mode != "new" and mode != "move" and self.marquee is not None:
            handle_point = self._marquee_handle_points_for_rect(self.marquee)[mode]
            handle_offset = start - handle_point

        self._marquee_drag_state = {
            "anchor": QtCore.QPointF(start),
            "handle_offset": QtCore.QPointF(handle_offset),
            "mode": mode,
            "rect": QtCore.QRectF(self.marquee) if self.marquee is not None else None,
            "move_axis": None,
            "move_edges": self._marquee_move_edges() if mode == "move" else (None, None),
            }

        if mode == "new":
            self.set_marquee_rect(QtCore.QRectF(start, start))

    def drag_marquee_to(
            self,
            point: QtCore.QPointF,
            modifiers: QtCore.Qt.KeyboardModifier = (
                QtCore.Qt.KeyboardModifier.NoModifier
                ),
            ) -> None:
        """
        Update the marquee during an active drag.

        """
        point = QtCore.QPointF(point)
        state = self._marquee_drag_state
        if state is None:
            return

        if state["mode"] == "new" or state["rect"] is None:
            rect = QtCore.QRectF(state["anchor"], point)
        elif state["mode"] == "move":
            rect = QtCore.QRectF(state["rect"])
            delta = point - state["anchor"]
            if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier:
                if state.get("move_axis") is None and not delta.isNull():
                    viewbox = self._marquee_viewbox()
                    screen_delta = viewbox.mapViewToScene(point) - viewbox.mapViewToScene(state["anchor"])
                    state["move_axis"] = "x" if abs(screen_delta.x()) >= abs(screen_delta.y()) else "y"
                if state.get("move_axis") == "x":
                    delta.setY(0)
                elif state.get("move_axis") == "y":
                    delta.setX(0)
            else:
                state["move_axis"] = None
            x_edges, y_edges = state["move_edges"]
            left, width = _moved_interval(x_edges, rect.left(), rect.width(), delta.x())
            bottom, height = _moved_interval(y_edges, rect.top(), rect.height(), delta.y())
            self._draw_marquee_rect(QtCore.QRectF(left, bottom, width, height))
            return
        else:
            rect = QtCore.QRectF(state["rect"])
            offset = state.get("handle_offset", QtCore.QPointF())
            point = QtCore.QPointF(
                point.x() - offset.x(),
                point.y() - offset.y(),
                )
            self._resize_marquee_rect(rect, state["mode"], point, modifiers)

        self.set_marquee_rect(rect)

    def finish_marquee_drag(self) -> None:
        self._marquee_drag_state = None

    def marquee_contains_scene_pos(self, scene_pos: QtCore.QPointF) -> bool:
        """
        Return whether a scene position is inside the current marquee.

        """
        if self.marquee is None:
            return False

        point = self._marquee_viewbox().mapSceneToView(scene_pos)
        return self.marquee.normalized().contains(point)

    def open_marquee_context_menu(
            self,
            scene_pos: QtCore.QPointF,
            global_pos: QtCore.QPoint | QtCore.QPointF | None = None,
            ) -> bool:
        """
        Open the marquee context menu when a right-click lands inside it.

        """
        if self.__dict__.get("_closed", False) or not self.marquee_contains_scene_pos(scene_pos):
            return False

        menu = self._new_marquee_context_menu()
        menu.setParent(self, menu.windowFlags())
        # Source replacement can close a plot while this nested event loop is
        # running. Its temporary popup must stop accepting actions at that point.
        self.closed.connect(menu.close)
        if global_pos is None:
            global_pos = QtGui.QCursor.pos()
        elif isinstance(global_pos, QtCore.QPointF):
            global_pos = global_pos.toPoint()

        try:
            menu.exec(global_pos)
        finally:
            if not sip.isdeleted(menu):
                menu.deleteLater()
        return True

    def _new_marquee_context_menu(self) -> qtw.QMenu:
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

    def _add_marquee_context_action(
            self,
            menu: qtw.QMenu,
            text: str,
            callback: _MarqueeAction,
            ) -> QtGui.QAction:
        action = menu.addAction(text)
        if action is None:
            raise RuntimeError("Marquee context action could not be created.")
        action.triggered.connect(
            lambda _checked=False, callback=callback: self._execute_marquee_action(
                callback
                )
            )
        return action

    def _add_marquee_color_context_action(
            self,
            menu: qtw.QMenu,
            ) -> QtGui.QAction | None:
        return None

    def _add_marquee_stats_context_action(self, menu: qtw.QMenu) -> QtGui.QAction:
        stats_text = self._marquee_stats_text()

        def open_stats_dialog(stats_text: str | None = stats_text) -> bool:
            return self.show_marquee_stats_dialog(stats_text)

        action = self._add_marquee_context_action(
            menu,
            "Stats...",
            open_stats_dialog,
            )
        if stats_text is None:
            action.setEnabled(False)
            action.setToolTip("No data points inside the marquee.")
        else:
            action.setToolTip("Show statistics for the marquee selection.")
            action.setStatusTip("Show statistics for the marquee selection.")
        return action

    def _execute_marquee_action(self, callback: _MarqueeAction) -> None:
        if self.__dict__.get("_closed", False):
            return
        if callback():
            self.clear_marquee()

    def zoom_marquee(self, axes: str) -> bool:
        rect = self.marquee.normalized() if self.marquee is not None else None
        if rect is None:
            return False

        if "x" in axes:
            self._marquee_zoom_viewbox("x").setXRange(
                rect.left(),
                rect.right(),
                padding=0,
            )
        if "y" in axes:
            self._marquee_zoom_viewbox("y").setYRange(
                rect.top(),
                rect.bottom(),
                padding=0,
            )
        self._view_range_changed_programmatically()
        return True

    def _marquee_stats_text(self) -> str | None:
        return None

    def show_marquee_stats_dialog(self, stats_text: str | None = None) -> bool:
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

    def _new_marquee_stats_dialog(self, stats_text: str) -> qtw.QDialog:
        return new_statistics_dialog(self, stats_text, "Marquee statistics")

    @staticmethod
    def _new_marquee_stats_table(results: list[tuple[str, str]], details: list[tuple[str, str]]) -> CopyableTableWidget:
        rows = results + details
        stats_table = CopyableTableWidget()
        stats_table.setObjectName("statisticsTable")
        stats_table.setColumnCount(2)
        stats_table.setHorizontalHeaderLabels(["Field", "Value"])
        stats_table.setRowCount(len(rows))
        stats_table.setAlternatingRowColors(True)
        stats_table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        stats_table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        stats_table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        stats_table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        stats_table.setWordWrap(True)
        vertical_header = stats_table.verticalHeader()
        horizontal_header = stats_table.horizontalHeader()
        if vertical_header is None or horizontal_header is None:
            raise RuntimeError("Marquee stats table headers are not available.")

        vertical_header.hide()
        vertical_header.setSectionResizeMode(qtw.QHeaderView.ResizeMode.ResizeToContents)
        horizontal_header.setStretchLastSection(True)
        horizontal_header.setSectionResizeMode(
            0,
            qtw.QHeaderView.ResizeMode.ResizeToContents
            )
        horizontal_header.setSectionResizeMode(
            1,
            qtw.QHeaderView.ResizeMode.Stretch
            )
        stats_table.setMinimumWidth(280)
        stats_table.setMinimumHeight(170)

        for row, (field, value) in enumerate(rows):
            field_item = qtw.QTableWidgetItem(field)
            value_item = qtw.QTableWidgetItem(value)
            field_item.setToolTip(field)
            value_item.setToolTip(value)
            stats_table.setItem(row, 0, field_item)
            stats_table.setItem(row, 1, value_item)

        if details:
            section_row = len(results)
            stats_table.insertRow(section_row)
            section = qtw.QTableWidgetItem("Selection and source")
            section.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            font = stats_table.font()
            font.setBold(True)
            section.setFont(font)
            section.setBackground(stats_table.palette().button())
            stats_table.setItem(section_row, 0, section)
            stats_table.setSpan(section_row, 0, 1, 2)

        return stats_table

    @staticmethod
    def _marquee_stats_table_rows(stats_text: str) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
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

    def _format_marquee_stats_text(
            self,
            size_text: str,
            values: Any,
            rect: QtCore.QRectF | None = None,
            *,
            contribution: str,
            heatmap: bool = False,
            x_axis: str = "x",
            y_axis: str = "y",
            ) -> str:
        marquee = self.marquee
        if rect is None and marquee is not None:
            rect = marquee.normalized()

        lines = [size_text]
        source = self.__dict__.get("_plot_source_info")
        if source is not None:
            lines.append(f"Run: {source[1]} ({self._dataset_key.guid})")
        parameter = self.__dict__.get("param")
        if parameter is not None:
            lines.append(f"Measurement: {quantity_text(parameter)}")
        axes = self.__dict__.get("axis_param", {})
        if not heatmap:
            line = self.__dict__.get("line")
            axes = native_axis_quantities(axes.get("x"), axes.get("y"), getattr(line, "opts", {}))
        for axis in ("x", "y"):
            quantity = quantity_text(axes.get(axis))
            if quantity:
                lines.append(f"{axis.upper()} quantity: {quantity}")
        measured = self.__dict__.get("display_param", parameter) if heatmap else axes.get("y")
        if quantity_text(measured):
            lines.append(f"Statistics quantity: {quantity_text(measured)}")
        lines.append(f"Data: {contribution}")
        cut = self.__dict__.get("_displayed_description")
        if cut:
            lines.append(f"Cut: {cut}")
        if rect is not None:
            view_to_data = getattr(self, "view_to_data", None)
            if callable(view_to_data):
                x_low = view_to_data(x_axis, rect.left())
                x_high = view_to_data(x_axis, rect.right())
                y_low = view_to_data(y_axis, rect.top())
                y_high = view_to_data(y_axis, rect.bottom())
            else:
                x_low, x_high = rect.left(), rect.right()
                y_low, y_high = rect.top(), rect.bottom()
            lines.extend((
                f"X range: {self.formatNum(x_low)} to {self.formatNum(x_high)}",
                f"Y range: {self.formatNum(y_low)} to {self.formatNum(y_high)}",
                ))

        average = finite_mean(values)
        deviation = finite_standard_deviation(values)
        if (not isfinite(average) or not isfinite(deviation)
                or (deviation == 0 and values.min() != values.max())
                or (average == 0 and not finite_sum_is_zero(values))):
            # A small selection inside an otherwise drawable plot can have
            # an unrepresentable mean/spread. Keep its geometry and Copy
            # action available without presenting a false zero summary.
            lines.append("Statistics: Selected values are outside the statistics display range.")
            return "\n".join(lines)

        lines.extend((
            f"Average: {self.formatNum(average)}",
            f"Standard deviation: {self.formatNum(deviation)}",
            f"Max: {self.formatNum(float(values.max()))}",
            f"Min: {self.formatNum(float(values.min()))}",
            ))

        return "\n".join(lines)

    def copy_marquee_stats_to_clipboard(self, stats_text: str | None = None) -> bool:
        if stats_text is None:
            stats_text = self._marquee_stats_text()
        if stats_text is None:
            return False

        clipboard = qtw.QApplication.clipboard()
        if clipboard is None:
            return False

        clipboard.setText(stats_text)
        return True

    def _resize_marquee_rect(
            self,
            rect: QtCore.QRectF,
            handle: _MarqueeHandle,
            point: QtCore.QPointF,
            modifiers: QtCore.Qt.KeyboardModifier = (
                QtCore.Qt.KeyboardModifier.NoModifier
                ),
            ) -> None:
        symmetric = bool(modifiers & QtCore.Qt.KeyboardModifier.AltModifier)
        asymmetric = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier) and not symmetric
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
            offset = -dx if symmetric else dx
            if "w" in handle:
                rect.setRight(original.right() + offset)
            else:
                rect.setLeft(original.left() + offset)

        if ("n" in handle or "s" in handle) and (symmetric or asymmetric):
            offset = -dy if symmetric else dy
            if "s" in handle:
                rect.setBottom(original.bottom() + offset)
            else:
                rect.setTop(original.top() + offset)

        if asymmetric:
            self._snap_translated_marquee_rect(rect, original, handle)

    def _snap_translated_marquee_rect(
            self,
            rect: QtCore.QRectF,
            original: QtCore.QRectF,
            handle: _MarqueeHandle,
            ) -> None:
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

    def set_marquee_rect(self, rect: QtCore.QRectF) -> None:
        """
        Snap, store, and draw the marquee rectangle.

        """
        snapped = self._snap_marquee_rect(rect.normalized())
        if snapped is None or snapped.width() <= 0 or snapped.height() <= 0:
            self.clear_marquee()
            return

        self._draw_marquee_rect(snapped)

    def _draw_marquee_rect(self, rect: QtCore.QRectF) -> None:
        self.marquee = QtCore.QRectF(rect)
        self.marquee_highlight.setRect(rect)
        self.marquee_outline.setRect(rect)
        self.marquee_highlight.show()
        self.marquee_outline.show()
        self._update_marquee_handles()

    def clear_marquee(self) -> None:
        self.marquee = None
        self.marquee_highlight.hide()
        self.marquee_outline.hide()
        self.marquee_handles.hide()

    def _snap_marquee_rect(self, rect: QtCore.QRectF) -> QtCore.QRectF | None:
        return rect

    def marquee_drag_mode_at(self, scene_pos: QtCore.QPointF) -> _MarqueeHandle | None:
        """
        Return the resize handle under a scene position, if there is one.

        """
        if self.marquee is None or not self.marquee_handles.isVisible():
            return None

        threshold = 8
        for handle, point in self._marquee_handle_points().items():
            handle_scene_pos = self._marquee_viewbox().mapViewToScene(point)
            distance = (
                (handle_scene_pos.x() - scene_pos.x()) ** 2
                + (handle_scene_pos.y() - scene_pos.y()) ** 2
                )
            if distance <= threshold ** 2:
                return handle

        return None

    def marquee_cursor_shape_at(
            self,
            scene_pos: QtCore.QPointF,
            modifiers: QtCore.Qt.KeyboardModifier = (
                QtCore.Qt.KeyboardModifier.NoModifier
                ),
            ) -> QtCore.Qt.CursorShape | None:
        mode = self.current_marquee_drag_mode()
        if mode == "move":
            return QtCore.Qt.CursorShape.SizeAllCursor
        if mode == "new":
            return QtCore.Qt.CursorShape.CrossCursor
        if mode is not None:
            return self._marquee_cursor_shape_for_handle(mode)

        handle = self.marquee_drag_mode_at(scene_pos)
        if handle is not None:
            return self._marquee_cursor_shape_for_handle(handle)
        if self.marquee_contains_scene_pos(scene_pos):
            return QtCore.Qt.CursorShape.SizeAllCursor
        if modifiers & QtCore.Qt.KeyboardModifier.AltModifier:
            return QtCore.Qt.CursorShape.CrossCursor

        return None

    def _marquee_cursor_shape_for_handle(
            self,
            handle: _MarqueeDragMode,
            ) -> QtCore.Qt.CursorShape:
        if handle in ("e", "w"):
            return QtCore.Qt.CursorShape.SizeHorCursor
        if handle in ("n", "s"):
            return QtCore.Qt.CursorShape.SizeVerCursor
        if handle in ("nw", "se"):
            return QtCore.Qt.CursorShape.SizeFDiagCursor
        if handle in ("ne", "sw"):
            return QtCore.Qt.CursorShape.SizeBDiagCursor

        return QtCore.Qt.CursorShape.CrossCursor

    def _update_marquee_handles(self) -> None:
        points = list(self._marquee_handle_points().values())
        self.marquee_handles.setData(
            [point.x() for point in points],
            [point.y() for point in points],
            )
        self.marquee_handles.show()

    def _marquee_handle_points(self) -> dict[_MarqueeHandle, QtCore.QPointF]:
        if self.marquee is None:
            return {}
        return self._marquee_handle_points_for_rect(self.marquee)

    def _marquee_handle_points_for_rect(
            self,
            rect: QtCore.QRectF,
            ) -> dict[_MarqueeHandle, QtCore.QPointF]:
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


def _statistics_sections(stats_text: str) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    rows = PlotMarqueeMixin._marquee_stats_table_rows(stats_text)
    fields = dict(rows)
    # For a transformed trace, the result units belong to the plotted
    # quantity. Keep the original measurement in the source details below.
    quantity = fields.get("Statistics quantity") or fields.get("Measurement", "")
    run, _, guid = fields.get("Run", "").partition(" (")
    heading = " · ".join(part for part in (quantity, f"Run {run}" if run else "") if part)
    results = []
    details = []
    for field, value in rows:
        if field in ("Average", "Standard deviation", "Max", "Min", "Statistics"):
            results.append((field, value))
        elif field == "Run":
            if guid:
                details.append(("Run GUID", guid.removesuffix(")")))
        elif field not in ("Measurement", "Statistics quantity") or value != quantity:
            details.append((field, value))
    return heading, results, details


def new_statistics_dialog(parent, stats_text, title):
    dialog = qtw.QDialog(parent)
    dialog.setWindowTitle(title)
    layout = dialog_layout(dialog)
    heading, results, details = _statistics_sections(stats_text)
    if heading:
        label = qtw.QLabel()
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        label.setText(heading)
        label.setToolTip(heading)
        label.setWordWrap(True)
        label.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred)
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
    stats_table = PlotMarqueeMixin._new_marquee_stats_table(results, details)
    stats_table.setAccessibleName(title)
    layout.addWidget(stats_table, 1)

    buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
    copy_button = buttons.addButton("Copy full report", qtw.QDialogButtonBox.ButtonRole.ActionRole)
    if copy_button is None:
        raise RuntimeError("Marquee stats copy button is not available.")

    def copy_stats(_checked: bool = False) -> None:
        clipboard = qtw.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(stats_text)

    copy_button.clicked.connect(copy_stats)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    close = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
    assert close is not None
    equal_button_widths(copy_button, close)
    close_default(buttons)
    # Keep the results visible on opening and let longer source details scroll.
    resize_dialog(dialog, 600, min(480, max(260, 112 + 27 * stats_table.rowCount())))

    return dialog
