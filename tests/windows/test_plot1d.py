import unittest
from unittest.mock import patch

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.windows import _plotWin as plotwin_module
from qplot.windows._plot1d_snap import _nearest_trace_sample
from qplot.windows._plot1d_traces import (
    TRACE_COLOR_PALETTE,
    Plot1DTraceMixin,
    _TraceAppearanceDialog,
)
from qplot.windows._plotWin import plotWidget
from qplot.windows._subplots import custom_viewbox
from qplot.windows._widgets import QDock_context, picker_1d
from qplot.windows.plot1d import plot1d


class SnapToTraceTestCase(unittest.TestCase):
    def test_nearest_trace_sample_returns_none_without_finite_points(self):
        self.assertIsNone(_nearest_trace_sample([], [], 0.0))
        self.assertIsNone(
            _nearest_trace_sample([0.0, np.nan, np.inf], [np.nan, 1.0, 2.0], 0.0)
            )

    def test_nearest_trace_sample_preserves_original_point_number(self):
        sample = _nearest_trace_sample(
            [0.0, 1.0, 2.0],
            [np.nan, 10.0, 20.0],
            1.1,
            )

        self.assertIsNotNone(sample)
        self.assertEqual(sample.x_value, 1.0)
        self.assertEqual(sample.y_value, 10.0)
        self.assertEqual(sample.point_number, 2)

    def test_nearest_trace_sample_clamps_to_endpoints_outside_range(self):
        x_data = [1.0, 2.0, 3.0]
        y_data = [10.0, 20.0, 30.0]

        left = _nearest_trace_sample(x_data, y_data, -100.0)
        right = _nearest_trace_sample(x_data, y_data, 100.0)

        self.assertEqual(left.x_value, 1.0)
        self.assertEqual(left.y_value, 10.0)
        self.assertEqual(left.point_number, 1)
        self.assertEqual(right.x_value, 3.0)
        self.assertEqual(right.y_value, 30.0)
        self.assertEqual(right.point_number, 3)

    def test_nearest_trace_sample_uses_first_duplicate_x_value(self):
        sample = _nearest_trace_sample(
            [0.0, 1.0, 1.0, 2.0],
            [0.0, 10.0, 20.0, 30.0],
            1.0,
            )

        self.assertEqual(sample.x_value, 1.0)
        self.assertEqual(sample.y_value, 10.0)
        self.assertEqual(sample.point_number, 2)

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

    def test_nearest_trace_point_ignores_hidden_traces(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        hidden_line = plot_item.plot(x=[1.0], y=[100.0])
        visible_line = plot_item.plot(x=[1.0], y=[0.0])
        hidden_line.setVisible(False)
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.right_vb = None
        window.lines = {
            "hidden": hidden_line,
            "visible": visible_line,
            }

        scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(1.0, 100.0))

        label, x_value, y_value, viewbox, point_number = window._nearest_trace_point(scene_pos)

        self.assertEqual(label, "visible")
        self.assertEqual(x_value, 1.0)
        self.assertEqual(y_value, 0.0)
        self.assertIs(viewbox, plot_item.vb)
        self.assertEqual(point_number, 1)

    def test_nearest_trace_point_chooses_closest_curve_by_scene_distance(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        low_line = plot_item.plot(x=[1.0], y=[0.0])
        high_line = plot_item.plot(x=[1.0], y=[10.0])
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.right_vb = None
        window.lines = {
            "low": low_line,
            "high": high_line,
            }

        scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(1.0, 9.5))

        label, x_value, y_value, viewbox, point_number = window._nearest_trace_point(scene_pos)

        self.assertEqual(label, "high")
        self.assertEqual(x_value, 1.0)
        self.assertEqual(y_value, 10.0)
        self.assertIs(viewbox, plot_item.vb)
        self.assertEqual(point_number, 1)

    def test_mouse_moved_shows_nearest_1d_array_index(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[0.0, 2.0, 5.0], y=[0.0, 4.0, 25.0])

        class Plot:
            vb = plot_item.vb

            def sceneBoundingRect(self):
                return QtCore.QRectF(-1e9, -1e9, 2e9, 2e9)

        window = plot1d.__new__(plot1d)
        window.plot = Plot()
        window.line = line
        window.pos_labels = {
            "index": qtw.QLabel(),
            "x": qtw.QLabel(),
            "y": qtw.QLabel(),
            }
        window.formatNum = lambda value: str(value)

        try:
            scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(2.2, 9.0))

            plotWidget.mouseMoved(window, scene_pos)

            self.assertEqual(window.pos_labels["index"].text(), "[1]")
        finally:
            widget.deleteLater()

    def test_snap_mouse_moved_shows_zero_based_array_index(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[0.0, 2.0, 5.0], y=[0.0, 4.0, 25.0])

        class Plot:
            vb = plot_item.vb

            def sceneBoundingRect(self):
                return QtCore.QRectF(-1e9, -1e9, 2e9, 2e9)

        action = QtGui.QAction()
        action.setCheckable(True)
        action.setChecked(True)
        window = plot1d.__new__(plot1d)
        window.plot = Plot()
        window.right_vb = None
        window.lines = {"main": line}
        window.snap_to_trace_action = action
        window.pos_labels = {
            "index": qtw.QLabel(),
            "x": qtw.QLabel(),
            "y": qtw.QLabel(),
            }
        window.formatNum = lambda value: str(value)
        window._show_snap_report = lambda *_args: None
        window._show_snap_marker = lambda *_args: None

        try:
            scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(4.8, 24.0))

            plot1d.mouseMoved(window, scene_pos)

            self.assertEqual(window.pos_labels["index"].text(), "[2]")
            self.assertEqual(window.pos_labels["x"].text(), "x = 5.0;")
            self.assertEqual(window.pos_labels["y"].text(), "y = 25.0")
        finally:
            action.deleteLater()
            widget.deleteLater()

    def test_register_main_line_defers_until_line_exists(self):
        window = plot1d.__new__(plot1d)
        window.label = "main"
        window.line = None
        window.lines = {}

        window._register_main_line()

        self.assertEqual(window.lines, {})
        self.assertIn("main", window._trace_styles)

    def test_register_main_line_applies_initial_trace_palette_style(self):
        class Theme:
            colors = [QtGui.QColor("#008000")]

        class Config:
            theme = Theme()

        class Line:
            def __init__(self):
                self.pen = None
                self.visible = None

            def setPen(self, pen):
                self.pen = pen

            def setSymbolPen(self, _pen):
                pass

            def setSymbolBrush(self, _brush):
                pass

            def setSymbolSize(self, _size):
                pass

            def setSymbol(self, _symbol):
                pass

            def setVisible(self, visible):
                self.visible = visible

        line = Line()
        window = plot1d.__new__(plot1d)
        window.config = Config()
        window.label = "main"
        window.line = line
        window.lines = {}

        window._register_main_line()

        style = window._trace_styles["main"]
        self.assertEqual(style.line_color, TRACE_COLOR_PALETTE[0])
        self.assertEqual(line.pen.color().name(), TRACE_COLOR_PALETTE[0])
        self.assertEqual(line.pen.widthF(), style.line_width)
        self.assertTrue(line.visible)

    def test_main_line_color_change_before_line_exists_is_stored(self):
        window = plot1d.__new__(plot1d)
        window.label = "main"
        window.line = None

        window._set_main_line_color(QtGui.QColor("#ff0000"))

        self.assertEqual(window._trace_styles["main"].line_color, "#ff0000")

    def test_trace_appearance_action_is_added_to_view_menu(self):
        class BaseWindow(qtw.QMainWindow):
            def initMenu(self):
                self.menuBar().addMenu("&View")

        class Host(Plot1DTraceMixin, BaseWindow):
            pass

        host = Host()
        try:
            host.initMenu()

            view_menu = next(
                action.menu()
                for action in host.menuBar().actions()
                if action.text().replace("&", "") == "View"
                )
            action_texts = [action.text() for action in view_menu.actions()]

            self.assertIn("Trace Appearance…", action_texts)
        finally:
            host.deleteLater()

    def test_trace_appearance_table_uses_readonly_preview_rows(self):
        class Param:
            name = "current"

        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = Param()
            host.lines = {host.label: object()}
            host._trace_styles = {
                host.label: host._TraceStyle(line_color="#d62728", dots_enabled=True)
                }

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            preview_item = dialog.table.item(0, 1)
            measurement_item = dialog.table.item(0, 2)

            self.assertEqual(
                dialog.table.editTriggers(),
                qtw.QAbstractItemView.EditTrigger.NoEditTriggers,
                )
            self.assertEqual(
                dialog.table.horizontalHeader().sectionResizeMode(2),
                qtw.QHeaderView.ResizeMode.Stretch,
                )
            self.assertEqual(dialog.table.columnCount(), 3)
            self.assertEqual(
                dialog.table.horizontalScrollBarPolicy(),
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                )
            self.assertFalse(hasattr(dialog, "order"))
            self.assertEqual(dialog.table.rowHeight(0), 28)
            self.assertEqual(preview_item.text(), "")
            self.assertFalse(preview_item.icon().isNull())
            self.assertTrue(preview_item.flags() & QtCore.Qt.ItemFlag.ItemIsDragEnabled)
            self.assertFalse(measurement_item.flags() & QtCore.Qt.ItemFlag.ItemIsEditable)
            self.assertFalse(dialog.line_color.itemIcon(0).isNull())
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_uses_tab10_color_palette_with_custom_choice(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = type("Param", (), {"name": "current"})()
            host.lines = {host.label: object()}
            host._trace_styles = {host.label: host._initial_trace_style()}

            dialog = _TraceAppearanceDialog(host)

            colors = [
                dialog.line_color.itemData(index, QtCore.Qt.ItemDataRole.UserRole)
                for index in range(len(TRACE_COLOR_PALETTE))
                ]
            self.assertEqual(colors, list(TRACE_COLOR_PALETTE))
            self.assertEqual(
                dialog.line_color.itemData(
                    len(TRACE_COLOR_PALETTE),
                    QtCore.Qt.ItemDataRole.UserRole,
                    ),
                dialog._CUSTOM_COLOR_DATA,
                )
            self.assertEqual(dialog.line_color.itemText(len(TRACE_COLOR_PALETTE)), "Custom")
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_custom_color_updates_selected_trace(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        class Line:
            def setPen(self, pen):
                self.pen = pen

            def setSymbolPen(self, _pen):
                pass

            def setSymbolBrush(self, _brush):
                pass

            def setSymbolSize(self, _size):
                pass

            def setSymbol(self, _symbol):
                pass

            def setVisible(self, _visible):
                pass

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = type("Param", (), {"name": "current"})()
            host.lines = {host.label: Line()}
            host._trace_styles = {host.label: host._initial_trace_style()}

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            custom_index = dialog.line_color.findData(dialog._CUSTOM_COLOR_DATA)
            with patch.object(
                    qtw.QColorDialog,
                    "getColor",
                    return_value=QtGui.QColor("#123456"),
                    ):
                dialog.line_color.setCurrentIndex(custom_index)

            self.assertEqual(host._trace_styles[host.label].line_color, "#123456")
            self.assertEqual(dialog.line_color.currentData(), "#123456")
            self.assertEqual(host.lines[host.label].pen.color().name(), "#123456")
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_plot_order_follows_table_position(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        class Line:
            def __init__(self):
                self.z_value = None

            def setZValue(self, value):
                self.z_value = value

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = type("Param", (), {"name": "current"})()
            host.lines = {
                "trace a": Line(),
                "trace b": Line(),
                "trace c": Line(),
                }
            host._trace_styles = {
                label: host._TraceStyle(order=-1)
                for label in host.lines
                }

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            self.assertEqual(
                [host._trace_styles[label].order for label in host.lines],
                [2, 1, 0],
                )
            self.assertEqual([line.z_value for line in host.lines.values()], [2, 1, 0])

            dialog.table.clearSelection()
            dialog.table.selectRow(1)
            dialog._move_selected_rows(-1)

            self.assertEqual(list(host.lines), ["trace b", "trace a", "trace c"])
            self.assertEqual(
                [host._trace_styles[label].order for label in host.lines],
                [2, 1, 0],
                )
            self.assertEqual(
                [line.z_value for line in host.lines.values()],
                [2, 1, 0],
                )
            self.assertEqual(dialog._selected_labels(), ["trace b"])
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_register_main_line_replaces_initial_empty_trace(self):
        line = object()
        window = plot1d.__new__(plot1d)
        window.label = "main"
        window.line = line
        window.lines = {"main": None}

        window._register_main_line()

        self.assertIs(window.lines["main"], line)

    def test_add_and_remove_secondary_trace_manages_right_axis(self):
        class Theme:
            colors = [
                QtGui.QColor("#008000"),
                QtGui.QColor("#0000ff"),
                QtGui.QColor("#ff0000"),
                ]

        class Config:
            theme = Theme()

        class Dataset:
            running = False

        class Worker:
            running = False

        class SourceWindow:
            label = "ID:2 voltage"
            _guid = "source-guid"
            visible = False
            ds = Dataset()
            worker = Worker()
            monitor = QtCore.QTimer()
            param_dict = {}
            axis_options = {"x": "gate", "y": "voltage"}
            axis_data = {
                "x": np.array([0.0, 1.0, 2.0]),
                "y": np.array([3.0, 4.0, 5.0]),
                }

            class EndWait:
                def connect(self, _slot):
                    pass

                def disconnect(self, _slot):
                    pass

            end_wait = EndWait()

        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            make_ds = QtCore.pyqtSignal([str])
            remove_dataset = QtCore.pyqtSignal([str])
            get_mergables = QtCore.pyqtSignal()

        host = Host()
        source = SourceWindow()
        made_datasets = []
        removed_datasets = []
        picker_updates = []

        try:
            host.config = Config()
            host.widget = pg.GraphicsLayoutWidget()
            host.vb = custom_viewbox()
            host.vb.setDefaultPadding(0)
            host.plot = host.widget.addPlot(viewBox=host.vb)
            host.vb.setParent(host.plot)
            host.axes_dock = QDock_context("Line control", host)
            host.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, host.axes_dock)
            host.lineScroll = qtw.QScrollArea()
            host.scrollWidget = qtw.QWidget()
            host.lineScroll.setWidget(host.scrollWidget)
            host.box_layout = qtw.QVBoxLayout(host.scrollWidget)
            host.box_layout.addStretch()
            host.box_count = 1
            host.right_vb = None
            host.line = host.plot.plot(x=[0.0, 1.0], y=[1.0, 2.0])
            host.lines = {"main": host.line}
            host.axis_options = {"x": "gate", "y": "current"}
            host.mergable = [source]
            host.make_ds.connect(made_datasets.append)
            host.remove_dataset.connect(removed_datasets.append)
            host.get_mergables.connect(lambda: picker_updates.append(True))

            selected_box = picker_1d(host, host.config, [source.label])
            selected_box.option_box.setCurrentIndex(0)
            selected_box.axis_side.setCurrentText("Right")
            selected_box.color_box.setColor(host.config.theme.colors[1])
            host.option_boxes = [selected_box]

            host.add_line(source.label)
            secondary = host.lines[source.label]

            self.assertEqual(made_datasets, ["source-guid"])
            self.assertEqual(host.mergable, [])
            self.assertIsNotNone(host.right_vb)
            self.assertEqual(secondary.side, "right")
            self.assertIn(secondary, host.right_vb.addedItems)
            self.assertTrue(host.plot.getAxis("right").style["showValues"])
            self.assertEqual(host.option_boxes[0], selected_box)
            self.assertEqual(len(host.option_boxes), 2)

            host.remove_line(source.label)

            self.assertNotIn(source.label, host.lines)
            self.assertNotIn(secondary, host.right_vb.addedItems)
            self.assertFalse(host.plot.getAxis("right").style["showValues"])
            self.assertEqual(removed_datasets, ["source-guid"])
            self.assertEqual(picker_updates, [True])
        finally:
            host.deleteLater()

    def test_alt_drag_edge_handle_resizes_marquee_symmetrically(self):
        window = plotWidget.__new__(plotWidget)
        rect = QtCore.QRectF(0.0, 0.0, 10.0, 8.0)

        window._resize_marquee_rect(
            rect,
            "w",
            QtCore.QPointF(2.0, 4.0),
            QtCore.Qt.KeyboardModifier.AltModifier,
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
            QtCore.Qt.KeyboardModifier.ShiftModifier,
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
            QtCore.Qt.KeyboardModifier.AltModifier,
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
            QtCore.Qt.KeyboardModifier.ShiftModifier,
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
        window.drag_marquee_to(QtCore.QPointF(1.0, 7.0), QtCore.Qt.KeyboardModifier.ShiftModifier)

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
                return QtCore.Qt.MouseButton.RightButton

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
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_Escape,
            QtCore.Qt.KeyboardModifier.NoModifier,
            )

        plotWidget.keyPressEvent(window, event)

        self.assertIsNone(window.marquee)
        self.assertTrue(event.isAccepted())

    def test_marquee_cursor_shapes_match_define_and_resize_modes(self):
        window = plotWidget.__new__(plotWidget)
        window._marquee_drag_state = {"mode": "new"}

        self.assertEqual(
            window.marquee_cursor_shape_at(QtCore.QPointF(), QtCore.Qt.KeyboardModifier.NoModifier),
            QtCore.Qt.CursorShape.CrossCursor,
            )

        window._marquee_drag_state = {"mode": "w"}
        self.assertEqual(
            window.marquee_cursor_shape_at(QtCore.QPointF(), QtCore.Qt.KeyboardModifier.NoModifier),
            QtCore.Qt.CursorShape.SizeHorCursor,
            )
        self.assertEqual(
            window._marquee_cursor_shape_for_handle("ne"),
            QtCore.Qt.CursorShape.SizeBDiagCursor,
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
