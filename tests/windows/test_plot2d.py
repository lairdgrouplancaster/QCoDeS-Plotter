import unittest

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._plotWin import plotWidget
from qplot.windows.plot2d import _COLORBAR_COLORMAPS, plot2d


class Plot2dLiveRefreshTestCase(unittest.TestCase):
    def test_empty_live_worker_data_releases_worker_without_rendering(self):
        class Signal:
            def __init__(self):
                self.emitted = 0

            def emit(self):
                self.emitted += 1

        class Worker:
            read_data = False
            running = True
            axis_data = {"x": np.array([]), "y": np.array([])}
            axis_param = {"x": object(), "y": object()}
            dataGrid = np.empty((0, 0))
            started_at = 0

        class Dataset:
            number_of_results = 0

        class Param:
            name = "signal"

        window = plot2d.__new__(plot2d)
        worker = Worker()
        window.worker = worker
        window._guid = "guid"
        window._dataset_holder = {
            "guid": {
                "dataset": Dataset(),
                "del_timer": None,
                }
            }
        window.param = Param()
        window.end_wait = Signal()
        window._set_param_axis_labels = lambda: None
        window.show_status_messages = []
        window.show_status = lambda *args: window.show_status_messages.append(args)

        plot2d.refreshPlot(window, True, worker=worker)

        self.assertFalse(worker.running)
        self.assertEqual(window.end_wait.emitted, 1)
        self.assertIn("Waiting for plottable data", window.show_status_messages[-1][0])

    def test_plottable_heatmap_data_requires_axes_and_finite_grid(self):
        window = plot2d.__new__(plot2d)
        window.axis_data = {"x": np.array([0.0]), "y": np.array([1.0])}
        window.dataGrid = np.array([[np.nan]])

        self.assertFalse(window._has_plottable_heatmap_data())

        window.dataGrid = np.array([[2.0]])

        self.assertTrue(window._has_plottable_heatmap_data())


class HeatmapHoverOutlineTestCase(unittest.TestCase):
    class SignalCatcher:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    class SweepLine:
        def __init__(self, sweep_id, angle, value):
            self.sweep_id = sweep_id
            self.angle = angle
            self._value = value
            self.bounds = None
            self.movable = True
            self.cursor_shape = None
            self.drag_events = []
            self.hover_events = []
            self.mouseHovering = False

        def setBounds(self, bounds):
            self.bounds = bounds

        def setPos(self, value):
            self._value = value

        def value(self):
            return self._value

        def setCursor(self, shape):
            self.cursor_shape = shape

        def unsetCursor(self):
            self.cursor_shape = None

        def mouseDragEvent(self, event):
            self.drag_events.append(event)

        def hoverEvent(self, event):
            self.hover_events.append(event)
            self.mouseHovering = not event.isExit()

    class SweepLineHoverEvent:
        def __init__(self, *, exit=False):
            self._exit = exit

        def isExit(self):
            return self._exit

    class Colorbar:
        def __init__(self):
            self.values = None
            self.color_map = None

        def setLevels(self, values):
            self.values = values

        def levels(self):
            return self.values

        def setColorMap(self, color_map):
            self.color_map = color_map

    class Axis:
        def __init__(self):
            self.width = None
            self.style = {}
            self.picture = object()
            self.updated = False
            self.tickStrings = None

        def setWidth(self, width):
            self.width = width

        def setStyle(self, **kwargs):
            self.style.update(kwargs)

        def update(self):
            self.updated = True

    class CheckBox:
        def __init__(self, checked=False):
            self.checked = checked

        def setChecked(self, checked):
            self.checked = checked

        def isChecked(self):
            return self.checked

    class ColorbarLine:
        def __init__(self, position):
            self.previous_drag_calls = []
            self.position = position

        def mouseDragEvent(self, event):
            self.previous_drag_calls.append(event)

        def mapToParent(self, position):
            return position

        def setPos(self, position):
            self.position = position

        def value(self):
            return self.position

    class ColorbarRegion:
        movable = True
        orientation = "horizontal"
        span = (0, 1)

        def __init__(self):
            self.previous_drag_calls = []
            self.lines = [
                HeatmapHoverOutlineTestCase.ColorbarLine(63.0),
                HeatmapHoverOutlineTestCase.ColorbarLine(191.0),
                ]
            self.updated = False

        def mouseDragEvent(self, event):
            self.previous_drag_calls.append(event)

        def prepareGeometryChange(self):
            pass

        def viewRect(self):
            return QtCore.QRectF(0.0, 0.0, 24.0, 254.0)

        def update(self):
            self.updated = True

    class ColorbarDragEvent:
        def __init__(
                self,
                y,
                *,
                down_y=0.0,
                modifiers=QtCore.Qt.KeyboardModifier.NoModifier,
                start=False,
                finish=False,
                ):
            self._y = y
            self._down_y = down_y
            self._modifiers = modifiers
            self._start = start
            self._finish = finish
            self.accepted = False

        def modifiers(self):
            return self._modifiers

        def button(self):
            return QtCore.Qt.MouseButton.LeftButton

        def isStart(self):
            return self._start

        def isFinish(self):
            return self._finish

        def buttonDownPos(self):
            return QtCore.QPointF(0.0, self._down_y)

        def pos(self):
            return QtCore.QPointF(0.0, self._y)

        def accept(self):
            self.accepted = True

    class SweepLineDragEvent:
        def __init__(
                self,
                *,
                button=QtCore.Qt.MouseButton.LeftButton,
                finish=False,
                ):
            self._button = button
            self._finish = finish

        def button(self):
            return self._button

        def isFinish(self):
            return self._finish

    class SweepLineClickEvent:
        def __init__(
                self,
                *,
                button=QtCore.Qt.MouseButton.LeftButton,
                double=False,
                modifiers=QtCore.Qt.KeyboardModifier.NoModifier,
                ):
            self._button = button
            self._double = double
            self._modifiers = modifiers
            self.accepted = False

        def button(self):
            return self._button

        def double(self):
            return self._double

        def modifiers(self):
            return self._modifiers

        def accept(self):
            self.accepted = True

    def test_hover_outline_tracks_heatmap_cell_geometry(self):
        window = plot2d.__new__(plot2d)
        window.hover_pixel_outline = qtw.QGraphicsRectItem()
        window.rect = QtCore.QRectF(10.0, 20.0, 8.0, 6.0)
        window.dataGrid = np.zeros((3, 4))
        window.z_index = None

        window.show_hover_pixel_outline(2, 1)

        outline_rect = window.hover_pixel_outline.rect()
        self.assertTrue(window.hover_pixel_outline.isVisible())
        self.assertEqual(window.z_index, [2, 1])
        self.assertEqual(outline_rect, QtCore.QRectF(14.0, 22.0, 2.0, 2.0))

    def test_hover_outline_hides_when_hover_index_is_invalid(self):
        window = plot2d.__new__(plot2d)
        window.hover_pixel_outline = qtw.QGraphicsRectItem()
        window.hover_pixel_outline.show()
        window.rect = QtCore.QRectF(0.0, 0.0, 2.0, 2.0)
        window.dataGrid = np.zeros((2, 2))
        window.z_index = [3, 0]

        window._update_hover_pixel_outline_from_index()

        self.assertFalse(window.hover_pixel_outline.isVisible())

    def test_marquee_edges_snap_to_heatmap_pixel_boundaries(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(10.0, 20.0, 8.0, 6.0)
        window.dataGrid = np.zeros((3, 4))

        rect = window._snap_marquee_rect(QtCore.QRectF(10.4, 21.1, 3.8, 4.8))

        self.assertEqual(rect, QtCore.QRectF(10.0, 20.0, 6.0, 6.0))

    def test_shift_drag_corner_keeps_heatmap_pixel_marquee_size_after_snap(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 0.0, 20.0, 20.0)
        window.dataGrid = np.zeros((20, 20))
        rect = QtCore.QRectF(0.0, 0.0, 10.0, 10.0)

        window._resize_marquee_rect(
            rect,
            "ne",
            QtCore.QPointF(10.1, 10.1),
            QtCore.Qt.KeyboardModifier.ShiftModifier,
            )
        rect = window._snap_marquee_rect(rect.normalized())

        self.assertEqual(rect, QtCore.QRectF(1.0, 1.0, 10.0, 10.0))

    def test_marquee_menu_includes_zoom_color_for_2d_plots(self):
        window = plot2d.__new__(plot2d)
        window.marquee = QtCore.QRectF(1.0, 1.0, 2.0, 2.0)
        window.rect = QtCore.QRectF(0.0, 0.0, 4.0, 4.0)
        window.dataGrid = np.arange(16.0).reshape(4, 4)

        menu = window._new_marquee_context_menu()
        action_texts = [action.text() for action in menu.actions()]

        self.assertEqual(action_texts, ["Zoom", "Zoom X", "Zoom Y", "Zoom color", "Stats..."])

    def test_context_menu_uses_short_cut_labels(self):
        class Host(plot2d):
            def __init__(self):
                qtw.QMainWindow.__init__(self)

            def register_shortcut(self, *_args, **_kwargs):
                pass

            def _init_colorbar_scale_controls(self):
                pass

        widget = pg.GraphicsLayoutWidget()
        host = Host()
        host.widget = widget
        host.plot = widget.addPlot()
        host.vb = host.plot.vb
        host.oper_dock = qtw.QDockWidget()

        try:
            host.initContextMenu()
            action_texts = [action.text().replace("&", "") for action in host.vbMenu.actions()]

            self.assertIn("Horizontal Cut", action_texts)
            self.assertIn("Vertical Cut", action_texts)
            self.assertNotIn("Plot Horizontal Cut", action_texts)
            self.assertNotIn("Plot Vertical Cut", action_texts)
        finally:
            host.deleteLater()
            widget.deleteLater()

    def test_zoom_color_uses_data_inside_marquee(self):
        window = plot2d.__new__(plot2d)
        window.marquee = QtCore.QRectF(1.0, 1.0, 2.0, 2.0)
        window.rect = QtCore.QRectF(0.0, 0.0, 4.0, 4.0)
        window.dataGrid = np.arange(16.0).reshape(4, 4)
        window.bar = self.Colorbar()
        window._colorbar_manual_levels = None

        self.assertTrue(window.zoom_marquee_color())

        self.assertEqual(window._colorbar_manual_levels, (5.0, 10.0))
        self.assertEqual(window.bar.values, (5.0, 10.0))

    def test_stats_action_opens_dialog_and_clears_marquee(self):
        window = plot2d.__new__(plot2d)
        window.marquee = QtCore.QRectF(1.0, 1.0, 2.0, 2.0)
        window.rect = QtCore.QRectF(0.0, 0.0, 4.0, 4.0)
        window.dataGrid = np.arange(16.0).reshape(4, 4)
        opened = []
        window.clear_marquee = lambda: setattr(window, "marquee", None)
        window.show_marquee_stats_dialog = lambda stats_text=None: opened.append(stats_text) or True
        qtw.QApplication.clipboard().clear()

        menu = window._new_marquee_context_menu()
        stats_action = next(action for action in menu.actions() if action.text() == "Stats...")
        stats_action.trigger()

        self.assertEqual(len(opened), 1)
        self.assertIn("2×2 points", opened[0])
        self.assertIn("X range: 1.000 to 3.000", opened[0])
        self.assertIn("Y range: 1.000 to 3.000", opened[0])
        self.assertEqual(qtw.QApplication.clipboard().text(), "")
        self.assertIsNone(window.marquee)

    def test_stats_dialog_copy_button_copies_displayed_stats(self):
        class Host(qtw.QMainWindow):
            _new_marquee_stats_dialog = plotWidget._new_marquee_stats_dialog
            _new_marquee_stats_table = plotWidget._new_marquee_stats_table
            _marquee_stats_table_rows = plotWidget._marquee_stats_table_rows
            copy_marquee_stats_to_clipboard = plotWidget.copy_marquee_stats_to_clipboard

        host = Host()
        stats_text = "2×2 points\nX range: 1.000 to 3.000\nAverage: 7.5"
        qtw.QApplication.clipboard().clear()

        dialog = host._new_marquee_stats_dialog(stats_text)
        stats_table = dialog.findChild(qtw.QTableWidget)
        copy_button = next(
            button for button in dialog.findChildren(qtw.QPushButton)
            if button.text() == "Copy"
            )
        copy_button.click()

        self.assertIsNotNone(stats_table)
        self.assertEqual(stats_table.rowCount(), 3)
        self.assertEqual(stats_table.horizontalHeaderItem(0).text(), "Field")
        self.assertEqual(stats_table.horizontalHeaderItem(1).text(), "Value")
        self.assertEqual(stats_table.item(0, 0).text(), "Selection")
        self.assertEqual(stats_table.item(0, 1).text(), "2×2 points")
        self.assertEqual(stats_table.item(1, 0).text(), "X range")
        self.assertEqual(stats_table.item(1, 1).text(), "1.000 to 3.000")
        self.assertEqual(qtw.QApplication.clipboard().text(), stats_text)

        stats_table.selectRow(2)
        stats_table.copySelection()
        self.assertEqual(qtw.QApplication.clipboard().text(), "Average\t7.5")

    def test_mouse_moved_clamps_heatmap_edge_to_last_cell(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        window = plot2d.__new__(plot2d)
        window.plot = plot_item
        window.rect = QtCore.QRectF(0.0, 0.0, 1.0, 1.0)
        window.dataGrid = np.array([[1.0, 2.0], [3.0, 4.0]])
        window.pos_labels = {
            "x": qtw.QLabel(),
            "y": qtw.QLabel(),
            "z": qtw.QLabel(),
            }
        window.formatNum = lambda value: str(value)
        shown_indices = []
        window.show_hover_pixel_outline = lambda i, j: shown_indices.append((i, j))
        window.hide_hover_pixel_outline = lambda: shown_indices.append(None)
        scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(1.0, 1.0))

        plotWidget.mouseMoved(window, scene_pos)

        self.assertEqual(shown_indices, [(1, 1)])
        self.assertEqual(window.pos_labels["x"].text(), "x = 0.75;")
        self.assertEqual(window.pos_labels["y"].text(), "y = 0.75;")
        self.assertEqual(window.pos_labels["z"].text(), "z = 4.0")

    def test_dragged_sweep_line_snaps_to_heatmap_pixel_centre(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 10.0, 4.0, 6.0)
        window.dataGrid = np.zeros((3, 4))
        window.sweep_moved = self.SignalCatcher()
        line = self.SweepLine(sweep_id=5, angle=90, value=2.7)

        window.moving_sweep(line)

        self.assertEqual(line.sweep_index, 2)
        self.assertEqual(window.active_sweep_line_id, 5)
        self.assertAlmostEqual(line.value(), 2.5)
        self.assertEqual(line.bounds, (0.5, 3.5))
        self.assertEqual(window.sweep_moved.calls, [(5, 2)])

    def test_shift_drag_moves_same_orientation_sweep_lines_together(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 10.0, 5.0, 4.0)
        window.dataGrid = np.zeros((4, 5))
        window.sweep_moved = self.SignalCatcher()
        window.sweep_group_drag_requested = lambda: True
        dragged_line = self.SweepLine(sweep_id=1, angle=90, value=2.7)
        dragged_line.sweep_index = 1
        companion_line = self.SweepLine(sweep_id=2, angle=90, value=3.5)
        companion_line.sweep_index = 3
        horizontal_line = self.SweepLine(sweep_id=3, angle=0, value=11.5)
        horizontal_line.sweep_index = 1
        window.sweep_lines = {
            1: dragged_line,
            2: companion_line,
            3: horizontal_line,
            }

        window.moving_sweep(dragged_line)

        self.assertEqual(dragged_line.sweep_index, 2)
        self.assertEqual(companion_line.sweep_index, 4)
        self.assertEqual(horizontal_line.sweep_index, 1)
        self.assertAlmostEqual(dragged_line.value(), 2.5)
        self.assertAlmostEqual(companion_line.value(), 4.5)
        self.assertAlmostEqual(horizontal_line.value(), 11.5)
        self.assertEqual(window.active_sweep_line_id, 1)
        self.assertEqual(window.sweep_moved.calls, [(1, 2), (2, 4)])

    def test_shift_drag_keeps_sweep_group_spacing_at_heatmap_edge(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 10.0, 5.0, 4.0)
        window.dataGrid = np.zeros((4, 5))
        window.sweep_moved = self.SignalCatcher()
        window.sweep_group_drag_requested = lambda: True
        dragged_line = self.SweepLine(sweep_id=1, angle=90, value=2.7)
        dragged_line.sweep_index = 1
        edge_line = self.SweepLine(sweep_id=2, angle=90, value=4.5)
        edge_line.sweep_index = 4
        window.sweep_lines = {
            1: dragged_line,
            2: edge_line,
            }

        window.moving_sweep(dragged_line)

        self.assertEqual(dragged_line.sweep_index, 1)
        self.assertEqual(edge_line.sweep_index, 4)
        self.assertAlmostEqual(dragged_line.value(), 1.5)
        self.assertAlmostEqual(edge_line.value(), 4.5)
        self.assertEqual(window.active_sweep_line_id, 1)

    def test_sweep_line_cursor_indicates_drag_direction(self):
        window = plot2d.__new__(plot2d)
        vertical_line = self.SweepLine(sweep_id=1, angle=90, value=0.0)
        horizontal_line = self.SweepLine(sweep_id=2, angle=0, value=0.0)

        window.set_sweep_line_cursor(vertical_line)
        window.set_sweep_line_cursor(horizontal_line)

        self.assertEqual(vertical_line.cursor_shape, QtCore.Qt.CursorShape.SizeHorCursor)
        self.assertEqual(horizontal_line.cursor_shape, QtCore.Qt.CursorShape.SizeVerCursor)

    def test_sweep_line_cursor_updates_when_line_appears_under_pointer(self):
        window = plot2d.__new__(plot2d)
        line = self.SweepLine(sweep_id=1, angle=90, value=0.0)
        window.sweep_line_contains_global_cursor = lambda current_line: current_line is line

        while qtw.QApplication.overrideCursor() is not None:
            qtw.QApplication.restoreOverrideCursor()

        try:
            window.set_sweep_line_cursor(line)

            self.assertEqual(
                qtw.QApplication.overrideCursor().shape(),
                QtCore.Qt.CursorShape.SizeHorCursor,
                )
        finally:
            window.restore_sweep_line_hover_cursor(line)
            while qtw.QApplication.overrideCursor() is not None:
                qtw.QApplication.restoreOverrideCursor()

    def test_sweep_line_hover_cursor_restores_on_exit(self):
        window = plot2d.__new__(plot2d)
        line = self.SweepLine(sweep_id=1, angle=0, value=0.0)
        window.sweep_line_contains_global_cursor = lambda _line: False

        while qtw.QApplication.overrideCursor() is not None:
            qtw.QApplication.restoreOverrideCursor()

        try:
            window.set_sweep_line_cursor(line)
            line.hoverEvent(self.SweepLineHoverEvent())

            self.assertEqual(
                qtw.QApplication.overrideCursor().shape(),
                QtCore.Qt.CursorShape.SizeVerCursor,
                )

            line.hoverEvent(self.SweepLineHoverEvent(exit=True))

            self.assertIsNone(qtw.QApplication.overrideCursor())
            self.assertEqual(len(line.hover_events), 2)
        finally:
            window.restore_sweep_line_hover_cursor(line)
            while qtw.QApplication.overrideCursor() is not None:
                qtw.QApplication.restoreOverrideCursor()

    def test_double_click_cut_line_requests_single_cut_close(self):
        window = plot2d.__new__(plot2d)
        window.close_sweeps_requested = self.SignalCatcher()
        line = self.SweepLine(sweep_id=5, angle=90, value=0.0)
        other_line = self.SweepLine(sweep_id=7, angle=90, value=0.0)
        window.sweep_lines = {5: line, 7: other_line}
        event = self.SweepLineClickEvent(double=True)

        window.activate_sweep_line(line, event)

        self.assertTrue(event.accepted)
        self.assertEqual(window.active_sweep_line_id, 5)
        self.assertEqual(window.close_sweeps_requested.calls, [(window, (5,))])

    def test_shift_double_click_cut_line_requests_all_cut_closes(self):
        window = plot2d.__new__(plot2d)
        window.close_sweeps_requested = self.SignalCatcher()
        line = self.SweepLine(sweep_id=5, angle=90, value=0.0)
        other_line = self.SweepLine(sweep_id=7, angle=0, value=0.0)
        window.sweep_lines = {7: other_line, 5: line}
        event = self.SweepLineClickEvent(
            double=True,
            modifiers=QtCore.Qt.KeyboardModifier.ShiftModifier,
            )

        window.activate_sweep_line(line, event)

        self.assertTrue(event.accepted)
        self.assertEqual(window.close_sweeps_requested.calls, [(window, (5, 7))])

    def test_sweep_line_drag_keeps_cursor_until_drag_finishes(self):
        window = plot2d.__new__(plot2d)
        line = self.SweepLine(sweep_id=1, angle=90, value=0.0)

        while qtw.QApplication.overrideCursor() is not None:
            qtw.QApplication.restoreOverrideCursor()

        try:
            window.set_sweep_line_cursor(line)
            line.mouseDragEvent(self.SweepLineDragEvent())

            self.assertEqual(
                qtw.QApplication.overrideCursor().shape(),
                QtCore.Qt.CursorShape.SizeHorCursor,
                )

            line.mouseDragEvent(self.SweepLineDragEvent(finish=True))

            self.assertIsNone(qtw.QApplication.overrideCursor())
            self.assertEqual(len(line.drag_events), 2)
        finally:
            while qtw.QApplication.overrideCursor() is not None:
                qtw.QApplication.restoreOverrideCursor()

    def test_arrow_key_moves_active_sweep_line_by_one_pixel(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 10.0, 4.0, 6.0)
        window.dataGrid = np.zeros((3, 4))
        window.sweep_moved = self.SignalCatcher()
        line = self.SweepLine(sweep_id=8, angle=90, value=1.5)
        line.sweep_index = 1
        window.sweep_lines = {8: line}
        window.active_sweep_line_id = 8

        window.move_sweep_with_arrow_key(QtCore.Qt.Key.Key_Right)

        self.assertEqual(line.sweep_index, 2)
        self.assertAlmostEqual(line.value(), 2.5)
        self.assertEqual(window.sweep_moved.calls, [(8, 2)])

    def test_arrow_key_clamps_sweep_line_to_heatmap_edge(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 10.0, 4.0, 6.0)
        window.dataGrid = np.zeros((3, 4))
        window.sweep_moved = self.SignalCatcher()
        line = self.SweepLine(sweep_id=8, angle=90, value=3.5)
        line.sweep_index = 3
        window.sweep_lines = {8: line}
        window.active_sweep_line_id = 8

        window.move_sweep_with_arrow_key(QtCore.Qt.Key.Key_Right)

        self.assertEqual(line.sweep_index, 3)
        self.assertAlmostEqual(line.value(), 3.5)
        self.assertEqual(window.sweep_moved.calls, [(8, 3)])

    def test_manual_colorbar_range_sets_levels_and_disables_refresh_autoscale(self):
        window = plot2d.__new__(plot2d)
        window.bar = self.Colorbar()
        window.relevel_refresh = self.CheckBox(checked=True)
        window._colorbar_manual_levels = None

        applied = window.setColorbarManualRange(10, 20)

        self.assertTrue(applied)
        self.assertEqual(window._colorbar_manual_levels, (10.0, 20.0))
        self.assertEqual(window.bar.values, (10, 20))
        self.assertFalse(window.relevel_refresh.checked)

    def test_auto_colorbar_clears_manual_range_and_uses_data_range(self):
        window = plot2d.__new__(plot2d)
        window.bar = self.Colorbar()
        window.relevel_refresh = self.CheckBox(checked=False)
        window._colorbar_manual_levels = (10.0, 20.0)
        window.dataGrid = np.array([[0.0, 40.0], [20.0, np.nan]])

        window.setColorbarAuto()

        self.assertIsNone(window._colorbar_manual_levels)
        self.assertTrue(window.relevel_refresh.checked)
        self.assertEqual(window.bar.values, (0.0, 40.0))

    def test_outside_colorbar_drag_widens_levels_about_midpoint(self):
        for start_y, drag_y in ((40.0, 24.0), (210.0, 226.0)):
            with self.subTest(start_y=start_y):
                window = plot2d.__new__(plot2d)
                window.bar = self.Colorbar()
                window.bar.values = (0.0, 100.0)
                window.bar.region = self.ColorbarRegion()
                window.bar.rounding = 1.0
                window.bar.horizontal = False
                window.bar.lo_lim = None
                window.bar.hi_lim = None
                window.relevel_refresh = self.CheckBox(checked=True)
                window._colorbar_manual_levels = None

                window._install_colorbar_alt_range_drag_handler(window.bar)
                start_event = self.ColorbarDragEvent(
                    start_y,
                    down_y=start_y,
                    start=True,
                    )
                move_event = self.ColorbarDragEvent(
                    drag_y,
                    down_y=start_y,
                    )
                finish_event = self.ColorbarDragEvent(
                    drag_y,
                    down_y=start_y,
                    finish=True,
                    )

                window.bar.region.mouseDragEvent(start_event)
                window.bar.region.mouseDragEvent(move_event)
                window.bar.region.mouseDragEvent(finish_event)

                self.assertTrue(start_event.accepted)
                self.assertTrue(move_event.accepted)
                self.assertTrue(finish_event.accepted)
                self.assertEqual(window.bar.values, (-6.0, 106.0))
                self.assertEqual(window._colorbar_manual_levels, (-6.0, 106.0))
                self.assertFalse(window.relevel_refresh.checked)
                self.assertEqual(window.bar.region.lines[0].position, 63.0)
                self.assertEqual(window.bar.region.lines[1].position, 191.0)

    def test_inside_colorbar_drag_keeps_pyqtgraph_range_slide_behavior(self):
        window = plot2d.__new__(plot2d)
        window.bar = self.Colorbar()
        window.bar.values = (0.0, 100.0)
        window.bar.region = self.ColorbarRegion()

        window._install_colorbar_alt_range_drag_handler(window.bar)
        event = self.ColorbarDragEvent(100.0, down_y=100.0, start=True)

        window.bar.region.mouseDragEvent(event)

        self.assertFalse(event.accepted)
        self.assertEqual(window.bar.region.previous_drag_calls, [event])

    def test_plain_colorbar_handle_drag_keeps_pyqtgraph_behavior(self):
        window = plot2d.__new__(plot2d)
        window.bar = self.Colorbar()
        window.bar.values = (0.0, 100.0)
        window.bar.region = self.ColorbarRegion()

        window._install_colorbar_alt_range_drag_handler(window.bar)
        line = window.bar.region.lines[1]
        event = self.ColorbarDragEvent(16.0)

        line.mouseDragEvent(event)

        self.assertFalse(event.accepted)
        self.assertEqual(line.previous_drag_calls, [event])

    def test_color_autoscale_button_sits_next_to_axis_autoscale_button(self):
        window = plot2d.__new__(plot2d)
        window.plot = pg.PlotItem()
        window.dataGrid = np.array([[0.0, 1.0], [2.0, 3.0]])
        calls = []
        window.scaleColorbar = lambda: calls.append(True)

        window._init_color_autoscale_button()
        window.plot.mouseHovering = True
        window._update_color_autoscale_button()

        self.assertTrue(window.color_auto_button.isVisible())
        self.assertGreater(
            window.color_auto_button.pos().x(),
            window.plot.autoBtn.pos().x(),
            )
        self.assertEqual(
            window.color_auto_button.toolTip(),
            "Autoscale color range",
            )

        window.color_auto_button.clicked.emit(window.color_auto_button)

        self.assertEqual(calls, [True])

    def test_colorbar_colormap_updates_bar_and_preference(self):
        class Config:
            def __init__(self):
                self.updates = []

            def update(self, key, value):
                self.updates.append((key, value))

        window = plot2d.__new__(plot2d)
        window.bar = self.Colorbar()
        window.config = Config()

        applied = window.setColorbarColorMap("Purples")

        self.assertTrue(applied)
        self.assertEqual(window._colorbar_colormap_name, "Purples")
        self.assertEqual(
            window.config.updates,
            [("user_preference.bar_colour", "Purples")],
            )
        self.assertIsInstance(window.bar.color_map, pg.ColorMap)

    def test_none_is_not_offered_or_applied_as_colorbar_colormap(self):
        window = plot2d.__new__(plot2d)
        window.status_messages = []
        window.show_status = lambda *args: window.status_messages.append(args)

        applied = window.setColorbarColorMap("none")

        self.assertFalse(applied)
        self.assertNotIn("none", _COLORBAR_COLORMAPS)
        self.assertEqual(window.status_messages, [("Unknown color map.", 5000)])

    def test_colorbar_colormap_config_filters_names_prefixes_and_groups(self):
        class Config:
            values = {
                "user_preference.bar_colour_include_cet": True,
                "user_preference.bar_colour_include_matplotlib": False,
                "user_preference.bar_colour_include_local": False,
                "user_preference.bar_colour_include_custom": False,
                "user_preference.bar_colour_excluded": ["Purples"],
                "user_preference.bar_colour_excluded_prefixes": ["CET-D"],
                }

            def get(self, key):
                return self.values[key]

        window = plot2d.__new__(plot2d)
        window.config = Config()

        available = window._available_colorbar_colormaps()

        self.assertNotIn("Purples", available)
        self.assertNotIn("viridis", available)
        self.assertNotIn("PAL-relaxed", available)
        self.assertNotIn("Greys", available)
        self.assertNotIn("CET-D1", available)
        self.assertNotIn("gist_yerg", available)
        self.assertNotIn("gray", available)
        self.assertNotIn("grey", available)
        self.assertNotIn("Grays", available)
        self.assertNotIn("Grays_r", available)
        self.assertIn("CET-C1", available)

    def test_colorbar_colormap_can_hide_every_source(self):
        class Config:
            values = {
                "user_preference.bar_colour_include_cet": False,
                "user_preference.bar_colour_include_matplotlib": False,
                "user_preference.bar_colour_include_local": False,
                "user_preference.bar_colour_include_custom": False,
                }

            def get(self, key):
                return self.values[key]

        window = plot2d.__new__(plot2d)
        window.config = Config()

        self.assertEqual(window._available_colorbar_colormaps(), ())
        self.assertEqual(window._fallback_colorbar_colormap_name(), "viridis")

    def test_colorbar_colormap_config_filters_subtypes(self):
        class Config:
            values = {
                "user_preference.bar_colour_include_cet": True,
                "user_preference.bar_colour_include_matplotlib": True,
                "user_preference.bar_colour_include_cet_linear": False,
                "user_preference.bar_colour_include_matplotlib_qualitative": False,
                }

            def get(self, key):
                return self.values[key]

        window = plot2d.__new__(plot2d)
        window.config = Config()

        available = window._available_colorbar_colormaps()

        self.assertNotIn("CET-L1", available)
        self.assertIn("CET-D1", available)
        self.assertNotIn("tab10", available)
        self.assertIn("viridis", available)

    def test_colorbar_tick_formatter_uses_scaled_ticks_and_unit_label(self):
        class Param:
            label = "Gate v2"
            unit = "V"

        window = plot2d.__new__(plot2d)
        window.param = Param()
        window.bar = pg.ColorBarItem(values=(-1.5e-3, 1.5e-3))

        window._set_colorbar_tick_formatter()

        axis = window.bar.axis
        self.assertEqual(
            axis.tickStrings([-1.5e-3, 0.0, 1.5e-3], axis.autoSIPrefixScale, 5e-4),
            ["-1.5", "0", "1.5"],
            )
        self.assertIn(
            "Gate v2 (10<sup>-3</sup> V)",
            window.bar.getAxis("right").labelString(),
            )
        self.assertNotIn("(x", window.bar.getAxis("right").labelString())

    def test_colorbar_label_reads_downwards(self):
        class Param:
            label = "Gate v2"
            unit = "V"

        window = plot2d.__new__(plot2d)
        window.param = Param()
        window.bar = pg.ColorBarItem(values=(-1.5e-3, 1.5e-3))

        window._set_colorbar_tick_formatter()

        self.assertEqual(window.bar.axis.label.rotation(), 90)

    def test_colorbar_tick_formatter_reserves_label_space(self):
        class Param:
            label = "Gate v2"
            unit = "V"

        window = plot2d.__new__(plot2d)
        window.param = Param()
        window.bar = self.Colorbar()
        window.bar.axis = self.Axis()

        window._set_colorbar_tick_formatter()

        self.assertNotIn("tickStrings", vars(window.bar.axis))
        self.assertEqual(window.bar.axis.width, 70)
        self.assertEqual(window.bar.axis.style["tickTextWidth"], 60)
        self.assertIsNone(window.bar.axis.picture)
        self.assertTrue(window.bar.axis.updated)


