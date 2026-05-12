import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets as qtw
import pyqtgraph as pg

from qplot.configuration.config import config
from qplot.configuration.scripts import scripts, sysHandle, try_as_num
from qplot.configuration.themes import dark, light
from qplot.windows import main as main_window
from qplot.windows import _plotWin as plotwin_module
from qplot.windows.plot1d import plot1d
from qplot.windows.plot2d import _COLORBAR_COLORMAPS, plot2d
from qplot.windows._subplots import custom_viewbox
from qplot.windows._plotWin import plotWidget
from qplot.windows._window_controls import (
    add_confirmation_options,
    add_restore_defaults_option,
    )
from qplot.windows._dragdrop import (
    make_run_preview_mime,
    preview_drop_is_compatible,
    run_preview_payload_from_mime,
    )
from qplot.windows._widgets.operations import operations_options_1d
from qplot.windows._widgets.toolbar import QDock_context
from qplot.windows._widgets import treeWidgets
from qplot.windows._widgets.preview import (
    DraggablePreviewImageLabel,
    PREVIEW_BACKGROUND_COLOR,
    PREVIEW_SIZE,
    PREVIEW_SELECTED_PROPERTY,
    PreviewTab,
    generate_run_previews,
    render_heatmap_preview,
    render_sparkline_preview,
    )
from qplot.datahandling import readSQL
from qplot.tools.general import data2matrix
from qplot.tools.plot_tools import differentiate, pass_filter, subtract_mean


class TemporaryConfigTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_default_path = config.default_path
        self.old_default_file = config.default_file

        config.default_path = str(Path(self.temp_dir.name) / ".qplot")
        config.default_file = str(Path(config.default_path) / config.config_file_name)

    def tearDown(self):
        config.default_path = self.old_default_path
        config.default_file = self.old_default_file
        self.temp_dir.cleanup()

    def test_config_update_writes_and_reloads_value(self):
        cfg = config()

        cfg.update("user_preference.theme", "dark")

        self.assertEqual(config().get("user_preference.theme"), "dark")
        self.assertIs(config().theme, dark)

    def test_config_accepts_extra_color_map_preferences(self):
        cfg = config()

        cfg.update("user_preference.bar_colour", "CET-L1")

        self.assertEqual(config().get("user_preference.bar_colour"), "CET-L1")

    def test_config_update_rejects_unknown_key(self):
        cfg = config()

        with self.assertRaises(KeyError):
            cfg.update("user_preference.missing", True)

    def test_config_cli_set_value_converts_values(self):
        with redirect_stdout(io.StringIO()):
            sysHandle("-set_value", "user_preference.theme", "dark")
            sysHandle("-set_value", "user_preference.confirm_close", "false")
            sysHandle("-set_value", "user_preference.confirm_close_all", "false")
            sysHandle("-set_value", "GUI.main_frame_size", "[900, 600]")
            sysHandle("-set_value", "GUI.preview_size", "300")

        cfg = config()
        self.assertEqual(cfg.get("user_preference.theme"), "dark")
        self.assertFalse(cfg.get("user_preference.confirm_close"))
        self.assertFalse(cfg.get("user_preference.confirm_close_all"))
        self.assertEqual(cfg.get("GUI.main_frame_size"), [900, 600])
        self.assertEqual(cfg.get("GUI.preview_size"), 300)

    def test_config_load_adds_new_defaults_to_existing_file(self):
        cfg = config()
        stored_config = cfg.config
        del stored_config["user_preference"]["confirm_close_all"]
        with open(config.default_file, "w") as fp:
            json.dump(stored_config, fp)

        reloaded = config()

        self.assertTrue(reloaded.get("user_preference.confirm_close_all"))

    def test_config_repr_returns_readable_json(self):
        cfg = config()

        self.assertEqual(repr(cfg), str(cfg))
        self.assertIn('"user_preference"', repr(cfg))

    def test_invalid_config_is_backed_up_and_reset_without_prompt(self):
        cfg = config()
        cfg.update("user_preference.theme", "dark")

        invalid_config = cfg.config
        invalid_config["user_preference"]["theme"] = "missing-theme"
        with open(config.default_file, "w") as fp:
            json.dump(invalid_config, fp)

        reloaded = config()

        self.assertEqual(reloaded.get("user_preference.theme"), "light")
        self.assertTrue(os.path.isfile(reloaded.invalid_config_backup_file))
        with open(reloaded.invalid_config_backup_file) as fp:
            backup = json.load(fp)
        self.assertEqual(backup["user_preference"]["theme"], "missing-theme")

    def test_scripts_without_arguments_shows_command_info(self):
        old_argv = sys.argv
        sys.argv = ["qplot-cfg"]
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                scripts()
        finally:
            sys.argv = old_argv

        self.assertIn("Valid Commands", output.getvalue())

    def test_main_window_uses_configured_default_refresh_rate(self):
        cfg = config()
        cfg.update("user_preference.default_refresh_rate", 3.5)
        window = main_window.MainWindow()

        try:
            self.assertEqual(window.spinBox.value(), 3.5)
        finally:
            window.monitor.stop()
            window.deleteLater()

    def test_main_window_loads_last_database_on_startup_when_available(self):
        cfg = config()
        calls = []
        old_load_database_path = main_window.MainWindow.load_database_path

        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            cfg.update("file.last_file_path", database.name)

            def load_database_path(window, filename):
                calls.append(filename)
                window.fileTextbox.setText(filename)
                return True

            main_window.MainWindow.load_database_path = load_database_path
            window = None
            try:
                window = main_window.MainWindow()
                self.assertEqual(calls, [os.path.abspath(database.name)])
            finally:
                main_window.MainWindow.load_database_path = old_load_database_path
                if window is not None:
                    window.monitor.stop()
                    window.deleteLater()

    def test_main_window_ignores_missing_last_database_on_startup(self):
        cfg = config()
        missing_database = str(Path(self.temp_dir.name) / "missing.db")
        cfg.update("file.last_file_path", missing_database)
        calls = []
        old_load_database_path = main_window.MainWindow.load_database_path

        def load_database_path(window, filename):
            calls.append(filename)
            return True

        main_window.MainWindow.load_database_path = load_database_path
        window = None
        try:
            window = main_window.MainWindow()
            self.assertEqual(calls, [])
        finally:
            main_window.MainWindow.load_database_path = old_load_database_path
            if window is not None:
                window.monitor.stop()
                window.deleteLater()

    def test_default_refresh_rate_is_one_second(self):
        cfg = config()

        self.assertEqual(cfg.get("user_preference.default_refresh_rate"), 1)


class ToolFunctionTestCase(unittest.TestCase):
    def test_try_as_num_handles_int_float_scientific_and_string(self):
        self.assertEqual(try_as_num("4"), 4)
        self.assertEqual(try_as_num("4.5"), 4.5)
        self.assertEqual(try_as_num("1e-3"), 1e-3)
        self.assertEqual(try_as_num("dark"), "dark")

    def test_data2matrix_pivots_flat_scan_data(self):
        matrix = data2matrix(
            np.array([0, 0, 1, 1]),
            np.array([0, 1, 0, 1]),
            np.array([10, 11, 12, 13]),
        )

        self.assertEqual(matrix.loc[0, 0], 10)
        self.assertEqual(matrix.loc[1, 1], 13)

    def test_plot_operations_return_updated_arrays(self):
        data = {
            "x": np.array([1.0, 2.0, 4.0]),
            "y": np.array([2.0, 4.0, 8.0]),
            "z": None,
        }

        filtered = pass_filter("low", 5.0, data)
        differentiated = differentiate("x", data)

        np.testing.assert_array_equal(filtered["y"], np.array([2.0, 4.0, 5.0]))
        np.testing.assert_allclose(differentiated["y"], np.array([2.0, 2.0, 2.0]))

    def test_subtract_mean_operates_by_axis(self):
        data = {
            "x": np.array([0.0, 1.0]),
            "y": np.array([0.0, 1.0]),
            "z": np.array([[1.0, 3.0], [2.0, 4.0]]),
        }

        result = subtract_mean("x", data)

        np.testing.assert_array_equal(result["z"], np.array([[-1.0, 1.0], [-1.0, 1.0]]))

    def test_plot_window_title_uses_database_basename(self):
        old_get_db_location = plotwin_module.get_DB_location
        plotwin_module.get_DB_location = lambda: "/tmp/qplot/example.db"

        class Dataset:
            run_id = 12

        class Param:
            name = "signal"
            label = "Signal"

        window = plotWidget.__new__(plotWidget)
        window._guid = "guid"
        window._dataset_holder = {
            "guid": {
                "dataset": Dataset(),
                "del_timer": None,
                }
            }
        window.param = Param()

        try:
            self.assertTrue(str(window).startswith("example.db | Run ID: 12"))
        finally:
            plotwin_module.get_DB_location = old_get_db_location


class SnapToTraceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

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


class HeatmapHoverOutlineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

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

        def setBounds(self, bounds):
            self.bounds = bounds

        def setPos(self, value):
            self._value = value

        def value(self):
            return self._value

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
                modifiers=QtCore.Qt.NoModifier,
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
            return QtCore.Qt.LeftButton

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
            QtCore.Qt.ShiftModifier,
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
        self.assertIn("2x2 points", opened[0])
        self.assertIn("X range: 1.000 to 3.000", opened[0])
        self.assertIn("Y range: 1.000 to 3.000", opened[0])
        self.assertEqual(qtw.QApplication.clipboard().text(), "")
        self.assertIsNone(window.marquee)

    def test_stats_dialog_copy_button_copies_displayed_stats(self):
        class Host(qtw.QMainWindow):
            _new_marquee_stats_dialog = plotWidget._new_marquee_stats_dialog
            copy_marquee_stats_to_clipboard = plotWidget.copy_marquee_stats_to_clipboard

        host = Host()
        stats_text = "2x2 points\nAverage: 7.5"
        qtw.QApplication.clipboard().clear()

        dialog = host._new_marquee_stats_dialog(stats_text)
        copy_button = next(
            button for button in dialog.findChildren(qtw.QPushButton)
            if button.text() == "Copy"
            )
        copy_button.click()

        self.assertEqual(dialog.findChild(qtw.QPlainTextEdit).toPlainText(), stats_text)
        self.assertEqual(qtw.QApplication.clipboard().text(), stats_text)

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

    def test_arrow_key_moves_active_sweep_line_by_one_pixel(self):
        window = plot2d.__new__(plot2d)
        window.rect = QtCore.QRectF(0.0, 10.0, 4.0, 6.0)
        window.dataGrid = np.zeros((3, 4))
        window.sweep_moved = self.SignalCatcher()
        line = self.SweepLine(sweep_id=8, angle=90, value=1.5)
        line.sweep_index = 1
        window.sweep_lines = {8: line}
        window.active_sweep_line_id = 8

        window.move_sweep_with_arrow_key(QtCore.Qt.Key_Right)

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

        window.move_sweep_with_arrow_key(QtCore.Qt.Key_Right)

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


class RunListParentLookupTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_main_window_lookup_works_through_splitter(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False
        main = None

        try:
            main = qtw.QMainWindow()
            main.ds = object()
            main.openPlot = lambda *args, **kwargs: None

            frame = qtw.QFrame()
            layout = qtw.QVBoxLayout(frame)
            splitter = qtw.QSplitter(QtCore.Qt.Vertical)
            run_list = treeWidgets.RunList()
            splitter.addWidget(run_list)
            splitter.addWidget(qtw.QTreeWidget())
            layout.addWidget(splitter)
            main.setCentralWidget(frame)

            self.assertIs(run_list.main_window(), main)
        finally:
            treeWidgets.isfile = old_isfile
            if main is not None:
                main.deleteLater()

    def test_run_context_menu_keeps_plot_actions_without_add_actions(self):
        old_isfile = treeWidgets.isfile
        old_exec = qtw.QMenu.exec_
        treeWidgets.isfile = lambda _: False
        captured = []
        main = None

        class Param:
            def __init__(self, name, depends_on):
                self.name = name
                self.depends_on = depends_on
                self.depends_on_ = (depends_on,)

        class Dataset:
            def get_parameters(self):
                return [Param("signal", "x"), Param("image", "y")]

        def capture_menu(menu, *_args, **_kwargs):
            captured.extend(action.text() for action in menu.actions())

        try:
            qtw.QMenu.exec_ = capture_menu
            main = qtw.QMainWindow()
            main.ds = Dataset()
            main.windows = []
            main.openPlot = lambda *args, **kwargs: None
            main.open_selected_run_all = lambda: None
            main.show_status = lambda *args, **kwargs: None

            run_list = treeWidgets.RunList()
            main.setCentralWidget(run_list)

            run_list.prepareMenu(QtCore.QPoint(0, 0))

            self.assertEqual(captured[0], "&Plot all")
            self.assertIn("&Plot all", captured)
            self.assertIn("  - signal", captured)
            self.assertIn("  - image", captured)
            self.assertNotIn("Add to open plot", captured)
            self.assertFalse(any(action.startswith("Add ") for action in captured))
            self.assertFalse(any(action.startswith("  - Add ") for action in captured))
        finally:
            qtw.QMenu.exec_ = old_exec
            treeWidgets.isfile = old_isfile
            if main is not None:
                main.deleteLater()

    def test_plot_export_is_removed_from_scene_context_menu(self):
        widget = pg.GraphicsLayoutWidget()
        fake_plot = type("FakePlot", (), {"widget": widget})()

        try:
            self.assertIn(
                "Export...",
                [action.text().replace("&", "") for action in widget.scene().contextMenu],
                )

            plotWidget._remove_scene_export_context_menu(fake_plot)

            self.assertNotIn(
                "Export...",
                [action.text().replace("&", "") for action in widget.scene().contextMenu],
                )
        finally:
            widget.deleteLater()

    def test_plot_export_action_has_keyboard_shortcut(self):
        class Host(qtw.QMainWindow):
            initContextMenu = plotWidget.initContextMenu
            register_shortcut = plotWidget.register_shortcut
            _remove_scene_export_context_menu = plotWidget._remove_scene_export_context_menu
            _context_menu_action = plotWidget._context_menu_action

            def _init_axis_scale_dialogs(self):
                pass

            def open_context_menu(self):
                pass

            def open_export_dialog(self):
                self.export_opened = True

        widget = pg.GraphicsLayoutWidget()
        host = Host()
        host.widget = widget
        host.plot = widget.addPlot()
        host.vb = host.plot.vb
        host.oper_dock = qtw.QDockWidget()
        host.export_opened = False

        try:
            host.initContextMenu()

            self.assertEqual(
                host.exportPlotAction.shortcut().toString(),
                "Ctrl+E",
                )
            self.assertEqual(
                host.exportPlotAction.shortcutContext(),
                QtCore.Qt.WindowShortcut,
                )
            self.assertIn(host.exportPlotAction, host.actions())

            host.exportPlotAction.trigger()

            self.assertTrue(host.export_opened)
        finally:
            host.deleteLater()
            widget.deleteLater()

    def test_axis_scale_controls_move_from_context_menu_to_dialogs(self):
        class Host(qtw.QMainWindow):
            initContextMenu = plotWidget.initContextMenu
            _init_axis_scale_dialogs = plotWidget._init_axis_scale_dialogs
            _menu_control_widget = plotWidget._menu_control_widget
            _install_axis_scale_double_click_handlers = (
                plotWidget._install_axis_scale_double_click_handlers
                )
            _axis_scale_dialog_title = plotWidget._axis_scale_dialog_title
            _axis_scale_axis_number = plotWidget._axis_scale_axis_number
            _axis_scale_axis_constant = plotWidget._axis_scale_axis_constant
            _new_axis_scale_controls = plotWidget._new_axis_scale_controls
            _sync_axis_scale_controls = plotWidget._sync_axis_scale_controls
            _sync_axis_scale_link_combo = plotWidget._sync_axis_scale_link_combo
            _axis_scale_mouse_toggled = plotWidget._axis_scale_mouse_toggled
            _axis_scale_manual_clicked = plotWidget._axis_scale_manual_clicked
            _axis_scale_range_text_changed = plotWidget._axis_scale_range_text_changed
            _axis_scale_auto_clicked = plotWidget._axis_scale_auto_clicked
            _axis_scale_auto_spin_changed = plotWidget._axis_scale_auto_spin_changed
            _axis_scale_link_changed = plotWidget._axis_scale_link_changed
            _axis_scale_auto_pan_toggled = plotWidget._axis_scale_auto_pan_toggled
            _axis_scale_visible_only_toggled = plotWidget._axis_scale_visible_only_toggled
            _axis_scale_invert_toggled = plotWidget._axis_scale_invert_toggled
            open_axis_scale_dialog = plotWidget.open_axis_scale_dialog
            _remove_scene_export_context_menu = plotWidget._remove_scene_export_context_menu
            _context_menu_action = plotWidget._context_menu_action

            def register_shortcut(self, *_args, **_kwargs):
                pass

            def open_context_menu(self):
                pass

            def open_export_dialog(self):
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

            self.assertNotIn("X axis", action_texts)
            self.assertNotIn("Y axis", action_texts)

            host.open_axis_scale_dialog("x")

            dialog = host._axis_scale_dialogs["x"]
            self.assertEqual(dialog.windowTitle(), "X axis scaling")
            self.assertIn("x", host._axis_scale_controls)
            self.assertEqual(host._axis_scale_controls["x"].manualRadio.text(), "Manual")
            self.assertEqual(host._axis_scale_controls["x"].autoRadio.text(), "Auto")
            self.assertEqual(host._axis_scale_controls["x"].invertCheck.text(), "Invert Axis")
        finally:
            host.deleteLater()
            widget.deleteLater()

    def test_colorbar_scale_action_opens_dialog_without_nested_menu(self):
        class Bar:
            def __init__(self):
                self.color_map = None

            def levels(self):
                return 1.0, 2.0

            def setColorMap(self, color_map):
                self.color_map = color_map

        class Config:
            def __init__(self):
                self.values = {
                "user_preference.bar_colour": "viridis",
                "user_preference.bar_colour_include_cet": True,
                "user_preference.bar_colour_include_matplotlib": True,
                "user_preference.bar_colour_include_local": True,
                "user_preference.bar_colour_include_custom": True,
                "user_preference.bar_colour_excluded": [],
                "user_preference.bar_colour_excluded_prefixes": [],
                }
                self.updates = []

            def get(self, key):
                self.get_key = key
                return self.values[key]

            def update(self, key, value):
                self.values[key] = value
                self.updates.append((key, value))

        class Host(qtw.QMainWindow):
            _current_colorbar_levels = plot2d._current_colorbar_levels
            _current_colorbar_colormap_name = plot2d._current_colorbar_colormap_name
            _available_colorbar_colormaps = plot2d._available_colorbar_colormaps
            _fallback_colorbar_colormap_name = plot2d._fallback_colorbar_colormap_name
            _colorbar_colormap = plot2d._colorbar_colormap
            _init_colorbar_scale_controls = plot2d._init_colorbar_scale_controls
            _init_colorbar_filter_controls = plot2d._init_colorbar_filter_controls
            _init_colorbar_colormap_table = plot2d._init_colorbar_colormap_table
            _populate_colorbar_colormap_table = plot2d._populate_colorbar_colormap_table
            _sync_colorbar_filter_controls = plot2d._sync_colorbar_filter_controls
            _set_colorbar_filter_setting = plot2d._set_colorbar_filter_setting
            _set_colorbar_subtype_filter_setting = (
                plot2d._set_colorbar_subtype_filter_setting
                )
            _colorbar_include_cet_changed = plot2d._colorbar_include_cet_changed
            _colorbar_include_matplotlib_changed = (
                plot2d._colorbar_include_matplotlib_changed
                )
            _colorbar_include_local_changed = plot2d._colorbar_include_local_changed
            _colorbar_include_custom_changed = plot2d._colorbar_include_custom_changed
            _colorbar_include_subtype_changed = plot2d._colorbar_include_subtype_changed
            _install_colorbar_scale_bar_handlers = plot2d._install_colorbar_scale_bar_handlers
            _install_colorbar_scale_axis_handlers = plot2d._install_colorbar_scale_axis_handlers
            _install_colorbar_scale_double_click_handler = (
                plot2d._install_colorbar_scale_double_click_handler
                )
            _suppress_colorbar_right_click_menu = plot2d._suppress_colorbar_right_click_menu
            _install_colorbar_level_sync_handlers = (
                plot2d._install_colorbar_level_sync_handlers
                )
            _install_colorbar_alt_range_drag_handler = (
                plot2d._install_colorbar_alt_range_drag_handler
                )
            _install_colorbar_alt_handle_drag_handler = (
                plot2d._install_colorbar_alt_handle_drag_handler
                )
            _colorbar_alt_range_drag_event = plot2d._colorbar_alt_range_drag_event
            _colorbar_alt_range_drag_axis_position = (
                plot2d._colorbar_alt_range_drag_axis_position
                )
            _set_colorbar_alt_range_drag_visual = (
                plot2d._set_colorbar_alt_range_drag_visual
                )
            _colorbar_alt_range_drag_levels = plot2d._colorbar_alt_range_drag_levels
            _colorbar_levels_from_bar = plot2d._colorbar_levels_from_bar
            _colorbar_interactive_levels_changed = (
                plot2d._colorbar_interactive_levels_changed
                )
            _colorbar_interactive_levels_finished = (
                plot2d._colorbar_interactive_levels_finished
                )
            _sync_colorbar_scale_controls = plot2d._sync_colorbar_scale_controls
            _sync_colorbar_level_fields = plot2d._sync_colorbar_level_fields
            _colorbar_colormap_row = plot2d._colorbar_colormap_row
            _select_colorbar_colormap = plot2d._select_colorbar_colormap
            _colorbar_colormap_selection_changed = (
                plot2d._colorbar_colormap_selection_changed
                )
            _colorbar_colormap_changed = plot2d._colorbar_colormap_changed
            open_colorbar_scale_dialog = plot2d.open_colorbar_scale_dialog
            _apply_colorbar_manual_fields = plot2d._apply_colorbar_manual_fields
            setColorbarColorMap = plot2d.setColorbarColorMap
            setColorbarManualRange = plot2d.setColorbarManualRange
            setColorbarAuto = plot2d.setColorbarAuto
            scaleColorbar = plot2d.scaleColorbar

            def show_status(self, *_args, **_kwargs):
                pass

        host = Host()
        host.vbMenu = qtw.QMenu(host)
        host.autoscaleSep = host.vbMenu.addSeparator()
        host.bar = Bar()
        host.config = Config()
        host._colorbar_manual_levels = None

        class Axis:
            pass

        class MouseEvent:
            def __init__(self, button):
                self._button = button
                self.accepted = False

            def button(self):
                return self._button

            def accept(self):
                self.accepted = True

        try:
            host._init_colorbar_scale_controls()
            action_texts = [action.text().replace("&", "") for action in host.vbMenu.actions()]

            self.assertNotIn("Color Scale...", action_texts)
            self.assertGreater(host._colorbar_colormap_row("Greys"), -1)
            self.assertGreater(host._colorbar_colormap_row("Purples"), -1)
            self.assertGreater(host._colorbar_colormap_row("CET-C1"), -1)
            self.assertGreater(host._colorbar_colormap_row("PAL-relaxed"), -1)
            self.assertEqual(host.colorbar_colormap_table.columnCount(), 3)
            self.assertTrue(host.colorbar_colormap_table.isSortingEnabled())
            self.assertEqual(
                host.colorbar_colormap_table.rowCount(),
                len(host._available_colorbar_colormaps()),
                )
            type_item = host.colorbar_colormap_table.item(
                host._colorbar_colormap_row("viridis"),
                2,
                )
            self.assertEqual(type_item.text(), "Matplotlib - Perceptual")
            preview_item = host.colorbar_colormap_table.item(
                host._colorbar_colormap_row("viridis"),
                1,
                )
            self.assertFalse(preview_item.icon().isNull())
            host.colorbar_colormap_table.sortItems(2, QtCore.Qt.AscendingOrder)
            self.assertGreater(host._colorbar_colormap_row("viridis"), -1)

            host.open_colorbar_scale_dialog()

            self.assertEqual(host.colorbar_scale_dialog.windowTitle(), "Color scale")
            self.assertIs(host.colorbar_scale_controls.parent(), host.colorbar_scale_dialog)

            host.colorbar_colormap_table.setCurrentCell(
                host._colorbar_colormap_row("Purples"),
                0,
                )
            self.assertEqual(
                host.config.updates,
                [("user_preference.bar_colour", "Purples")],
                )
            self.assertIsInstance(host.bar.color_map, pg.ColorMap)

            host.colorbar_include_local_check.setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_local", False),
                )
            self.assertEqual(host._colorbar_colormap_row("PAL-relaxed"), -1)

            host.colorbar_include_custom_check.setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_custom", False),
                )
            self.assertEqual(host._colorbar_colormap_row("Greys"), -1)

            host.colorbar_cet_subtype_checks["linear"].setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_cet_linear", False),
                )
            self.assertEqual(host._colorbar_colormap_row("CET-L1"), -1)
            self.assertGreater(host._colorbar_colormap_row("CET-C1"), -1)

            host.colorbar_include_cet_check.setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_cet", False),
                )
            self.assertEqual(host._colorbar_colormap_row("CET-C1"), -1)

            double_click_calls = []
            host.open_colorbar_scale_dialog = lambda: double_click_calls.append(True)
            axis = Axis()
            host._install_colorbar_scale_axis_handlers(axis)
            event = MouseEvent(QtCore.Qt.LeftButton)
            axis.mouseDoubleClickEvent(event)

            self.assertTrue(event.accepted)
            self.assertEqual(double_click_calls, [True])
            self.assertFalse(hasattr(axis, "contextMenuEvent"))

            previous_bar_clicks = []
            bar = Axis()
            bar.mouseClickEvent = lambda event: previous_bar_clicks.append(event.button())
            host._install_colorbar_scale_bar_handlers(bar)
            double_click_calls.clear()

            event = MouseEvent(QtCore.Qt.LeftButton)
            bar.mouseDoubleClickEvent(event)

            self.assertTrue(event.accepted)
            self.assertEqual(double_click_calls, [True])

            event = MouseEvent(QtCore.Qt.RightButton)
            bar.mouseClickEvent(event)

            self.assertTrue(event.accepted)
            self.assertEqual(previous_bar_clicks, [])
        finally:
            host.deleteLater()


class CloseAllPlotsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_close_all_can_be_cancelled_when_warning_enabled(self):
        old_question = qtw.QMessageBox.question
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == "user_preference.confirm_close_all":
                    return True
                raise KeyError(key)

        class FakeWindow:
            def close(self):
                closed.append(self)

        class Harness:
            closeAll = main_window.MainWindow.closeAll

            def __init__(self):
                self.config = FakeConfig()
                self.windows = [FakeWindow()]
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.No
            harness = Harness()
            harness.closeAll()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertEqual(closed, [])
        self.assertEqual(harness.status_messages[-1][0], "Close all plot windows cancelled.")

    def test_close_all_without_warning_closes_each_window(self):
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == "user_preference.confirm_close_all":
                    return False
                raise KeyError(key)

        class FakeWindow:
            def close(self):
                closed.append(self)

        class Harness:
            closeAll = main_window.MainWindow.closeAll

            def __init__(self):
                self.config = FakeConfig()
                self.windows = [FakeWindow(), FakeWindow()]
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        harness = Harness()
        harness.closeAll()

        self.assertEqual(closed, harness.windows)
        self.assertEqual(harness.status_messages[-1][0], "Closing plot windows...")

    def test_confirmation_options_use_shared_labels_and_config_keys(self):
        updates = []

        class FakeConfig:
            values = {
                "user_preference.confirm_close_all": True,
                "user_preference.confirm_close": False,
                }

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                updates.append((key, value))
                self.values[key] = value

        window = qtw.QMainWindow()
        window.config = FakeConfig()
        menu = qtw.QMenu(window)

        try:
            add_confirmation_options(window, menu)
            actions = [
                action for action in menu.actions()
                if not action.isSeparator()
                ]

            self.assertEqual(
                [action.text() for action in actions],
                [
                    "Confirm Before Closing All Plot Windows",
                    "Confirm Before Quit",
                    ]
                )
            self.assertTrue(actions[0].isChecked())
            self.assertFalse(actions[1].isChecked())

            actions[0].setChecked(False)
            actions[1].setChecked(True)

            self.assertEqual(updates, [
                ("user_preference.confirm_close_all", False),
                ("user_preference.confirm_close", True),
                ])
        finally:
            window.deleteLater()

    def test_restore_defaults_option_requests_main_window_reset(self):
        called = []
        window = qtw.QMainWindow()
        window.restore_default_settings = lambda: called.append(True)
        menu = qtw.QMenu(window)

        try:
            action = add_restore_defaults_option(window, menu)
            self.assertEqual(action.text(), "Restore Default Settings...")

            action.trigger()

            self.assertEqual(called, [True])
        finally:
            window.deleteLater()

    def test_restore_default_settings_can_be_cancelled(self):
        old_question = qtw.QMessageBox.question

        class FakeConfig:
            def __init__(self):
                self.reset_called = False

            def reset_to_defaults(self):
                self.reset_called = True

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self):
                self.config = FakeConfig()
                self.status_messages = []

            def apply_current_settings(self):
                raise AssertionError("Settings should not be applied after cancelling")

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.No
            harness = Harness()
            harness.restore_default_settings()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertFalse(harness.config.reset_called)
        self.assertEqual(harness.status_messages[-1][0], "Default settings restore cancelled.")

    def test_restore_default_settings_resets_and_applies_defaults(self):
        old_question = qtw.QMessageBox.question

        class FakeConfig:
            def __init__(self):
                self.reset_called = False

            def reset_to_defaults(self):
                self.reset_called = True

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self):
                self.config = FakeConfig()
                self.applied = False
                self.status_messages = []

            def apply_current_settings(self):
                self.applied = True

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.Yes
            harness = Harness()
            harness.restore_default_settings()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertTrue(harness.config.reset_called)
        self.assertTrue(harness.applied)
        self.assertEqual(harness.status_messages[-1][0], "Default settings restored.")


class RunListTooltipTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_run_tooltip_summarises_parameters(self):
        tooltip = treeWidgets.run_tooltip_text({
            "sweep_parameters": ["dac_ch1", "dac_ch2"],
            "measure_parameters": ["dmm_v1", "dmm_v2"],
            "run_timestamp": 100.0,
            "completed_timestamp": None,
            "is_completed": False,
            "result_count": 25,
            "expected_results": 100,
            })

        self.assertTrue(tooltip.startswith("<table"))
        self.assertEqual(tooltip.count("<tr>"), 2)
        self.assertIn("<td style='padding:0 0.5em 0 0'>Sweep</td>", tooltip)
        self.assertIn(
            "<td nowrap='nowrap' style='padding:0; white-space:nowrap'>"
            "(dac_ch1,&nbsp;dac_ch2)</td>",
            tooltip
            )
        self.assertIn("<td style='padding:0 0.5em 0 0'>Measure</td>", tooltip)
        self.assertIn(
            "<td nowrap='nowrap' style='padding:0; white-space:nowrap'>"
            "(dmm_v1,&nbsp;dmm_v2)</td>",
            tooltip
            )
        self.assertNotIn("Duration", tooltip)

    def test_format_point_count_summarises_multidimensional_sweeps(self):
        self.assertEqual(
            treeWidgets.format_point_count({
                "point_shape": [10, 100],
                "expected_results": 1000,
                }),
            "1,000 = 10 × 100"
            )

    def test_add_runs_only_watches_unfinished_rows(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "exp_name": "exp",
                    "sample_name": "sample",
                    "name": "unfinished",
                    "result_table_name": "results_1",
                    "guid": "unfinished-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["y"],
                    "result_count": 1,
                    "expected_results": 10,
                    "point_shape": [10],
                    "storage_bytes": 102400,
                    },
                2: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "exp_name": "exp",
                    "sample_name": "sample",
                    "name": "finished",
                    "result_table_name": "results_2",
                    "guid": "finished-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["z", "w"],
                    "result_count": 10,
                    "expected_results": 1000,
                    "point_shape": [10, 100],
                    "storage_bytes": 1536,
                    },
                })

            self.assertEqual(
                [run_list.headerItem().text(col) for col in range(run_list.columnCount())],
                ["ID", "Measurements", "Setpoints", "Started", "Complete", "Duration", "Size"]
                )
            self.assertIsInstance(
                run_list.itemDelegateForColumn(2),
                treeWidgets.EqualsAlignedDelegate
                )
            self.assertFalse(run_list.rootIsDecorated())
            self.assertEqual(run_list.indentation(), 0)
            self.assertEqual(
                run_list.horizontalScrollBarPolicy(),
                QtCore.Qt.ScrollBarAlwaysOff
                )
            self.assertTrue(
                all(
                    run_list.header().sectionResizeMode(col) == qtw.QHeaderView.Interactive
                    for col in range(run_list.columnCount())
                    )
                )
            items = {
                run_list.topLevelItem(row).guid: run_list.topLevelItem(row)
                for row in range(run_list.topLevelItemCount())
                }

            self.assertEqual([item.guid for item in run_list.watching], ["unfinished-guid"])
            self.assertEqual(items["unfinished-guid"].text(1), "")
            self.assertEqual(items["unfinished-guid"].data(1, QtCore.Qt.UserRole), 1)
            self.assertIsInstance(
                run_list.itemWidget(items["unfinished-guid"], 1),
                treeWidgets.RunPreviewCell
                )
            self.assertEqual(
                len(
                    run_list.itemWidget(
                        items["unfinished-guid"], 1
                        ).findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
                    ),
                1
                )
            self.assertEqual(items["unfinished-guid"].text(2), r"10 = 10")
            self.assertEqual(items["unfinished-guid"].text(4), "10.0%")
            self.assertRegex(items["unfinished-guid"].text(5), r"^\d+\.\d s$")
            self.assertEqual(items["unfinished-guid"].text(6), "100 KB")
            self.assertEqual(items["finished-guid"].text(1), "")
            self.assertEqual(items["finished-guid"].data(1, QtCore.Qt.UserRole), 2)
            self.assertEqual(
                len(
                    run_list.itemWidget(
                        items["finished-guid"], 1
                        ).findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
                    ),
                2
                )
            self.assertEqual(items["finished-guid"].text(2), "1,000 = 10 × 100")
            self.assertEqual(items["finished-guid"].text(4), "✓")
            self.assertEqual(items["finished-guid"].text(5), "10.0 s")
            self.assertEqual(
                int(items["finished-guid"].textAlignment(0)),
                int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(2)),
                int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(4)),
                int(QtCore.Qt.AlignCenter)
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(5)),
                int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(6)),
                int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                )
            self.assertIn("Measure</td>", items["unfinished-guid"].toolTip(0))
            self.assertIn("(y)</td>", items["unfinished-guid"].toolTip(0))
            self.assertNotIn("Complete", items["finished-guid"].toolTip(0))

            run_list.sortItems(1, QtCore.Qt.DescendingOrder)
            self.assertEqual(
                [run_list.topLevelItem(row).guid for row in range(run_list.topLevelItemCount())],
                ["finished-guid", "unfinished-guid"]
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_run_table_measurement_previews_use_preview_metadata(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            requested = []
            run_list.previewPlotRequested.connect(
                lambda guid, parameter: requested.append((guid, parameter))
                )
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "result_table_name": "results_1",
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal", "other"],
                    "result_count": 2,
                    "expected_results": 2,
                    "storage_bytes": 2048,
                    },
                })

            item = run_list.topLevelItem(0)
            cell = run_list.itemWidget(item, 1)
            self.assertEqual(
                len(cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")),
                2
                )

            run_list.set_run_previews("run-guid", [{
                "parameter": "signal",
                "axes": ["x"],
                "title": "signal vs x",
                "image": render_sparkline_preview(
                    np.array([0, 1], dtype=float),
                    np.array([1, 2], dtype=float),
                    size=40,
                    ),
                }])

            images = cell.findChildren(qtw.QLabel, "measurementPreviewImage")
            placeholders = cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
            self.assertEqual(len(images), 1)
            self.assertEqual(len(placeholders), 1)
            self.assertIsInstance(images[0], DraggablePreviewImageLabel)
            self.assertEqual(images[0].guid, "run-guid")
            self.assertEqual(images[0].parameter, "signal")
            self.assertEqual(images[0].axes, ["x"])
            self.assertEqual(images[0].toolTip(), "signal vs x")
            self.assertEqual(images[0].width(), treeWidgets.MEASUREMENT_PREVIEW_SIZE)
            self.assertEqual(images[0].height(), treeWidgets.MEASUREMENT_PREVIEW_SIZE)

            event = QtGui.QMouseEvent(
                QtCore.QEvent.MouseButtonDblClick,
                QtCore.QPointF(5, 5),
                QtCore.Qt.LeftButton,
                QtCore.Qt.LeftButton,
                QtCore.Qt.NoModifier,
                )
            qtw.QApplication.sendEvent(images[0], event)

            self.assertEqual(requested, [("run-guid", "signal")])
            self.assertIs(run_list.currentItem(), item)

            export_requested = []
            run_list.previewExportRequested.connect(
                lambda guid, parameter: export_requested.append((guid, parameter))
                )
            images[0].exportRequested.emit("signal")

            self.assertEqual(export_requested, [("run-guid", "signal")])
            self.assertIs(run_list.currentItem(), item)
        finally:
            treeWidgets.isfile = old_isfile


class RunPreviewDragDropTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_run_preview_drag_payload_round_trips_and_checks_axes(self):
        payload = run_preview_payload_from_mime(
            make_run_preview_mime("run-guid", "signal", ["x"])
            )

        self.assertEqual(payload, {
            "guid": "run-guid",
            "parameter": "signal",
            "axes": ["x"],
            })
        self.assertTrue(preview_drop_is_compatible(("x",), payload))
        self.assertFalse(preview_drop_is_compatible(("y",), payload))
        self.assertFalse(preview_drop_is_compatible(("x", "y"), payload))
        self.assertIsNone(run_preview_payload_from_mime(QtCore.QMimeData()))

    def test_add_trace_to_plot_uses_existing_add_path(self):
        class Param:
            def __init__(self, name, depends_on):
                self.name = name
                self.depends_on = depends_on
                self.depends_on_ = (depends_on,)

        class Dataset:
            def __init__(self, guid):
                self.guid = guid
                self.running = False

        class Combo:
            def __init__(self, items):
                self.items = items
                self.index = None

            def findText(self, text):
                try:
                    return self.items.index(text)
                except ValueError:
                    return -1

            def setCurrentIndex(self, index):
                self.index = index

        class Box:
            def __init__(self, items):
                self.option_box = Combo(items)

            def isEnabled(self):
                return True

        class Window:
            def __init__(self, guid, param, label):
                self.ds = Dataset(guid)
                self.param = param
                self.label = label
                self.visible = True
                self.closed = False

            def close(self):
                self.closed = True

        class Harness:
            _plot_window_for_param = main_window.MainWindow._plot_window_for_param
            add_trace_to_plot = main_window.MainWindow.add_trace_to_plot

            def __init__(self):
                self.status_messages = []
                self.errors = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

            def show_error(self, title, message, details=None):
                self.errors.append((title, message, details))

            def get_1d_wins(self, win):
                pass

        source_param = Param("signal", "x")
        target_param = Param("target", "x")
        source = Window("source-guid", source_param, "ID:1 signal")
        target = Window("target-guid", target_param, "ID:2 target")
        target.option_boxes = [Box([source.label])]

        harness = Harness()
        harness.windows = [target, source]

        added = harness.add_trace_to_plot(
            target,
            "source-guid",
            "signal",
            param=source_param
            )

        self.assertTrue(added)
        self.assertEqual(target.option_boxes[0].option_box.index, 0)
        self.assertTrue(source.closed)
        self.assertEqual(harness.status_messages, [])


class RunSizeTestCase(unittest.TestCase):
    def test_point_shape_uses_largest_measured_parameter_shape(self):
        self.assertEqual(
            readSQL._point_shape(
                {
                    "shapes": {
                        "dmm_v1": [10, 100],
                        "dmm_v2": [10],
                        }
                    },
                ["dmm_v1", "dmm_v2"]
                ),
            [10, 100]
            )

    def test_storage_size_falls_back_to_schema_estimate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "storage.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute("""
                  CREATE TABLE "results-1-4" (
                      id INTEGER,
                      timestamp REAL,
                      dac_ch1 REAL,
                      dac_ch2 REAL,
                      dmm_v1 REAL,
                      dmm_v2 REAL
                  )
                """)
                cursor.executemany(
                    'INSERT INTO "results-1-4" VALUES (?, ?, ?, ?, ?, ?)',
                    [(i, i * 0.01, 1.0, 2.0, 3.0, 4.0) for i in range(2000)]
                    )
                conn.commit()

                self.assertEqual(
                    readSQL._estimated_table_storage_bytes(cursor, "results-1-4"),
                    112000
                    )
            finally:
                cursor.close()
                conn.close()


class ThemeStylesheetTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_light_and_dark_stylesheets_parse_without_qt_warnings(self):
        messages = []

        def handler(_mode, _context, message):
            messages.append(message)

        previous = QtCore.qInstallMessageHandler(handler)
        try:
            for theme in (light, dark):
                window = qtw.QMainWindow()
                window.setStyleSheet(theme.main)
                window.deleteLater()
        finally:
            QtCore.qInstallMessageHandler(previous)

        parse_warnings = [
            message for message in messages
            if "Could not parse stylesheet" in message
            ]
        self.assertEqual(parse_warnings, [])


class OperationsPanelTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_operations_panel_layout_is_installed_once(self):
        messages = []

        def handler(_mode, _context, message):
            messages.append(message)

        main = qtw.QMainWindow()
        main.oper_dock = QDock_context("Operations", main)

        previous = QtCore.qInstallMessageHandler(handler)
        try:
            widget = operations_options_1d(main)
            main.oper_dock.addWidget(widget)
        finally:
            QtCore.qInstallMessageHandler(previous)
            main.deleteLater()

        layout_warnings = [
            message for message in messages
            if "Attempting to add QLayout" in message
            ]
        self.assertEqual(layout_warnings, [])


class DatabaseDropTestCase(unittest.TestCase):
    def test_database_path_from_mime_data_accepts_one_local_db_file(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            mime_data = QtCore.QMimeData()
            mime_data.setUrls([QtCore.QUrl.fromLocalFile(database.name)])

            self.assertEqual(
                main_window.database_path_from_mime_data(mime_data),
                database.name
                )

    def test_database_path_from_mime_data_rejects_ambiguous_or_non_db_drops(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".db") as database,
            tempfile.NamedTemporaryFile(suffix=".txt") as text_file,
        ):
            text_drop = QtCore.QMimeData()
            text_drop.setUrls([QtCore.QUrl.fromLocalFile(text_file.name)])

            multiple_drop = QtCore.QMimeData()
            multiple_drop.setUrls([
                QtCore.QUrl.fromLocalFile(database.name),
                QtCore.QUrl.fromLocalFile(text_file.name),
                ])

            self.assertIsNone(main_window.database_path_from_mime_data(text_drop))
            self.assertIsNone(main_window.database_path_from_mime_data(multiple_drop))


class RunDetailsTabsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_run_details_show_overview_parameters_metadata_and_raw_tabs(self):
        class Param:
            def __init__(self, name, label, unit, axes=()):
                self.name = name
                self.label = label
                self.unit = unit
                self.depends_on_ = axes

        class Dataset:
            run_id = 2
            name = "results"
            exp_name = "Demo_experiment"
            sample_name = "no sample"
            running = False
            guid = "abc-123"
            run_timestamp_raw = 1768129603
            completed_timestamp_raw = 1768129626.1

            def get_parameters(self):
                return [
                    Param("dac_ch1", "Gate ch1", "V"),
                    Param("dmm_v1", "Gate v1", "V", ("dac_ch1",)),
                    ]

            def run_timestamp(self):
                return 1768129603

            def completed_timestamp(self):
                return 1768129626.1

            def get_parameter_data(self, name):
                return {
                    "dmm_v1": {
                        "dac_ch1": np.array([-1.0, -0.5, 0.0, 0.5, 1.0]),
                        "dmm_v1": np.array([0.1, 0.2, 0.3, 0.4, 0.5]),
                        }
                    }

        widget = treeWidgets.moreInfo()
        widget.setInfo(
            {
                "Data Structure": {
                    "Data points": 200,
                    "dac_ch1": {"unit": "V", "label": "Gate ch1"},
                    "dmm_v1": {"unit": "V", "label": "Gate v1", "axes": ["dac_ch1"]},
                    },
                "MetaData": {"export_info": "x" * 300},
                "Snapshot": {
                    "station": {
                        "parameters": {
                            "ch1": {
                                "full_name": "dac_ch1",
                                "value": 25.0,
                                "instrument_name": "dac",
                                "vals": "<Numbers -800<=v<=400>",
                                },
                            "v1": {
                                "full_name": "dmm_v1",
                                "value": -0.0048,
                                "instrument_name": "dmm",
                                "vals": "<Numbers -800<=v<=400>",
                                },
                            }
                        }
                    },
                },
            Dataset()
            )

        self.assertEqual([widget.tabText(i) for i in range(widget.count())],
                         ["Overview", "Sweep parameters", "Preview", "Metadata", "Raw key-value"])
        self.assertEqual(
            [
                widget.overview.item(row, 0).text()
                for row in range(widget.overview.rowCount())
                ],
            [
                "Status",
                "Data points",
                "Duration",
                "Measured parameters",
                "Setpoints",
                "Started",
                "Completed",
                "Experiment",
                "Sample",
                "Name",
                "GUID",
                ]
            )
        self.assertEqual(
            [
                widget.parameters.horizontalHeaderItem(col).text()
                for col in range(widget.parameters.columnCount())
                ],
            ["Name", "Label", "Unit", "From", "To", "Steps", "Delay", "Instrument"]
            )
        self.assertEqual(widget.parameters.rowCount(), 4)
        self.assertEqual(widget.parameters.item(0, 0).text(), "Set parameters")
        self.assertTrue(widget.parameters.item(0, 0).font().bold())
        self.assertEqual(widget.parameters.item(1, 0).text(), "dac_ch1")
        self.assertEqual(widget.parameters.item(1, 3).text(), "-1")
        self.assertEqual(widget.parameters.item(1, 4).text(), "1")
        self.assertEqual(widget.parameters.item(1, 5).text(), "5")
        self.assertEqual(widget.parameters.item(1, 6).text(), "")
        self.assertEqual(widget.parameters.item(1, 7).text(), "dac")
        self.assertFalse(widget.parameters.item(1, 0).font().bold())
        self.assertFalse(widget.parameters.item(1, 0).font().italic())
        self.assertEqual(widget.parameters.item(2, 0).text(), "Measure parameters")
        self.assertTrue(widget.parameters.item(2, 0).font().bold())
        self.assertEqual(widget.parameters.item(3, 0).text(), "dmm_v1")
        self.assertEqual(widget.parameters.item(3, 3).text(), "")
        self.assertEqual(widget.parameters.item(3, 4).text(), "")
        self.assertEqual(widget.parameters.item(3, 5).text(), "")
        self.assertEqual(widget.parameters.item(3, 6).text(), "")
        self.assertEqual(widget.parameters.item(3, 7).text(), "dmm")
        self.assertFalse(widget.parameters.item(3, 0).font().bold())
        self.assertFalse(widget.parameters.item(3, 0).font().italic())
        self.assertEqual(
            widget.overview.item(2, 1).text(),
            "23.10 s\t(0d 0h 0m 23s; 0.115 s/point)"
            )
        self.assertLessEqual(len(widget.metadata.topLevelItem(0).text(1)), 180)
        self.assertTrue(widget.metadata.wordWrap())
        self.assertTrue(widget.raw.wordWrap())
        self.assertEqual(widget.metadata.textElideMode(), QtCore.Qt.ElideNone)
        self.assertEqual(widget.raw.textElideMode(), QtCore.Qt.ElideNone)
        self.assertEqual(
            widget.metadata.horizontalScrollBarPolicy(),
            QtCore.Qt.ScrollBarAlwaysOff
            )
        self.assertIsInstance(
            widget.raw.itemDelegateForColumn(1),
            treeWidgets.WrappedValueDelegate
            )
        self.assertEqual(
            widget.metadata.header().sectionResizeMode(1),
            qtw.QHeaderView.Stretch
            )
        self.assertEqual(
            widget.raw.header().sectionResizeMode(1),
            qtw.QHeaderView.Stretch
            )
        self.assertEqual(
            widget.parameters.horizontalHeader().sectionResizeMode(7),
            qtw.QHeaderView.Stretch
            )
        widget.parameters.selectRow(3)
        widget.parameters.copySelection()
        self.assertEqual(
            qtw.QApplication.clipboard().text(),
            "dmm_v1\tGate v1\tV\t\t\t\t\tdmm"
            )
        self.assertEqual(
            widget.parameters.copy_selection_action.shortcuts()[0].toString(),
            "Ctrl+C"
            )
        self.assertEqual(
            widget.parameters.copy_cell_action.shortcuts()[0].toString(),
            "Ctrl+Shift+C"
            )
        widget.parameters.setCurrentCell(3, 0)
        widget.parameters.copyCell()
        self.assertEqual(qtw.QApplication.clipboard().text(), "dmm_v1")

        widget.metadata.setCurrentItem(widget.metadata.topLevelItem(0))
        widget.metadata.copySelection()
        self.assertTrue(qtw.QApplication.clipboard().text().startswith("export_info\t"))
        widget.metadata.copyValue()
        self.assertTrue(qtw.QApplication.clipboard().text().startswith("xxx"))

        widget.setInfo({"Data Structure": {"Data points": 0}}, Dataset())
        self.assertEqual(
            widget.parameters.horizontalHeader().sectionResizeMode(7),
            qtw.QHeaderView.Stretch
            )

    def test_preview_renderers_make_square_images(self):
        sparkline = render_sparkline_preview(
            np.array([0, 1, 2, 3], dtype=float),
            np.array([1, 4, 2, 3], dtype=float),
            )
        heatmap = render_heatmap_preview(
            np.array([0, 1, 0, 1], dtype=float),
            np.array([0, 0, 1, 1], dtype=float),
            np.array([1, 2, 3, 4], dtype=float),
            )

        self.assertEqual(sparkline.width(), PREVIEW_SIZE)
        self.assertEqual(sparkline.height(), PREVIEW_SIZE)
        self.assertEqual(heatmap.width(), PREVIEW_SIZE)
        self.assertEqual(heatmap.height(), PREVIEW_SIZE)

    def test_sparkline_preview_uses_subtle_non_white_background(self):
        sparkline = render_sparkline_preview(
            np.array([], dtype=float),
            np.array([], dtype=float),
            size=20,
            )

        background = QtGui.QColor(sparkline.pixel(0, 0))
        self.assertEqual(background, QtGui.QColor(PREVIEW_BACKGROUND_COLOR))
        self.assertNotEqual(background, QtGui.QColor("white"))

    def test_heatmap_preview_keeps_x_horizontal_and_y_vertical(self):
        heatmap = render_heatmap_preview(
            np.array([0, 1, 0, 1], dtype=float),
            np.array([0, 0, 1, 1], dtype=float),
            np.array([0, 255, 0, 0], dtype=float),
            size=20,
            )

        high = QtGui.QColor(heatmap.pixel(15, 15))
        above = QtGui.QColor(heatmap.pixel(15, 5))
        left = QtGui.QColor(heatmap.pixel(5, 15))

        self.assertGreater(high.green(), 200)
        self.assertGreater(high.red(), 200)
        self.assertLess(high.blue(), 80)
        self.assertLess(above.green(), 80)
        self.assertLess(left.green(), 80)

    def test_generate_2d_preview_matches_full_plot_axis_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "preview.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE results (slow_y REAL, fast_x REAL, signal REAL)")
                cursor.executemany(
                    "INSERT INTO results VALUES (?, ?, ?)",
                    [
                        (0.0, 0.0, 0.0),
                        (0.0, 1.0, 255.0),
                        (1.0, 0.0, 0.0),
                        (1.0, 1.0, 0.0),
                        ]
                    )
                conn.commit()
            finally:
                cursor.close()
                conn.close()

            previews = generate_run_previews(database_path, {
                "run_id": 8,
                "result_table_name": "results",
                "result_count": 4,
                "measure_parameters": ["signal"],
                "sweep_parameters": ["slow_y", "fast_x"],
                "run_description": """
                {
                  "interdependencies_": {
                    "dependencies": {
                      "signal": ["slow_y", "fast_x"]
                    }
                  }
                }
                """,
                }, size=20)

        heatmap = previews[0]["image"]
        high = QtGui.QColor(heatmap.pixel(15, 15))
        left = QtGui.QColor(heatmap.pixel(5, 15))
        self.assertGreater(high.green(), 200)
        self.assertLess(left.green(), 80)

    def test_preview_tab_arranges_images_horizontally_with_tooltips(self):
        preview = PreviewTab(preview_size=150)
        self.assertLessEqual(preview.minimumHeight(), 50)
        preview._show_previews([
            {
                "parameter": "signal",
                "title": "signal vs x",
                "image": render_sparkline_preview(
                    np.array([0, 1], dtype=float),
                    np.array([1, 2], dtype=float),
                    size=150,
                    ),
                },
            {
                "parameter": "image",
                "title": "image vs x and y",
                "image": render_heatmap_preview(
                    np.array([0, 1, 0, 1], dtype=float),
                    np.array([0, 0, 1, 1], dtype=float),
                    np.array([1, 2, 3, 4], dtype=float),
                    size=150,
                    ),
                },
            ])

        self.assertIsInstance(preview.content_layout, qtw.QHBoxLayout)
        cards = [
            preview.content_layout.itemAt(index).widget()
            for index in range(preview.content_layout.count())
            if preview.content_layout.itemAt(index).widget() is not None
            ]
        self.assertEqual(len(cards), 2)
        for card, title in zip(cards, ["signal vs x", "image vs x and y"]):
            labels = card.findChildren(qtw.QLabel)
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0].toolTip(), title)
            self.assertEqual(labels[0].width(), 150)
            self.assertEqual(labels[0].height(), 150)
            self.assertIsInstance(labels[0], DraggablePreviewImageLabel)

    def test_preview_tab_images_carry_drag_metadata_for_current_run(self):
        preview = PreviewTab(preview_size=100)
        preview.current_guid = "run-guid"
        preview._show_previews([
            {
                "parameter": "signal",
                "axes": ["x"],
                "title": "signal vs x",
                "image": render_sparkline_preview(
                    np.array([0, 1], dtype=float),
                    np.array([1, 2], dtype=float),
                    size=100,
                    ),
                },
            ])

        image = preview.findChild(qtw.QLabel, "previewImage")

        self.assertIsInstance(image, DraggablePreviewImageLabel)
        self.assertEqual(image.guid, "run-guid")
        self.assertEqual(image.parameter, "signal")
        self.assertEqual(image.axes, ["x"])

    def test_double_clicking_preview_requests_matching_parameter_plot(self):
        preview = PreviewTab(preview_size=100)
        requested = []
        preview.plotRequested.connect(requested.append)
        preview._show_previews([
            {
                "parameter": "dmm_v2",
                "title": "dmm_v2 vs dac_ch1 and dac_ch2",
                "image": render_heatmap_preview(
                    np.array([0, 1, 0, 1], dtype=float),
                    np.array([0, 0, 1, 1], dtype=float),
                    np.array([1, 2, 3, 4], dtype=float),
                    size=100,
                    ),
                },
            ])

        image = preview.findChild(qtw.QLabel, "previewImage")
        event = QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonDblClick,
            QtCore.QPointF(10, 10),
            QtCore.Qt.LeftButton,
            QtCore.Qt.LeftButton,
            QtCore.Qt.NoModifier,
            )
        qtw.QApplication.sendEvent(image, event)

        self.assertEqual(requested, ["dmm_v2"])

    def test_right_clicking_preview_can_request_export(self):
        old_exec = qtw.QMenu.exec_
        captured_actions = []
        preview = PreviewTab(preview_size=100)
        requested = []
        preview.exportRequested.connect(requested.append)

        def capture_menu(menu, *_args, **_kwargs):
            captured_actions.extend(menu.actions())

        try:
            qtw.QMenu.exec_ = capture_menu
            preview._show_previews([
                {
                    "parameter": "signal",
                    "title": "signal vs x",
                    "image": render_sparkline_preview(
                        np.array([0, 1], dtype=float),
                        np.array([1, 2], dtype=float),
                        size=100,
                        ),
                    },
                ])

            image = preview.findChild(qtw.QLabel, "previewImage")
            event = QtGui.QContextMenuEvent(
                QtGui.QContextMenuEvent.Mouse,
                QtCore.QPoint(10, 10),
                QtCore.QPoint(10, 10),
                )
            qtw.QApplication.sendEvent(image, event)

            export_action = next(
                action for action in captured_actions
                if action.text().replace("&", "") == "Export CSV..."
                )
            export_action.trigger()

            self.assertEqual(requested, ["signal"])
        finally:
            qtw.QMenu.exec_ = old_exec

    def test_clicking_preview_marks_it_selected(self):
        preview = PreviewTab(preview_size=80)
        preview._show_previews([
            {
                "parameter": "signal",
                "title": "signal vs x",
                "image": render_sparkline_preview(
                    np.array([0, 1], dtype=float),
                    np.array([1, 2], dtype=float),
                    size=80,
                    ),
                },
            {
                "parameter": "image",
                "title": "image vs x and y",
                "image": render_heatmap_preview(
                    np.array([0, 1, 0, 1], dtype=float),
                    np.array([0, 0, 1, 1], dtype=float),
                    np.array([1, 2, 3, 4], dtype=float),
                    size=80,
                    ),
                },
            ])

        images = preview.findChildren(qtw.QLabel, "previewImage")
        first_press = QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonPress,
            QtCore.QPointF(10, 10),
            QtCore.Qt.LeftButton,
            QtCore.Qt.LeftButton,
            QtCore.Qt.NoModifier,
            )
        second_press = QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonPress,
            QtCore.QPointF(10, 10),
            QtCore.Qt.LeftButton,
            QtCore.Qt.LeftButton,
            QtCore.Qt.NoModifier,
            )

        qtw.QApplication.sendEvent(images[0], first_press)
        self.assertTrue(images[0].property(PREVIEW_SELECTED_PROPERTY))
        self.assertFalse(images[1].property(PREVIEW_SELECTED_PROPERTY))

        qtw.QApplication.sendEvent(images[1], second_press)
        self.assertFalse(images[0].property(PREVIEW_SELECTED_PROPERTY))
        self.assertTrue(images[1].property(PREVIEW_SELECTED_PROPERTY))

    def test_generate_run_previews_reads_1d_and_2d_sql_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "previews.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute("""
                  CREATE TABLE results (
                      x REAL,
                      y REAL,
                      signal_1d REAL,
                      signal_2d REAL
                  )
                """)
                cursor.executemany(
                    "INSERT INTO results VALUES (?, ?, ?, ?)",
                    [
                        (0.0, 0.0, 1.0, 1.0),
                        (1.0, 0.0, 2.0, 2.0),
                        (0.0, 1.0, 3.0, 3.0),
                        (1.0, 1.0, 4.0, 4.0),
                        ]
                    )
                conn.commit()
            finally:
                cursor.close()
                conn.close()

            previews = generate_run_previews(database_path, {
                "run_id": 7,
                "result_table_name": "results",
                "result_count": 4,
                "measure_parameters": ["signal_1d", "signal_2d"],
                "sweep_parameters": ["x", "y"],
                "run_description": """
                {
                  "interdependencies_": {
                    "dependencies": {
                      "signal_1d": ["x"],
                      "signal_2d": ["x", "y"]
                    }
                  }
                }
                """,
                })

        self.assertEqual([preview["title"] for preview in previews], [
            "signal_1d vs x",
            "signal_2d vs x and y",
            ])
        self.assertEqual([preview["parameter"] for preview in previews], [
            "signal_1d",
            "signal_2d",
            ])
        self.assertEqual([preview["axes"] for preview in previews], [
            ["x"],
            ["x", "y"],
            ])
        self.assertTrue(all(preview["image"].width() == PREVIEW_SIZE for preview in previews))


if __name__ == "__main__":
    unittest.main()
