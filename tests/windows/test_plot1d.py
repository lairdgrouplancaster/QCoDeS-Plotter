import unittest

import numpy as np
from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets as qtw
import pyqtgraph as pg

from qplot.windows import _plotWin as plotwin_module
from qplot.windows.plot1d import plot1d
from qplot.windows._subplots import custom_viewbox
from qplot.windows._plotWin import plotWidget


class SnapToTraceTestCase(unittest.TestCase):
    def test_axis_label_uses_power_scaled_units_for_auto_si_prefix(self):
        axis = plotwin_module._PowerScaledAxisItem("bottom")
        axis.setLabel(text="Gate ch2", units="V")
        axis.setRange(1e-9, 9e-9)

        self.assertIn("Gate ch2 (10<sup>-9</sup> V)", axis.labelString())
        self.assertEqual(
            axis.tickStrings([2e-9, 9e-9], axis.autoSIPrefixScale * axis.scale, 1e-9),
            ["2", "9"],
            )

    def test_axis_label_keeps_plain_units_without_auto_si_prefix(self):
        axis = plotwin_module._PowerScaledAxisItem("left")
        axis.setLabel(text="Gate ch1", units="V")
        axis.setRange(-50.0, 50.0)

        self.assertIn("Gate ch1 (V)", axis.labelString())

    def test_param_axis_labels_pass_units_separately(self):
        class Param:
            def __init__(self, label, unit):
                self.label = label
                self.unit = unit

        class Plot:
            def __init__(self):
                self.calls = []

            def setLabel(self, axis, text, units):
                self.calls.append((axis, text, units))

        window = plotWidget.__new__(plotWidget)
        window.plot = Plot()
        window.axis_param = {
            "x": Param("Gate ch2", "V"),
            "y": Param("Gate ch1", "V"),
            }

        window._set_param_axis_labels()

        self.assertEqual(
            window.plot.calls,
            [
                ("bottom", "Gate ch2", "V"),
                ("left", "Gate ch1", "V"),
                ],
            )

    def test_nearest_trace_point_uses_plotted_data_point(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[0.0, 1.0, 2.0], y=[0.0, 1.0, 4.0])
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.right_vb = None
        window.lines = {"main": line}

        scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(2.1, 3.8))

        label, x_value, y_value, viewbox, point_number = window._nearest_trace_point(scene_pos)

        self.assertEqual(label, "main")
        self.assertEqual(x_value, 2.0)
        self.assertEqual(y_value, 4.0)
        self.assertIs(viewbox, plot_item.vb)
        self.assertEqual(point_number, 3)

    def test_register_main_line_replaces_initial_empty_trace(self):
        line = object()
        window = plot1d.__new__(plot1d)
        window.label = "main"
        window.line = line
        window.lines = {"main": None}

        window._register_main_line()

        self.assertIs(window.lines["main"], line)

    def test_alt_drag_edge_handle_resizes_marquee_symmetrically(self):
        window = plotWidget.__new__(plotWidget)
        rect = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

        window._resize_marquee_rect(
            rect,
            "w",
            QtCore.QPointF(2.0, 4.0),
            QtCore.Qt.AltModifier,
            )

        self.assertEqual(rect.left(), 2.0)
        self.assertEqual(rect.right(), 8.0)

    def test_shift_drag_edge_handle_moves_opposite_edge_in_same_direction(self):
        window = plotWidget.__new__(plotWidget)
        rect = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

        window._resize_marquee_rect(
            rect,
            "w",
            QtCore.QPointF(2.0, 4.0),
            QtCore.Qt.ShiftModifier,
            )

        self.assertEqual(rect.left(), 2.0)
        self.assertEqual(rect.right(), 12.0)

    def test_alt_drag_corner_handle_resizes_marquee_symmetrically(self):
        window = plotWidget.__new__(plotWidget)
        rect = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

        window._resize_marquee_rect(
            rect,
            "nw",
            QtCore.QPointF(2.0, 6.0),
            QtCore.Qt.AltModifier,
            )

        self.assertEqual(rect.left(), 2.0)
        self.assertEqual(rect.right(), 8.0)
        self.assertEqual(rect.top(), 2.0)
        self.assertEqual(rect.bottom(), 6.0)

    def test_shift_drag_corner_handle_moves_opposite_corner_in_same_direction(self):
        window = plotWidget.__new__(plotWidget)
        rect = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

        window._resize_marquee_rect(
            rect,
            "nw",
            QtCore.QPointF(2.0, 6.0),
            QtCore.Qt.ShiftModifier,
            )

        self.assertEqual(rect.left(), 2.0)
        self.assertEqual(rect.right(), 12.0)
        self.assertEqual(rect.top(), -2.0)
        self.assertEqual(rect.bottom(), 6.0)

    def test_shift_drag_corner_uses_initial_handle_grab_offset(self):
        window = plotWidget.__new__(plotWidget)
        window.marquee = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)
        captured = []

        window.set_marquee_rect = lambda rect: captured.append(QtCore.QRectF(rect))

        window.begin_marquee_drag(QtCore.QPointF(1.0, 7.0), "nw")
        window.drag_marquee_to(QtCore.QPointF(1.0, 7.0), QtCore.Qt.ShiftModifier)

        self.assertEqual(captured[-1], QtCore.QRectF(0.0, 0.0, 10.0, 8.0))

    def test_right_click_inside_marquee_opens_marquee_context_menu(self):
        viewbox = custom_viewbox()
        calls = []

        class Owner:
            marquee = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

            def open_marquee_context_menu(self, scene_pos, global_pos=None):
                calls.append((scene_pos, global_pos))
                return True

        class Event:
            accepted = False

            def button(self):
                return QtCore.Qt.RightButton

            def scenePos(self):
                return QtCore.QPointF(1.0, 2.0)

            def screenPos(self):
                return QtCore.QPointF(20.0, 30.0)

            def accept(self):
                self.accepted = True

        event = Event()
        viewbox.set_marquee_owner(Owner())

        viewbox.mouseClickEvent(event)

        self.assertTrue(event.accepted)
        self.assertEqual(calls, [(QtCore.QPointF(1.0, 2.0), QtCore.QPoint(20, 30))])

    def test_marquee_menu_omits_zoom_color_for_1d_plots(self):
        window = plot1d.__new__(plot1d)
        window.marquee = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

        menu = window._new_marquee_context_menu()
        action_texts = [action.text() for action in menu.actions()]

        self.assertEqual(action_texts, ["Zoom", "Zoom X", "Zoom Y", "Stats..."])

    def test_1d_marquee_stats_include_axis_ranges(self):
        window = plot1d.__new__(plot1d)
        window.marquee = QtCore.QRectF(1.0, 2.0, 3.0, 4.0)
        window.axis_data = {
            "x": np.array([1.0, 2.0, 4.0, 5.0]),
            "y": np.array([2.0, 4.0, 6.0, 9.0]),
            }

        stats_text = window._marquee_stats_text()

        self.assertIn("X range: 1.000 to 4.000", stats_text)
        self.assertIn("Y range: 2.000 to 6.000", stats_text)

    def test_zoom_marquee_sets_selected_axes_without_padding(self):
        class ViewBox:
            def __init__(self):
                self.x_range = None
                self.y_range = None

            def setXRange(self, low, high, padding=0):
                self.x_range = (low, high, padding)

            def setYRange(self, low, high, padding=0):
                self.y_range = (low, high, padding)

        window = plotWidget.__new__(plotWidget)
        window.vb = ViewBox()
        window.marquee = QtCore.QRectF(1.0, 2.0, 3.0, 4.0)

        self.assertTrue(window.zoom_marquee("xy"))
        self.assertEqual(window.vb.x_range, (1.0, 4.0, 0))
        self.assertEqual(window.vb.y_range, (2.0, 6.0, 0))

    def test_escape_clears_marquee(self):
        window = qtw.QMainWindow()
        window.marquee = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)
        window.clear_marquee = lambda: setattr(window, "marquee", None)
        event = QtGui.QKeyEvent(
            QtCore.QEvent.KeyPress,
            QtCore.Qt.Key_Escape,
            QtCore.Qt.NoModifier,
            )

        plotWidget.keyPressEvent(window, event)

        self.assertIsNone(window.marquee)
        self.assertTrue(event.isAccepted())

    def test_marquee_cursor_shapes_match_define_and_resize_modes(self):
        window = plotWidget.__new__(plotWidget)
        window._marquee_drag_state = {"mode": "new"}

        self.assertEqual(
            window.marquee_cursor_shape_at(QtCore.QPointF(), QtCore.Qt.NoModifier),
            QtCore.Qt.CrossCursor,
            )

        window._marquee_drag_state = {"mode": "w"}
        self.assertEqual(
            window.marquee_cursor_shape_at(QtCore.QPointF(), QtCore.Qt.NoModifier),
            QtCore.Qt.SizeHorCursor,
            )
        self.assertEqual(
            window._marquee_cursor_shape_for_handle("ne"),
            QtCore.Qt.SizeBDiagCursor,
            )

    def test_marquee_x_edges_snap_between_points_without_changing_y(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[0.0, 1.0, 3.0], y=[4.0, 5.0, 6.0])
        window = plot1d.__new__(plot1d)
        window.line = line

        rect = window._snap_marquee_rect(QtCore.QRectF(0.6, 7.25, 1.8, 2.5))

        self.assertEqual(rect.left(), 0.5)
        self.assertEqual(rect.right(), 4.0)
        self.assertEqual(rect.top(), 7.25)
        self.assertEqual(rect.bottom(), 9.75)


