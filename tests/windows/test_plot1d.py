import math
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.windows import _plotWin as plotwin_module
from qplot.windows._dataset_handle import DatasetKey, TraceKey
from qplot.windows._plot1d_snap import _nearest_trace_sample
from qplot.windows._plot1d_traces import (
    TRACE_COLOR_PALETTE,
    Plot1DTraceMixin,
    _TraceAppearanceDialog,
)
from qplot.windows._plot_axis_scaling import PlotAxisScalingMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows._subplots import custom_viewbox
from qplot.windows._subplots.subplot1d import _subplot_axis_order, subplot1d
from qplot.windows._widgets import QDock_context, picker_1d
from qplot.windows.plot1d import plot1d


class SnapToTraceTestCase(unittest.TestCase):
    def test_major_tick_count_preference_applies_to_plot_and_colorbar_axes(self):
        class Axis:
            def __init__(self):
                self.picture = object()
                self.logMode = False
                self.tickSpacing = lambda *args: []
                self.tickStrings = lambda values, scale, spacing: list(values)
                self.styles = []
                self.updates = 0

            def setStyle(self, **style):
                self.styles.append(style)

            def update(self):
                self.updates += 1

        class AxisOwner:
            def __init__(self):
                self.axes = {side: Axis() for side in ("left", "bottom", "right", "top")}
                self.updates = 0

            def getAxis(self, side):
                return self.axes[side]

            def update(self):
                self.updates += 1

        class Viewport:
            def __init__(self):
                self.updates = 0

            def update(self):
                self.updates += 1

        class Widget:
            def __init__(self):
                self.view = Viewport()

            def viewport(self):
                return self.view

        class Config:
            def get(self, key):
                self.key = key
                return 3

        window = plotWidget.__new__(plotWidget)
        window.plot = AxisOwner()
        window.bar = AxisOwner()
        window.widget = Widget()
        window.config = Config()

        window.apply_axis_major_tick_count_preference()

        self.assertEqual(window.config.key, "user_preference.axis_major_tick_count")
        for owner in (window.plot, window.bar):
            for axis in owner.axes.values():
                self.assertEqual(
                    axis.styles,
                    [{"maxTickLevel": 1, "maxTextLevel": 0}],
                    )
                self.assertIsNone(axis.picture)
                self.assertEqual(axis.updates, 1)
                self.assertEqual(
                    axis.tickSpacing(-1, 1, 100),
                    [(1.0, 0.0), (0.2, 0.0)],
                    )
                self.assertEqual(
                    axis.tickStrings([-1.0, -2e-14, 1.0], 1.0, 1.0),
                    ["-1", "0", "1"],
                    )
        self.assertEqual(window.plot.updates, 1)
        self.assertEqual(window.widget.view.updates, 1)

    def test_major_tick_spacing_uses_nice_values_and_keeps_minor_level(self):
        axis = type("Axis", (), {"_qplot_major_tick_count": 3})()

        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, -201, 201, 600),
            [(200.0, 0.0), (50.0, 0.0)],
            )
        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, -9.33, 9.33, 600),
            [(5.0, 0.0), (1.0, 0.0)],
            )

        axis._qplot_major_tick_count = 2
        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, -201, 201, 600),
            [(200.0, 0.0), (50.0, 0.0)],
            )

        axis._qplot_major_tick_count = 5
        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, -9.33, 9.33, 600),
            [(5.0, 0.0), (1.0, 0.0)],
            )

        axis._qplot_major_tick_count = 4
        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, -1.5, 1.5, 600),
            [(1.0, 0.0), (0.2, 0.0)],
            )

        axis._qplot_major_tick_count = 7
        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, -9.33, 9.33, 600),
            [(2.0, 0.0), (0.5, 0.0)],
            )

    def test_major_tick_spacing_tolerates_roundoff_at_nice_endpoints(self):
        lower = -9.999999999999998
        upper = 10.000000000000002
        axis = type("Axis", (), {"_qplot_major_tick_count": 3})()

        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, lower, upper, 600),
            [(10.0, 0.0), (2.0, 0.0)],
            )
        self.assertEqual(
            plotwin_module._tick_positions(lower, upper, 10.0, 0.0),
            [-10.0, 0.0, 10.0],
            )
        self.assertEqual(
            plotwin_module._tick_positions(-9.999999, 9.999999, 10.0, 0.0),
            [0.0],
            )

        axis._qplot_major_tick_count = 4
        self.assertEqual(
            plotwin_module._major_tick_spacing(axis, lower, upper, 600),
            [(5.0, 0.0), (1.0, 0.0)],
            )

    def test_horizontal_axis_target_counts_only_labels_that_fit(self):
        widget = pg.PlotWidget()
        widget.resize(600, 400)
        widget.show()
        plot = widget.getPlotItem()
        plot.setXRange(-10.0, 10.0, padding=0)
        axis = plot.getAxis("bottom")
        image = QtGui.QImage(
            800,
            600,
            QtGui.QImage.Format.Format_ARGB32,
        )
        painter = QtGui.QPainter(image)

        def dispose_plot_widget():
            # Closing alone leaves the native PlotWidget alive until Python's
            # later garbage collection.  On the Windows offscreen backend that
            # can race QPainter teardown and abort the test worker.
            painter.end()
            widget.close()
            widget.deleteLater()
            qtw.QApplication.sendPostedEvents(
                None,
                QtCore.QEvent.Type.DeferredDelete,
            )
            qtw.QApplication.processEvents()

        self.addCleanup(dispose_plot_widget)

        for target, expected in (
                (3, ["-5", "0", "5"]),
                (4, ["-5", "0", "5"]),
                ):
            plotwin_module._configure_major_ticks(axis, target)
            qtw.QApplication.processEvents()
            specs = axis.generateDrawSpecs(painter)
            self.assertEqual([spec[2] for spec in specs[2]], expected)

    def test_tick_positions_are_bounded_and_precision_aware(self):
        self.assertEqual(
            plotwin_module._tick_positions(0.0, 1.0, 0.5, 0.0),
            [0.0, 0.5, 1.0],
            )
        self.assertEqual(
            plotwin_module._tick_positions(1.0, -1.0, 1.0, 0.0),
            [-1.0, 0.0, 1.0],
            )
        self.assertEqual(
            plotwin_module._tick_positions(-9.0, -1.0, 2.0, 0.0),
            [-8.0, -6.0, -4.0, -2.0],
            )

        for lower, upper, spacing in (
                (1e12, 1e12 + 1.0, 0.5),
                (1e15, 1e15 + 1.0, 0.5),
                (1e-9, 1e-9 + 1e-12, 5e-13),
                ):
            with self.subTest(lower=lower, upper=upper):
                positions = plotwin_module._tick_positions(
                    lower,
                    upper,
                    spacing,
                    0.0,
                    )
                self.assertGreaterEqual(len(positions), 2)
                self.assertLessEqual(
                    len(positions),
                    plotwin_module._MAX_TICK_POSITIONS,
                    )

        # This lattice would previously allocate around two billion entries
        # because the endpoint tolerance was based on 1e15 rather than the span.
        with patch("builtins.range", side_effect=AssertionError("range allocated")):
            self.assertEqual(
                plotwin_module._tick_positions(
                    1e15,
                    1e15 + 1.0,
                    1e-6,
                    0.0,
                    ),
                [],
                )

    def test_major_tick_spacing_handles_edge_ranges_without_invalid_levels(self):
        axis = type("Axis", (), {"_qplot_major_tick_count": 3})()
        smallest_positive = math.nextafter(0.0, 1.0)
        ranges = (
            (0.0, 1.0),
            (-10.0, -1.0),
            (-1.0, 1.0),
            (1e12, 1e12 + 1.0),
            (1e15, 1e15 + 1.0),
            (1e-9, 1e-9 + 1e-12),
            (0.0, smallest_positive),
            (0.0, 1e-300),
            (0.0, 1e300),
            (-1e308, 1e308),
            )

        for lower, upper in ranges:
            with self.subTest(lower=lower, upper=upper):
                levels = plotwin_module._major_tick_spacing(
                    axis,
                    lower,
                    upper,
                    600,
                    )
                self.assertGreaterEqual(len(levels), 1)
                self.assertLessEqual(len(levels), 2)
                for spacing, offset in levels:
                    self.assertTrue(math.isfinite(spacing))
                    self.assertGreater(spacing, 0.0)
                    self.assertTrue(math.isfinite(offset))
                positions = plotwin_module._tick_positions(
                    lower,
                    upper,
                    *levels[0],
                    )
                self.assertLessEqual(
                    len(positions),
                    plotwin_module._MAX_TICK_POSITIONS,
                    )

        for lower, upper in (
                (1.0, 1.0),
                (math.nan, 1.0),
                (-math.inf, 1.0),
                (0.0, math.inf),
                ):
            with self.subTest(lower=lower, upper=upper):
                self.assertEqual(
                    plotwin_module._major_tick_spacing(
                        axis,
                        lower,
                        upper,
                        600,
                        ),
                    [],
                    )

    def test_major_tick_spacing_has_safe_fallback_when_no_labels_fit(self):
        axis = type("Axis", (), {"_qplot_major_tick_count": 3})()

        with patch.object(
                plotwin_module,
                "_visible_horizontal_tick_positions",
                return_value=[],
                ):
            levels = plotwin_module._major_tick_spacing(axis, 0.0, 1.0, 1)

        self.assertEqual(levels, [(0.5, 0.0), (0.1, 0.0)])

    def test_real_axes_handle_narrow_vertical_reversed_and_log_ranges(self):
        for size in (1, 5, 10, 20):
            axis = pg.AxisItem("bottom")
            plotwin_module._configure_major_ticks(axis, 3)
            with self.subTest(size=size):
                levels = axis.tickSpacing(0.0, 1.0, size)
                self.assertGreater(levels[0][0], 0.0)

        vertical_axis = pg.AxisItem("left")
        plotwin_module._configure_major_ticks(vertical_axis, 3)
        self.assertEqual(
            vertical_axis.tickSpacing(1.0, -1.0, 1),
            [(1.0, 0.0), (0.2, 0.0)],
            )

        log_axis = pg.AxisItem("bottom")
        log_axis.setLogMode(True)
        plotwin_module._configure_major_ticks(log_axis, 3)
        levels = log_axis.tickSpacing(3.0, -3.0, 10)
        self.assertGreater(levels[0][0], 0.0)
        values = [-2.0, 0.0, 2.0]
        self.assertEqual(
            log_axis.tickStrings(values, 1.0, levels[0][0]),
            log_axis._qplot_original_tick_strings(
                values,
                1.0,
                levels[0][0],
                ),
            )

    def test_plot_widget_narrow_resize_and_grab_does_not_reach_qt_exception_hook(self):
        widget = pg.PlotWidget()
        widget.show()
        plot = widget.getPlotItem()
        for side in ("bottom", "left"):
            plotwin_module._configure_major_ticks(plot.getAxis(side), 3)

        def dispose_plot_widget():
            widget.close()
            widget.deleteLater()
            qtw.QApplication.sendPostedEvents(
                None,
                QtCore.QEvent.Type.DeferredDelete,
                )
            qtw.QApplication.processEvents()

        self.addCleanup(dispose_plot_widget)
        qt_errors = []

        def record_qt_error(exception_type, exception, traceback):
            qt_errors.append((exception_type, exception, traceback))

        with patch.object(sys, "excepthook", side_effect=record_qt_error):
            for lower, upper in (
                    (0.0, 1.0),
                    (1e15, 1e15 + 1.0),
                    ):
                plot.setXRange(lower, upper, padding=0.0)
                plot.setYRange(-1.0, 1.0, padding=0.0)
                for width in (1, 5, 10, 20):
                    with self.subTest(lower=lower, width=width):
                        widget.resize(width, 120)
                        qtw.QApplication.processEvents()
                        self.assertFalse(widget.grab().isNull())

            plot.setLogMode(x=True)
            plot.setXRange(-3.0, 3.0, padding=0.0)
            qtw.QApplication.processEvents()
            self.assertFalse(widget.grab().isNull())

        self.assertEqual(qt_errors, [])

    def test_trace_scroll_area_does_not_lock_dock_width(self):
        host = Plot1DTraceMixin.__new__(Plot1DTraceMixin)
        host.lineScroll = qtw.QScrollArea()
        host.lineScroll.setMinimumWidth(500)
        host.scrollWidget = qtw.QWidget()
        host.scrollWidget.setMinimumWidth(600)
        host.lineScroll.setWidget(host.scrollWidget)

        host._resize_scrollArea()

        self.assertEqual(host.lineScroll.minimumWidth(), 1)

    def test_nearest_trace_sample_returns_none_without_finite_points(self):
        self.assertIsNone(_nearest_trace_sample([], [], 0.0))
        self.assertIsNone(
            _nearest_trace_sample([0.0, np.nan, np.inf], [np.nan, 1.0, 2.0], 0.0)
            )

    def test_empty_window_update_clears_stale_add_to_plot_choices(self):
        class Combo:
            def isEnabled(self):
                return True

            def currentData(self):
                return "closed plot"

            def currentIndex(self):
                return 0

            def currentText(self):
                return "closed plot"

        class Box:
            def __init__(self):
                self.option_box = Combo()
                self.resets = []

            def reset_box(self, items, item_data=None):
                self.resets.append(items)

        host = Plot1DTraceMixin.__new__(Plot1DTraceMixin)
        box = Box()
        host.mergable = [object()]
        host.option_boxes = [box]

        host.update_line_picker([])

        self.assertEqual(host.mergable, [])
        self.assertEqual(box.resets, [[]])

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

    def test_nearest_trace_sample_uses_y_to_disambiguate_duplicate_x_values(self):
        sample = _nearest_trace_sample(
            [0.0, 1.0, 1.0, 2.0],
            [0.0, 10.0, 20.0, 30.0],
            1.0,
            cursor_y=19.0,
            )

        self.assertEqual(sample.x_value, 1.0)
        self.assertEqual(sample.y_value, 20.0)
        self.assertEqual(sample.point_number, 3)

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

    def test_secondary_axes_use_power_scaled_units(self):
        for orientation in ("top", "right"):
            axis = plotwin_module._PowerScaledAxisItem(orientation)
            axis.setLabel(text="Current", units="A")
            axis.setRange(1e-9, 9e-9)

            self.assertIn(
                "Current (10<sup>-9</sup> A)",
                axis.labelString(),
            )

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

    def test_nearest_trace_point_uses_screen_distance_with_duplicate_x_values(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[1.0, 1.0], y=[10.0, 20.0])
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.right_vb = None
        window.lines = {"main": line}

        scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(1.0, 19.0))

        _label, _x_value, y_value, _viewbox, point_number = (
            window._nearest_trace_point(scene_pos)
            )

        self.assertEqual(y_value, 20.0)
        self.assertEqual(point_number, 2)

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

    def test_real_plot_data_item_snap_reports_raw_values_and_marks_view_coordinates(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(
            x=np.array([1.0, 10.0, 100.0]),
            y=np.array([1.0, 10.0, 100.0]),
        )
        line.setLogMode(True, True)
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.right_vb = None
        window.lines = {"main": line}
        window.snap_marker = None
        window._snap_marker_view = None

        try:
            raw_x, raw_y = line.getOriginalDataset()
            view_x, view_y = line.getData()
            np.testing.assert_array_equal(raw_x, [1.0, 10.0, 100.0])
            np.testing.assert_array_equal(raw_y, [1.0, 10.0, 100.0])
            np.testing.assert_allclose(view_x, [0.0, 1.0, 2.0])
            np.testing.assert_allclose(view_y, [0.0, 1.0, 2.0])

            scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(1.05, 0.95))
            nearest = window._nearest_trace_point(scene_pos)

            label, x_value, y_value, viewbox, point_number = nearest
            self.assertEqual(label, "main")
            self.assertEqual((x_value, y_value), (10.0, 10.0))
            self.assertEqual(point_number, 2)
            self.assertIs(viewbox, plot_item.vb)
            self.assertEqual(
                (nearest.x_view_value, nearest.y_view_value),
                (1.0, 1.0),
            )

            window._show_snap_marker(
                nearest.x_view_value,
                nearest.y_view_value,
                nearest.viewbox,
            )
            marker_x, marker_y = window.snap_marker.getData()
            np.testing.assert_array_equal(marker_x, [1.0])
            np.testing.assert_array_equal(marker_y, [1.0])
            self.assertEqual(
                window.snap_marker.zValue(),
                -1,
            )
            self.assertGreater(
                line.curve.zValue(),
                window.snap_marker.zValue(),
            )
        finally:
            window._hide_snap_marker()
            widget.deleteLater()

    def test_real_plot_data_item_log_toggle_and_data_change_keep_raw_boundary(self):
        line = pg.PlotDataItem(x=[1.0, 10.0, 100.0], y=[2.0, 20.0, 200.0])

        try:
            line.setLogMode(True, False)
            np.testing.assert_allclose(line.getData()[0], [0.0, 1.0, 2.0])
            np.testing.assert_array_equal(
                line.getOriginalDataset()[0],
                [1.0, 10.0, 100.0],
            )

            line.setData(x=[10.0, 100.0, 1000.0], y=[3.0, 30.0, 300.0])
            np.testing.assert_allclose(line.getData()[0], [1.0, 2.0, 3.0])
            np.testing.assert_array_equal(
                line.getOriginalDataset()[0],
                [10.0, 100.0, 1000.0],
            )

            line.setLogMode(False, True)
            np.testing.assert_array_equal(line.getData()[0], [10.0, 100.0, 1000.0])
            np.testing.assert_allclose(
                line.getData()[1],
                np.log10([3.0, 30.0, 300.0]),
            )
            np.testing.assert_array_equal(
                line.getOriginalDataset()[1],
                [3.0, 30.0, 300.0],
            )
        finally:
            line.deleteLater()

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

    def test_free_cursor_converts_both_log_view_coordinates_to_physical_values(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[1.0, 10.0, 100.0], y=[1.0, 10.0, 100.0])
        line.setLogMode(True, True)
        plot_item.getAxis("bottom").setLogMode(True)
        plot_item.getAxis("left").setLogMode(True)
        window = plot1d.__new__(plot1d)
        qtw.QMainWindow.__init__(window)
        window.plot = plot_item
        window.line = line
        window.pos_labels = {
            "index": qtw.QLabel(),
            "x": qtw.QLabel(),
            "y": qtw.QLabel(),
        }
        window.formatNum = lambda value: f"{value:.3g}"

        try:
            plot_item.vb.setRange(xRange=(0.0, 2.0), yRange=(0.0, 2.0), padding=0)
            scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(1.3, 0.7))
            plotWidget.mouseMoved(window, scene_pos)

            self.assertEqual(window.pos_labels["x"].text(), "x = 20;")
            self.assertEqual(window.pos_labels["y"].text(), "y = 5.01")
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

    def test_snap_report_keeps_cross_database_trace_display_label(self):
        class Dataset:
            run_id = 1

        class Param:
            name = "voltage"
            label = "Conductance"

        class Gate:
            name = "gate"
            label = "Gate voltage"

        class Source:
            label = "ID:1 voltage"
            ds = Dataset()
            param = Param()
            axis_options = {"x": "gate", "y": "voltage"}
            param_dict = {"gate": Gate(), "voltage": param}

        class Line:
            from_win = Source()

        trace_key = TraceKey(
            DatasetKey("database-b.db", "shared-guid"),
            "voltage",
            )
        window = plot1d.__new__(plot1d)
        window.line = object()
        window.lines = {trace_key: Line()}
        window._axis_selection = {"x": "gate", "y": "voltage"}
        window.param_dict = Source.param_dict
        window.trace_label = qtw.QLabel()
        window.toolbarCo_ord = qtw.QToolBar()

        window._show_snap_report(trace_key, 2)

        self.assertEqual(
            window.trace_label.text(),
            "Run 1: Conductance vs Gate voltage (snapped to point 1)",
        )
        self.assertEqual(window.trace_label.toolTip(), Source.label)

        window.line = Line()
        window._clear_snap_report()

        self.assertEqual(
            window.trace_label.text(),
            "Run 1: Conductance vs Gate voltage",
            )

    def test_swapped_secondary_snap_report_uses_its_horizontal_measurement(self):
        class Param:
            def __init__(self, name, label):
                self.name = name
                self.label = label

        gate = Param("gate", "Gate voltage")
        conductance = Param("conductance", "Conductance")
        current = Param("current", "Current")
        source = type(
            "Source",
            (),
            {
                "label": "ID:2 current",
                "ds": type("Dataset", (), {"run_id": 2})(),
                "param": current,
                "axis_options": {"x": "gate", "y": "current"},
                "param_dict": {"gate": gate, "current": current},
            },
        )()
        secondary = type(
            "Line",
            (),
            {"from_win": source, "choose_from": ("y", "x")},
        )()

        window = plot1d.__new__(plot1d)
        window.line = object()
        window.lines = {"secondary": secondary}
        window._axis_selection = {"x": "conductance", "y": "gate"}
        window.param_dict = {"gate": gate, "conductance": conductance}
        window.trace_label = qtw.QLabel()
        window.toolbarCo_ord = qtw.QToolBar()

        window._show_snap_report("secondary", 3)

        self.assertEqual(
            window.trace_label.text(),
            "Run 2: Gate voltage vs Current (snapped to point 2)",
        )

    def test_swap_plot_axes_exchanges_loaded_data_labels_and_refreshes(self):
        class Param:
            def __init__(self, name, label, unit, depends_on_=()):
                self.name = name
                self.label = label
                self.unit = unit
                self.depends_on_ = depends_on_

        class BaseWindow(qtw.QMainWindow):
            @property
            def axis_options(self):
                return {
                    axis: dropdown.currentText()
                    for axis, dropdown in self.axis_dropdown.items()
                    }

            def _set_param_axis_labels(self):
                self.plot.setLabel(
                    "bottom",
                    text=self.axis_param["x"].label,
                    units=self.axis_param["x"].unit,
                    )
                self.plot.setLabel(
                    "left",
                    text=self.axis_param["y"].label,
                    units=self.axis_param["y"].unit,
                    )

        class Host(Plot1DTraceMixin, BaseWindow):
            pass

        class SecondaryLine:
            def __init__(self, parent, source):
                self.parent = parent
                self.from_win = source
                self.choose_from = ("x", "y")
                self.data = (None, None)
                self.side = "right"

            def refresh(self):
                self.choose_from = _subplot_axis_order(
                    self.parent.axis_options,
                    self.from_win.axis_options,
                    shared_parameter="gate",
                )
                if self.choose_from is None:
                    self.setData(x=[], y=[])
                    return
                self.setData(
                    x=self.from_win.axis_data[self.choose_from[0]],
                    y=self.from_win.axis_data[self.choose_from[1]],
                )

            def setData(self, *, x, y):
                self.data = (np.asarray(x), np.asarray(y))

            def getData(self):
                return self.data

            def setPen(self, _pen):
                pass

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

            def setZValue(self, _value):
                pass

        host = Host()
        widget = pg.GraphicsLayoutWidget()
        try:
            gate = Param("gate", "Gate voltage", "V")
            conductance = Param(
                "conductance",
                "Conductance",
                "S",
                depends_on_=("gate",),
                )
            host.param = conductance
            host.param_dict = {"gate": gate, "conductance": conductance}
            host.axis_dropdown = {}
            for axis, current in (("x", "gate"), ("y", "conductance")):
                dropdown = qtw.QComboBox()
                dropdown.addItems(["gate", "conductance"])
                dropdown.setCurrentText(current)
                host.axis_dropdown[axis] = dropdown
            host._axis_selection = {"x": "gate", "y": "conductance"}
            host.axis_data = {
                "x": np.array([1.0, 2.0]),
                "y": np.array([10.0, 20.0]),
                }
            host.axis_param = {"x": gate, "y": conductance}
            host.plot = widget.addPlot()
            host.line = host.plot.plot(
                x=host.axis_data["x"],
                y=host.axis_data["y"],
                )
            current = Param("current", "Current", "A", depends_on_=("gate",))
            source = type(
                "Source",
                (),
                {
                    "axis_options": {"x": "gate", "y": "current"},
                    "axis_data": {
                        "x": np.array([1.0, 2.0]),
                        "y": np.array([100.0, 200.0]),
                    },
                    "axis_param": {"x": gate, "y": current},
                    "param_dict": {"gate": gate, "current": current},
                    "param": current,
                    "visible": True,
                },
            )()
            secondary = SecondaryLine(host, source)
            secondary.refresh()
            host.label = "main"
            host.lines = {host.label: host.line, "secondary": secondary}
            host._trace_styles = {
                host.label: host._initial_trace_style(),
                "secondary": host._initial_trace_style(order=1),
            }
            host._trace_styles["secondary"].y_axis = "Right"
            host.right_vb = None
            host.top_vb = None
            host.top_right_vb = None
            host.marquee = None
            host._trace_appearance_dialog = None
            refreshes = []
            host.refreshWindow = lambda force=False: refreshes.append(force)

            self.assertTrue(host.set_plot_axes_swapped(True))

            self.assertEqual(
                host.axis_options,
                {"x": "conductance", "y": "gate"},
                )
            np.testing.assert_array_equal(host.axis_data["x"], [10.0, 20.0])
            np.testing.assert_array_equal(host.axis_data["y"], [1.0, 2.0])
            np.testing.assert_array_equal(host.line.getData()[0], [10.0, 20.0])
            np.testing.assert_array_equal(host.line.getData()[1], [1.0, 2.0])
            np.testing.assert_array_equal(secondary.getData()[0], [100.0, 200.0])
            np.testing.assert_array_equal(secondary.getData()[1], [1.0, 2.0])
            self.assertEqual(secondary.choose_from, ("y", "x"))
            self.assertEqual(host._trace_styles["secondary"].x_axis, "Top")
            self.assertEqual(host._trace_styles["secondary"].y_axis, "Left")
            self.assertEqual(host.plot.getAxis("bottom").labelText, "Conductance")
            self.assertEqual(host.plot.getAxis("left").labelText, "Gate voltage")
            self.assertEqual(host.plot.getAxis("top").labelText, "Current")
            self.assertTrue(host.plot.getAxis("top").style["showValues"])
            self.assertFalse(host.plot.getAxis("right").style["showValues"])
            self.assertEqual(refreshes, [True])

            self.assertTrue(host.set_plot_axes_swapped(False))
            self.assertEqual(
                host.axis_options,
                {"x": "gate", "y": "conductance"},
                )
            np.testing.assert_array_equal(secondary.getData()[0], [1.0, 2.0])
            np.testing.assert_array_equal(secondary.getData()[1], [100.0, 200.0])
            self.assertEqual(secondary.choose_from, ("x", "y"))
            self.assertEqual(host._trace_styles["secondary"].x_axis, "Bottom")
            self.assertEqual(host._trace_styles["secondary"].y_axis, "Right")
            self.assertFalse(host.plot.getAxis("top").style["showValues"])
            self.assertTrue(host.plot.getAxis("right").style["showValues"])
            self.assertEqual(host.plot.getAxis("right").labelText, "Current")
            self.assertEqual(refreshes, [True, True])
        finally:
            widget.deleteLater()
            host.deleteLater()

    def test_trace_appearance_swap_checkbox_applies_and_disables_when_invalid(self):
        class Host(qtw.QMainWindow):
            def __init__(self):
                super().__init__()
                self.lines = {}
                self.applied = []
                self.valid = True
                self.swapped = False

            def can_swap_plot_axes(self):
                return self.valid

            def plot_axes_swapped(self):
                return self.swapped

            def set_plot_axes_swapped(self, checked):
                if not self.valid:
                    return False
                self.swapped = checked
                self.applied.append(checked)
                return True

        host = Host()
        dialog = _TraceAppearanceDialog(host)
        try:
            self.assertTrue(dialog.swap_axes.isEnabled())
            self.assertFalse(dialog.swap_axes.isChecked())

            dialog.swap_axes.setChecked(True)

            self.assertEqual(host.applied, [True])
            self.assertTrue(host.swapped)

            host.valid = False
            dialog.sync_swap_axes_control()

            self.assertFalse(dialog.swap_axes.isEnabled())
            self.assertFalse(dialog.swap_axes.isChecked())
        finally:
            dialog.deleteLater()
            host.deleteLater()

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

    def test_secondary_trace_axis_visibility_tracks_selected_sides(self):
        class Axis:
            def __init__(self):
                self.show_values = None
                self.label = None

            def setStyle(self, *, showValues):
                self.show_values = showValues

            def setLabel(self, *, text, units):
                self.label = (text, units)

        class Plot:
            def __init__(self):
                self.axes = {"left": Axis(), "right": Axis(), "top": Axis()}

            def getAxis(self, name):
                return self.axes[name]

        class Line:
            def __init__(self, source=None):
                self.side = "left"
                self.from_win = source

            def setPen(self, _pen):
                pass

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

            def set_side(self, side):
                self.side = side

            def setZValue(self, _value):
                pass

        window = plot1d.__new__(plot1d)
        window.plot = Plot()
        window.label = "main"
        window.line = Line()
        window.param = type(
            "Param",
            (),
            {"name": "conductance", "label": "Conductance", "unit": "S"},
        )()
        window.axis_param = {
            "x": type(
                "Param",
                (),
                {"name": "gate", "label": "Gate voltage", "unit": "V"},
            )(),
            }
        source = type(
            "Source",
            (),
            {
                "param": type(
                    "Param",
                    (),
                    {"name": "current", "label": "Current", "unit": "A"},
                )(),
            },
        )()
        secondary = Line(source)
        window.lines = {"main": window.line, "secondary": secondary}
        window._trace_styles = {
            "main": window._initial_trace_style(),
            "secondary": window._initial_trace_style(),
            }

        window._apply_trace_style("secondary", secondary)
        self.assertFalse(window.plot.axes["right"].show_values)
        self.assertEqual(window.plot.axes["right"].label, ("", ""))
        self.assertFalse(window.plot.axes["top"].show_values)
        self.assertEqual(window.plot.axes["top"].label, ("", ""))

        window._set_trace_y_axis("secondary", "Right")
        self.assertEqual(secondary.side, "right")
        self.assertTrue(window.plot.axes["right"].show_values)
        self.assertEqual(window.plot.axes["right"].label, ("Current", "A"))

        window._set_trace_y_axis("secondary", "Left")
        self.assertEqual(secondary.side, "left")
        self.assertFalse(window.plot.axes["right"].show_values)
        self.assertEqual(window.plot.axes["right"].label, ("", ""))

        window._set_trace_y_axis("main", "Right")
        self.assertEqual(window._trace_styles["main"].y_axis, "Right")
        self.assertEqual(window.line.side, "right")
        self.assertTrue(window.plot.axes["right"].show_values)
        self.assertEqual(window.plot.axes["right"].label, ("Conductance", "S"))
        self.assertTrue(window.plot.axes["left"].show_values)
        self.assertEqual(window.plot.axes["left"].label, ("Current", "A"))

        window._set_trace_y_axis("main", "Left")
        self.assertEqual(window.line.side, "left")
        self.assertFalse(window.plot.axes["right"].show_values)

        window._trace_styles["secondary"].x_axis = "Top"
        window._apply_trace_style("secondary", secondary)
        self.assertTrue(window.plot.axes["top"].show_values)
        self.assertEqual(window.plot.axes["top"].label, ("Gate voltage", "V"))

        window._trace_styles["secondary"].x_axis = "Bottom"
        window._apply_trace_style("secondary", secondary)
        self.assertFalse(window.plot.axes["top"].show_values)
        self.assertEqual(window.plot.axes["top"].label, ("", ""))

    def test_top_trace_has_independent_autoscaled_horizontal_viewbox(self):
        class Host(Plot1DTraceMixin, PlotAxisScalingMixin, qtw.QMainWindow):
            def _view_range_changed_programmatically(self):
                pass

        host = Host()
        host.widget = pg.GraphicsLayoutWidget()
        host.vb = custom_viewbox()
        host.vb.setDefaultPadding(0)
        host.plot = host.widget.addPlot(viewBox=host.vb)
        host.vb.setParent(host.plot)
        host.right_vb = None
        host.top_vb = None
        host.top_right_vb = None
        host._axis_scale_controls = {}
        host._axis_scale_custom_auto_axes = set()
        host.label = "blue"
        host.param = type("Param", (), {"name": "conductance"})()
        host.axis_param = {
            "x": type(
                "Param",
                (),
                {"name": "voltage", "label": "Voltage", "unit": "V"},
            )(),
        }
        blue = host.plot.plot(x=[-100.0, 100.0], y=[-1.0, 1.0])
        green = host.plot.plot(x=[-10.0, 10.0], y=[-3.0, 4.0])
        host.line = blue
        host.lines = {"blue": blue, "green": green}
        host._trace_styles = {
            "blue": host._initial_trace_style(),
            "green": host._initial_trace_style(),
        }
        host._trace_styles["green"].x_axis = "Top"

        try:
            host._apply_trace_style("green", green)
            host.vb.autoRange()
            host._refresh_top_axis_auto_range()

            bottom_range = host.vb.viewRange()[0]
            top_range = host.top_vb.viewRange()[0]
            left_range = host.vb.viewRange()[1]
            self.assertIs(green.getViewBox(), host.top_vb)
            self.assertIs(host.plot.getAxis("top").linkedView(), host.top_vb)
            self.assertLessEqual(bottom_range[0], -100.0)
            self.assertGreaterEqual(bottom_range[1], 100.0)
            self.assertLessEqual(top_range[0], -10.0)
            self.assertGreaterEqual(top_range[1], 10.0)
            self.assertGreater(top_range[0], -50.0)
            self.assertLess(top_range[1], 50.0)
            self.assertLessEqual(left_range[0], -3.0)
            self.assertGreaterEqual(left_range[1], 4.0)

            host._trace_styles["green"].y_axis = "Right"
            host._apply_trace_style("green", green)
            host._refresh_top_axis_auto_range()
            top_right_range = host.top_vb.viewRange()[0]
            left_only_range = host.vb.viewRange()[1]
            right_range = host.right_vb.viewRange()[1]
            self.assertIs(green.getViewBox(), host.top_right_vb)
            self.assertLessEqual(top_right_range[0], -10.0)
            self.assertGreaterEqual(top_right_range[1], 10.0)
            self.assertGreater(top_right_range[0], -50.0)
            self.assertLess(top_right_range[1], 50.0)
            self.assertGreater(left_only_range[0], -2.0)
            self.assertLess(left_only_range[1], 2.0)
            self.assertLessEqual(right_range[0], -3.0)
            self.assertGreaterEqual(right_range[1], 4.0)

            host._axis_scale_custom_auto_axes.clear()
            host.vb.setXRange(-1.0, 1.0, padding=0)
            host.vb.setYRange(-0.1, 0.1, padding=0)
            host.top_vb.setXRange(-1.0, 1.0, padding=0)
            host.right_vb.setYRange(-0.1, 0.1, padding=0)
            host.force_all_axes_autoscale()
            self.assertEqual(
                host._axis_scale_custom_auto_axes,
                {"x", "y", "x2", "y2"},
            )
            self.assertLessEqual(host.vb.viewRange()[0][0], -100.0)
            self.assertGreaterEqual(host.vb.viewRange()[0][1], 100.0)
            self.assertLessEqual(host.vb.viewRange()[1][0], -1.0)
            self.assertGreaterEqual(host.vb.viewRange()[1][1], 1.0)
            self.assertLessEqual(host.top_vb.viewRange()[0][0], -10.0)
            self.assertGreaterEqual(host.top_vb.viewRange()[0][1], 10.0)
            self.assertLessEqual(host.right_vb.viewRange()[1][0], -3.0)
            self.assertGreaterEqual(host.right_vb.viewRange()[1][1], 4.0)
        finally:
            host.deleteLater()
            host.widget.deleteLater()

    def test_axis_swap_moves_a_right_secondary_trace_to_the_top_viewbox(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        class Param:
            def __init__(self, name, label, unit):
                self.name = name
                self.label = label
                self.unit = unit

        host = Host()
        host.widget = pg.GraphicsLayoutWidget()
        host.vb = custom_viewbox()
        host.vb.setDefaultPadding(0)
        host.plot = host.widget.addPlot(viewBox=host.vb)
        host.vb.setParent(host.plot)
        host.right_vb = None
        host.top_vb = None
        host.top_right_vb = None

        gate = Param("gate", "Gate voltage", "V")
        conductance = Param("conductance", "Conductance", "S")
        current = Param("current", "Current", "A")
        host.param = conductance
        host.axis_param = {"x": gate, "y": conductance}
        host.label = "main"
        host.line = host.plot.plot(x=[0.0, 1.0], y=[1.0, 2.0])
        secondary = host.plot.plot(x=[0.0, 1.0], y=[3.0, 4.0])
        secondary.from_win = type(
            "Source",
            (),
            {
                "axis_param": {"x": gate, "y": current},
                "param": current,
            },
        )()
        secondary.choose_from = ("x", "y")
        host.lines = {host.label: host.line, "secondary": secondary}
        host._trace_styles = {
            host.label: host._initial_trace_style(),
            "secondary": host._initial_trace_style(order=1),
        }
        host._trace_styles["secondary"].y_axis = "Right"

        try:
            host._apply_trace_style("secondary", secondary)
            self.assertIs(secondary.getViewBox(), host.right_vb)

            host._transpose_trace_axis_assignments()

            self.assertEqual(host._trace_styles["secondary"].x_axis, "Top")
            self.assertEqual(host._trace_styles["secondary"].y_axis, "Left")
            self.assertIs(secondary.getViewBox(), host.top_vb)

            host._transpose_trace_axis_assignments()

            self.assertEqual(host._trace_styles["secondary"].x_axis, "Bottom")
            self.assertEqual(host._trace_styles["secondary"].y_axis, "Right")
            self.assertIs(secondary.getViewBox(), host.right_vb)
        finally:
            host.deleteLater()
            host.widget.deleteLater()

    def test_main_plot_gestures_update_top_x_and_right_y_once(self):
        class MainViewBox:
            def sceneBoundingRect(self):
                return QtCore.QRectF(1.0, 2.0, 300.0, 200.0)

        class OverlayViewBox:
            def __init__(self):
                self.geometry = None
                self.wheels = []
                self.drags = []

            def setGeometry(self, geometry):
                self.geometry = geometry

            def wheelEvent(self, event, axis=None):
                self.wheels.append((event, axis))

            def mouseDragEvent(self, event, axis=None):
                self.drags.append((event, axis))

        host = Plot1DTraceMixin.__new__(Plot1DTraceMixin)
        host.vb = MainViewBox()
        host.right_vb = OverlayViewBox()
        host.top_vb = OverlayViewBox()
        host.top_right_vb = OverlayViewBox()
        wheel = type("QGraphicsSceneWheelEvent", (), {})()
        drag = type("MouseDragEvent", (), {})()

        host.updateViews(wheel)
        host.updateViews(drag)

        self.assertEqual(host.right_vb.wheels, [(wheel, 1)])
        self.assertEqual(host.right_vb.drags, [(drag, 1)])
        self.assertEqual(host.top_vb.wheels, [(wheel, 0)])
        self.assertEqual(host.top_vb.drags, [(drag, 0)])
        self.assertEqual(host.top_right_vb.wheels, [])
        self.assertEqual(host.top_right_vb.drags, [])
        for viewbox in (host.right_vb, host.top_vb, host.top_right_vb):
            self.assertEqual(viewbox.geometry, host.vb.sceneBoundingRect())

        host.right_vb.drags.clear()
        host.top_vb.drags.clear()
        host.vb._main_moved_axis = 0
        host.updateViews(drag)
        self.assertEqual(host.right_vb.drags, [])
        self.assertEqual(host.top_vb.drags, [(drag, 0)])

        host.top_vb.drags.clear()
        host.vb._main_moved_axis = 1
        host.updateViews(drag)
        self.assertEqual(host.right_vb.drags, [(drag, 1)])
        self.assertEqual(host.top_vb.drags, [])

    def test_failed_secondary_construction_rolls_back_dataset_and_picker(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            make_ds = QtCore.pyqtSignal(object)
            remove_dataset = QtCore.pyqtSignal(object)

        class Combo:
            def __init__(self, data, text):
                self.data = data
                self.text = text
                self.enabled = False

            def currentData(self):
                return self.data

            def currentText(self):
                return self.text

            def setEnabled(self, enabled):
                self.enabled = enabled

        class Button:
            def __init__(self):
                self.enabled = True

            def setEnabled(self, enabled):
                self.enabled = enabled

        class Box:
            def __init__(self, trace_key, label):
                self.option_box = Combo(trace_key, label)
                self.del_box = Button()
                self.reset_calls = []

            def reset_box(self, labels, item_data=None):
                self.reset_calls.append((labels, item_data))
                self.option_box.data = None
                self.option_box.text = ""

        dataset_key = DatasetKey("database.db", "guid")
        trace_key = TraceKey(dataset_key, "signal")
        source = type(
            "Source",
            (),
            {
                "_dataset_key": dataset_key,
                "_trace_key": trace_key,
                "label": "ID:2 signal",
            },
        )()
        box = Box(trace_key, source.label)
        host = Host()
        retained = []
        released = []
        try:
            host.right_vb = object()
            host.mergable = [source]
            host.option_boxes = [box]
            host.lines = {}
            host.make_ds.connect(retained.append)
            host.remove_dataset.connect(released.append)

            with (
                    patch(
                        "qplot.windows._plot1d_traces.subplot1d",
                        side_effect=RuntimeError("subplot construction failed"),
                        ),
                    self.assertRaisesRegex(RuntimeError, "construction failed"),
                    ):
                host.add_line(source.label, trace_key)

            self.assertEqual(retained, [dataset_key])
            self.assertEqual(released, [dataset_key])
            self.assertEqual(host.mergable, [source])
            self.assertTrue(box.option_box.enabled)
            self.assertFalse(box.del_box.enabled)
            self.assertEqual(box.reset_calls, [([source.label], [trace_key])])
            self.assertEqual(host.lines, {})
        finally:
            host.deleteLater()

    def test_main_trace_control_uses_authoritative_initial_style(self):
        class Theme:
            colors = [QtGui.QColor("#008000")]

        class Config:
            theme = Theme()

        class BaseWindow(qtw.QMainWindow):
            def initAxes(self):
                pass

        class Host(Plot1DTraceMixin, BaseWindow):
            pass

        host = Host()
        try:
            host.config = Config()
            host.label = "main"
            host.line = pg.PlotDataItem()
            host.axes_dock = QDock_context("Data axes", host)
            host.addDockWidget(
                QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
                host.axes_dock,
                )

            host.initAxes()

            main_picker = host.box_layout.itemAt(0).widget()
            stored_color = host._trace_styles[host.label].line_color
            self.assertEqual(stored_color, TRACE_COLOR_PALETTE[0])
            self.assertEqual(main_picker.color_box.color().name(), stored_color)
            self.assertEqual(host.line.opts["pen"].color().name(), stored_color)
        finally:
            host.deleteLater()

    def test_theme_change_reapplies_authoritative_trace_style_and_control(self):
        class Theme:
            colors = [QtGui.QColor("#ff0000")]

        class Config:
            theme = Theme()

        class BaseWindow(qtw.QMainWindow):
            def initAxes(self):
                pass

            def update_theme(self, config):
                self.config = config
                for line in self.lines.values():
                    line.setPen(pg.mkPen(config.theme.colors[0]))

        class Host(Plot1DTraceMixin, BaseWindow):
            pass

        host = Host()
        try:
            host.config = Config()
            host.label = "main"
            host.line = pg.PlotDataItem()
            host.axes_dock = QDock_context("Data axes", host)
            host.addDockWidget(
                QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
                host.axes_dock,
                )
            host.initAxes()
            style = host._trace_styles[host.label]
            style.line_color = "#123456"
            style.line_width = 3.5
            style.line_style = "Dash"
            host._apply_trace_style(host.label, host.line)

            secondary_label = "secondary"
            secondary_line = pg.PlotDataItem()
            secondary_picker = picker_1d(host, host.config, [secondary_label])
            secondary_style = host._TraceStyle(
                line_color="#654321",
                line_width=4.5,
                line_style="Dot",
                )
            host.lines[secondary_label] = secondary_line
            host._trace_styles[secondary_label] = secondary_style
            host._trace_controls[secondary_label] = secondary_picker
            host._apply_trace_style(secondary_label, secondary_line)

            host.update_theme(Config())

            main_picker = host.box_layout.itemAt(0).widget()
            pen = host.line.opts["pen"]
            self.assertEqual(style.line_color, "#123456")
            self.assertEqual(main_picker.color_box.color().name(), style.line_color)
            self.assertEqual(pen.color().name(), style.line_color)
            self.assertEqual(pen.widthF(), style.line_width)
            self.assertEqual(pen.style(), QtCore.Qt.PenStyle.DashLine)
            secondary_pen = secondary_line.opts["pen"]
            self.assertEqual(
                secondary_picker.color_box.color().name(),
                secondary_style.line_color,
                )
            self.assertEqual(
                secondary_pen.color().name(),
                secondary_style.line_color,
                )
            self.assertEqual(secondary_pen.widthF(), secondary_style.line_width)
            self.assertEqual(secondary_pen.style(), QtCore.Qt.PenStyle.DotLine)
        finally:
            host.deleteLater()

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

    def test_double_clicking_trace_opens_appearance_for_that_trace(self):
        class Param:
            name = "current"
            label = "Current"
            unit = "A"

        class Source:
            label = "ID:2 current"
            param = Param()

        class DoubleClickEvent:
            def button(self):
                return QtCore.Qt.MouseButton.LeftButton

            def double(self):
                return True

        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        host = Host()
        dialog = None
        try:
            secondary_key = "secondary"
            host.label = "ID:1 conductance"
            host.param = type("Param", (), {"name": "conductance"})()
            host.line = pg.PlotDataItem()
            secondary = pg.PlotDataItem()
            secondary.from_win = Source()
            host.lines = {host.label: host.line, secondary_key: secondary}
            host._trace_styles = {
                host.label: host._TraceStyle(),
                secondary_key: host._TraceStyle(),
                }

            host._install_trace_appearance_click_handler(secondary_key, secondary)
            self.assertTrue(secondary.curve.clickable)

            secondary.sigClicked.emit(secondary, DoubleClickEvent())

            dialog = host._trace_appearance_dialog
            self.assertIsNotNone(dialog)
            self.assertEqual(dialog._selected_labels(), [secondary_key])
            self.assertEqual(
                dialog._trace_key_for_row(dialog.table.currentRow()),
                secondary_key,
                )
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_table_uses_readonly_preview_rows(self):
        class Param:
            name = "current"
            depends_on_ = ("gate",)

        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = Param()
            host._guid = "guid"
            host._dataset_key = DatasetKey("database.db", host._guid)
            host.line = pg.PlotDataItem()
            host.lines = {host.label: host.line}
            host._trace_styles = {
                host.label: host._TraceStyle(line_color="#d62728", dots_enabled=True)
                }

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            preview_item = dialog.table.item(0, dialog._COL_PREVIEW)
            measurement_item = dialog.table.item(0, dialog._COL_MEASUREMENT)

            self.assertEqual(
                dialog.table.editTriggers(),
                qtw.QAbstractItemView.EditTrigger.NoEditTriggers,
                )
            self.assertEqual(
                dialog.table.horizontalHeader().sectionResizeMode(
                    dialog._COL_MEASUREMENT
                ),
                qtw.QHeaderView.ResizeMode.Stretch,
                )
            self.assertEqual(dialog.table.columnCount(), 3)
            self.assertEqual(
                dialog.table.horizontalScrollBarPolicy(),
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                )
            self.assertFalse(hasattr(dialog, "order"))
            self.assertFalse(hasattr(dialog, "move_up_button"))
            self.assertFalse(hasattr(dialog, "move_down_button"))
            self.assertEqual(
                [dialog.x_axis.itemText(index) for index in range(dialog.x_axis.count())],
                ["Bottom", "Top"],
            )
            self.assertNotIn(
                "Traces",
                [group.title() for group in dialog.findChildren(qtw.QGroupBox)],
            )
            self.assertFalse(
                any(
                    label.text().startswith("Editing ")
                    for label in dialog.findChildren(qtw.QLabel)
                )
            )
            self.assertEqual(dialog.table.rowHeight(0), 28)
            self.assertEqual(preview_item.text(), "")
            self.assertFalse(preview_item.icon().isNull())
            self.assertFalse(preview_item.flags() & QtCore.Qt.ItemFlag.ItemIsDragEnabled)
            self.assertFalse(measurement_item.flags() & QtCore.Qt.ItemFlag.ItemIsEditable)
            self.assertTrue(
                dialog.table.item(0, dialog._COL_ID).flags()
                & QtCore.Qt.ItemFlag.ItemIsDragEnabled
            )
            self.assertTrue(
                measurement_item.flags() & QtCore.Qt.ItemFlag.ItemIsDragEnabled
            )
            id_rect = dialog.table.visualRect(
                dialog.table.model().index(0, dialog._COL_ID)
            )
            dialog.table._update_reorder_cursor(id_rect.center())
            self.assertEqual(
                dialog.table.viewport().cursor().shape(),
                QtCore.Qt.CursorShape.SizeVerCursor,
            )
            preview_rect = dialog.table.visualRect(
                dialog.table.model().index(0, dialog._COL_PREVIEW)
            )
            dialog.table._update_reorder_cursor(preview_rect.center())
            self.assertNotEqual(
                dialog.table.viewport().cursor().shape(),
                QtCore.Qt.CursorShape.SizeVerCursor,
            )
            dialog.table._show_drop_indicator(0)
            self.assertFalse(dialog.table._drop_indicator.isHidden())
            self.assertEqual(dialog.table._drop_indicator.height(), 2)
            dialog.table._hide_drop_indicator()
            self.assertTrue(dialog.table._drop_indicator.isHidden())
            self.assertEqual(
                dialog.table.defaultDropAction(),
                QtCore.Qt.DropAction.MoveAction,
            )
            self.assertFalse(dialog.line_color.itemIcon(0).isNull())

            dialog.table.selectRow(0)
            dialog._sync_controls_from_selection()
            self.assertTrue(dialog.y_axis.isEnabled())
            dialog.x_axis.setCurrentText("Top")
            self.assertEqual(host._trace_styles[host.label].x_axis, "Top")

            dialog.dots_enable.setChecked(True)
            dialog.marker_enable.setChecked(True)
            self.assertFalse(dialog.dots_enable.isChecked())
            self.assertTrue(dialog.marker_enable.isChecked())

            dialog.dots_enable.setChecked(True)
            self.assertTrue(dialog.dots_enable.isChecked())
            self.assertFalse(dialog.marker_enable.isChecked())
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_cut_preview_is_not_draggable_but_row_can_be_reordered(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        cut_key = TraceKey(
            DatasetKey("database.db", "guid"),
            "heatmap_signal",
            sweep_id=1,
        )
        cut_source = type(
            "CutSource",
            (),
            {
                "_guid": "guid",
                "_dataset_key": cut_key.dataset_key,
                "label": "ID:1 heatmap_signal [cut 2]",
                "param": type(
                    "Param",
                    (),
                    {
                        "name": "heatmap_signal",
                        "depends_on_": ("gate", "field"),
                    },
                )(),
            },
        )()
        cut_line = type("CutLine", (), {"from_win": cut_source})()
        host = Host()
        dialog = None
        try:
            host.label = "ID:2 current"
            host.param = type(
                "Param",
                (),
                {"name": "current", "depends_on_": ("gate",)},
            )()
            host._guid = "target-guid"
            host._dataset_key = DatasetKey("database.db", host._guid)
            host.line = object()
            host.lines = {host.label: host.line, cut_key: cut_line}
            host._trace_styles = {
                label: host._initial_trace_style()
                for label in host.lines
            }

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            preview_item = dialog.table.item(1, dialog._COL_PREVIEW)
            self.assertFalse(
                preview_item.flags() & QtCore.Qt.ItemFlag.ItemIsDragEnabled
            )
            self.assertTrue(
                dialog.table.item(1, dialog._COL_ID).flags()
                & QtCore.Qt.ItemFlag.ItemIsDragEnabled
            )

            dialog._move_rows_to_position([1], 0)

            self.assertEqual(list(host.lines), [cut_key, host.label])
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_adds_and_removes_secondary_traces(self):
        secondary_key = "secondary-key"

        class Source:
            label = "ID:2 current"
            param = type("Param", (), {"name": "current"})()

        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            def add_trace_from_dialog(self, label, trace_key):
                self.added.append((label, trace_key))
                source = self.mergable.pop(0)
                line = type("SecondaryLine", (), {"from_win": source})()
                self.lines[trace_key] = line
                self._trace_styles[trace_key] = self._initial_trace_style()

            def remove_line(self, label, trace_key=None):
                self.removed.append((label, trace_key))
                self.lines.pop(trace_key)
                self._trace_styles.pop(trace_key)
                self.mergable.append(self.source)

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 conductance"
            host.param = type("Param", (), {"name": "conductance"})()
            host.line = object()
            host.lines = {host.label: host.line}
            host._trace_styles = {host.label: host._initial_trace_style()}
            host.source = Source()
            host.source._trace_key = secondary_key
            host.mergable = [host.source]
            host.added = []
            host.removed = []

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            self.assertEqual(dialog.add_trace_combo.count(), 2)
            self.assertFalse(dialog.add_trace_button.isEnabled())
            self.assertFalse(dialog.remove_trace_button.isEnabled())

            dialog.add_trace_combo.setCurrentIndex(
                dialog.add_trace_combo.findData(secondary_key)
            )
            self.assertTrue(dialog.add_trace_button.isEnabled())
            dialog.add_trace_button.click()

            self.assertEqual(host.added, [(host.source.label, secondary_key)])
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog._selected_labels(), [secondary_key])
            self.assertTrue(dialog.remove_trace_button.isEnabled())

            dialog.remove_trace_button.click()

            self.assertEqual(host.removed, [(host.source.label, secondary_key)])
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog._selected_labels(), [host.label])
            self.assertFalse(dialog.remove_trace_button.isEnabled())
            self.assertGreaterEqual(
                dialog.add_trace_combo.findData(secondary_key),
                1,
            )
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_adds_database_candidate_without_open_source_window(self):
        source_key = DatasetKey("database.db", "source-guid")
        trace_key = TraceKey(source_key, "current")

        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            def add_trace_from_dialog(self, label, selected_trace_key):
                self.added.append((label, selected_trace_key))
                self.lines[selected_trace_key] = type("Line", (), {})()
                self._trace_styles[selected_trace_key] = self._initial_trace_style()

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 conductance"
            host.param = type("Param", (), {"name": "conductance"})()
            host.line = object()
            host.lines = {host.label: host.line}
            host._trace_styles = {host.label: host._initial_trace_style()}
            host.mergable = []
            host.added = []
            host._trace_candidate_provider = lambda: [("ID:2 current", trace_key)]

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()

            self.assertEqual(dialog.add_trace_combo.count(), 2)
            dialog.add_trace_combo.setCurrentIndex(1)
            dialog.add_trace_button.click()

            self.assertEqual(host.added, [("ID:2 current", trace_key)])
            self.assertIn(trace_key, host.lines)
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_add_bridge_claims_empty_legacy_picker(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        host = Host()
        option_box = qtw.QComboBox()
        delete_button = qtw.QPushButton()
        picker = type(
            "Picker",
            (),
            {"option_box": option_box, "del_box": delete_button},
        )()
        added = []
        try:
            host.option_boxes = [picker]
            host.add_line = lambda label, key: added.append((label, key))

            host.add_trace_from_dialog("ID:2 current", "secondary-key")

            self.assertEqual(added, [("ID:2 current", "secondary-key")])
            self.assertEqual(option_box.currentData(), "secondary-key")
            self.assertFalse(option_box.isEnabled())
            self.assertTrue(delete_button.isEnabled())
        finally:
            option_box.deleteLater()
            delete_button.deleteLater()
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

    def test_trace_appearance_opacity_updates_selected_trace(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = type("Param", (), {"name": "current"})()
            host.line = pg.PlotDataItem()
            host.lines = {host.label: host.line}
            host._trace_styles = {host.label: host._initial_trace_style()}

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()
            dialog.opacity_slider.setValue(37)

            self.assertEqual(dialog.opacity_slider.value(), 37)
            self.assertEqual(dialog.opacity.value(), 37)
            self.assertEqual(host._trace_styles[host.label].opacity, 0.37)
            self.assertEqual(host.line.opacity(), 1.0)
            effect = host.line.graphicsEffect()
            self.assertIsInstance(effect, qtw.QGraphicsOpacityEffect)
            self.assertEqual(effect.opacity(), 0.37)

            dialog.opacity.setValue(100)

            self.assertEqual(dialog.opacity_slider.value(), 100)
            self.assertIsNone(host.line.graphicsEffect())
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_updates_legacy_trace_control(self):
        class Theme:
            colors = [QtGui.QColor("#008000")]

        class Config:
            theme = Theme()

        class BaseWindow(qtw.QMainWindow):
            def initAxes(self):
                pass

        class Host(Plot1DTraceMixin, BaseWindow):
            pass

        host = Host()
        dialog = None
        try:
            host.config = Config()
            host.label = "ID:1 current"
            host.param = type("Param", (), {"name": "current"})()
            host.line = pg.PlotDataItem()
            host.axes_dock = QDock_context("Data axes", host)
            host.addDockWidget(
                QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
                host.axes_dock,
                )
            host.initAxes()
            main_picker = host.box_layout.itemAt(0).widget()

            dialog = _TraceAppearanceDialog(host)
            dialog.refresh_rows()
            dialog._set_combo_value(dialog.line_color, "#d62728")
            dialog._apply_selection()

            style = host._trace_styles[host.label]
            self.assertEqual(style.line_color, "#d62728")
            self.assertEqual(main_picker.color_box.color().name(), style.line_color)
            self.assertEqual(host.line.opts["pen"].color().name(), style.line_color)
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_trace_appearance_plot_order_follows_dragged_table_position(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            pass

        class Line:
            def __init__(self, source=None):
                self.z_value = None
                self.refresh_count = 0
                if source is not None:
                    self.from_win = source

            def setZValue(self, value):
                self.z_value = value

            def refresh(self):
                self.refresh_count += 1

        source = type(
            "Source",
            (),
            {
                "visible": True,
                "ds": type("Dataset", (), {"running": False})(),
            },
        )()

        host = Host()
        dialog = None
        try:
            host.label = "ID:1 current"
            host.param = type("Param", (), {"name": "current"})()
            main_line = Line()
            host.line = main_line
            host.lines = {
                "trace a": main_line,
                "trace b": Line(source),
                "trace c": Line(source),
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
            dialog._move_rows_to_position([1], 0)

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

            host.refresh_secondary_lines()

            self.assertEqual(main_line.refresh_count, 0)
            self.assertEqual(host.lines["trace b"].refresh_count, 1)
            self.assertEqual(host.lines["trace c"].refresh_count, 1)
            self.assertEqual(
                host._secondary_lines(),
                [host.lines["trace b"], host.lines["trace c"]],
            )
        finally:
            if dialog is not None:
                dialog.deleteLater()
            host.deleteLater()

    def test_close_releases_reordered_secondary_traces_only(self):
        class Host(Plot1DTraceMixin, qtw.QMainWindow):
            remove_dataset = QtCore.pyqtSignal([object])

        class Monitor:
            def __init__(self):
                self.stop_count = 0

            def stop(self):
                self.stop_count += 1

        class Secondary:
            def __init__(self, source):
                self.from_win = source
                self.disconnect_count = 0

            def disconnect_source_updates(self):
                self.disconnect_count += 1

        dataset_key = DatasetKey("database.db", "guid")
        source = type(
            "Source",
            (),
            {
                "_dataset_key": dataset_key,
                "visible": False,
                "monitor": Monitor(),
            },
        )()
        host = Host()
        released = []
        try:
            host.line = object()
            secondary = Secondary(source)
            host.lines = {"secondary": secondary, "main": host.line}
            host.remove_dataset.connect(released.append)

            host.closeEvent(QtGui.QCloseEvent())

            self.assertEqual(released, [dataset_key])
            self.assertEqual(source.monitor.stop_count, 0)
            self.assertEqual(secondary.disconnect_count, 1)
            self.assertEqual(host.lines, {"main": host.line})
        finally:
            host.deleteLater()

    def test_legacy_label_picker_matches_resolved_trace_key(self):
        class Combo:
            def currentData(self):
                return "ID:1 voltage"

            def currentText(self):
                return "ID:1 voltage"

        box = type("Box", (), {"option_box": Combo()})()
        trace_key = TraceKey(
            DatasetKey("database.db", "guid"),
            "voltage",
        )

        self.assertTrue(
            Plot1DTraceMixin._picker_matches_trace(
                box,
                trace_key,
                "ID:1 voltage",
            )
        )

    def test_subplot_axis_mapping_requires_source_to_match_host_displayed_x(self):
        self.assertEqual(
            _subplot_axis_order(
                {"x": "gate", "y": "current"},
                {"x": "gate", "y": "field"},
                source_is_cut=True,
            ),
            ("x", "y"),
        )
        self.assertIsNone(
            _subplot_axis_order(
                {"x": "current", "y": "gate"},
                {"x": "gate", "y": "field"},
                source_is_cut=True,
            )
        )
        self.assertIsNone(
            _subplot_axis_order(
                {"x": "gate", "y": "current"},
                {"x": "field", "y": "gate"},
                source_is_cut=True,
            )
        )
        self.assertEqual(
            _subplot_axis_order(
                {"x": "gate", "y": "current"},
                {"x": "field", "y": "gate"},
            ),
            ("y", "x"),
        )

        self.assertEqual(
            _subplot_axis_order(
                {"x": "current", "y": "gate"},
                {"x": "gate", "y": "conductance"},
                shared_parameter="gate",
            ),
            ("y", "x"),
        )
        self.assertEqual(
            _subplot_axis_order(
                {"x": "current", "y": "gate"},
                {"x": "conductance", "y": "gate"},
                shared_parameter="gate",
            ),
            ("x", "y"),
        )
        self.assertIsNone(
            _subplot_axis_order(
                {"x": "current", "y": "gate"},
                {"x": "field", "y": "current"},
                shared_parameter="gate",
            )
        )
        self.assertEqual(
            _subplot_axis_order(
                {"x": "current", "y": "gate"},
                {"x": "gate", "y": "field"},
                source_is_cut=True,
                shared_parameter="gate",
            ),
            ("y", "x"),
        )
        self.assertIsNone(
            _subplot_axis_order(
                {"x": "current", "y": "gate"},
                {"x": "field", "y": "gate"},
                source_is_cut=True,
                shared_parameter="gate",
            )
        )

    def test_completed_cut_updates_and_clears_merged_subplot_immediately(self):
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def disconnect(self, slot):
                if slot not in self.slots:
                    raise TypeError("slot is not connected")
                self.slots.remove(slot)

            def emit(self, *args):
                for slot in list(self.slots):
                    slot(*args)

        class Plot:
            def __init__(self):
                self.items = []

            def addItem(self, item):
                self.items.append(item)

        source = type(
            "CutSource",
            (),
            {
                "label": "ID:1 signal [cut 1]",
                "param_dict": {},
                "sweep_id": 0,
                "ds": type("Dataset", (), {"running": False})(),
                "worker": type("Worker", (), {"running": False})(),
                "end_wait": Signal(),
                "trace_updated": Signal(),
                "merge_compatibility_changed": Signal(),
                "axis_options": {"x": "gate", "y": "field"},
                "axis_data": {
                    "x": np.array([0.0, 1.0]),
                    "y": np.array([10.0, 11.0]),
                },
            },
        )()
        parent = type(
            "Parent",
            (),
            {
                "axis_options": {"x": "gate", "y": "current"},
                "plot": Plot(),
            },
        )()
        line = subplot1d(parent, source)

        _, initial_y = line.getData()
        np.testing.assert_array_equal(initial_y, [10.0, 11.0])

        source.axis_data["y"] = np.array([20.0, 21.0])
        source.trace_updated.emit()
        _, updated_y = line.getData()
        np.testing.assert_array_equal(updated_y, [20.0, 21.0])

        source.axis_options = {"x": "field", "y": "gate"}
        source.merge_compatibility_changed.emit()
        empty_x, empty_y = line.getData()
        self.assertTrue(empty_x is None or len(empty_x) == 0)
        self.assertTrue(empty_y is None or len(empty_y) == 0)

        line.disconnect_source_updates()
        source.axis_options = {"x": "gate", "y": "field"}
        source.axis_data["y"] = np.array([30.0, 31.0])
        source.trace_updated.emit()
        disconnected_x, disconnected_y = line.getData()
        self.assertTrue(disconnected_x is None or len(disconnected_x) == 0)
        self.assertTrue(disconnected_y is None or len(disconnected_y) == 0)

    def test_live_regular_source_waits_for_final_trace_update_signal(self):
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def disconnect(self, slot):
                if slot not in self.slots:
                    raise TypeError("slot is not connected")
                self.slots.remove(slot)

            def emit(self, *args):
                for slot in list(self.slots):
                    slot(*args)

        class Plot:
            def addItem(self, _item):
                pass

        source = type(
            "LineSource",
            (),
            {
                "label": "ID:2 signal",
                "param_dict": {},
                "visible": True,
                "ds": type("Dataset", (), {"running": True})(),
                "worker": type("Worker", (), {"running": True})(),
                "end_wait": Signal(),
                "trace_updated": Signal(),
                "axis_options": {"x": "gate", "y": "signal"},
                "axis_data": {
                    "x": np.array([0.0, 1.0]),
                    "y": np.array([10.0, 11.0]),
                },
            },
        )()
        parent = type(
            "Parent",
            (),
            {
                "axis_options": {"x": "gate", "y": "current"},
                "plot": Plot(),
            },
        )()

        line = subplot1d(parent, source)

        initial_x, initial_y = line.getData()
        self.assertTrue(initial_x is None or len(initial_x) == 0)
        self.assertTrue(initial_y is None or len(initial_y) == 0)
        self.assertEqual(source.end_wait.slots, [])

        source.worker.running = False
        source.axis_data["y"] = np.array([20.0, 21.0])
        source.trace_updated.emit()

        _, updated_y = line.getData()
        np.testing.assert_array_equal(updated_y, [20.0, 21.0])

    def test_hidden_source_monitor_is_owned_until_last_subplot_disconnects(self):
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def disconnect(self, slot):
                if slot not in self.slots:
                    raise TypeError("slot is not connected")
                self.slots.remove(slot)

            def emit(self, *args):
                for slot in list(self.slots):
                    slot(*args)

        class SpinBox:
            def __init__(self, value):
                self._value = value
                self.valueChanged = Signal()

            def value(self):
                return self._value

            def setValue(self, value):
                self._value = value

        class Monitor:
            def __init__(self):
                self.stop_count = 0

            def stop(self):
                self.stop_count += 1

        class Plot:
            def addItem(self, _item):
                pass

        source = type(
            "HiddenSource",
            (),
            {
                "label": "ID:2 signal",
                "param_dict": {},
                "visible": False,
                "ds": type("Dataset", (), {"running": True})(),
                "worker": type("Worker", (), {"running": False})(),
                "end_wait": Signal(),
                "trace_updated": Signal(),
                "axis_options": {"x": "gate", "y": "signal"},
                "axis_data": {
                    "x": np.array([0.0, 1.0]),
                    "y": np.array([10.0, 11.0]),
                },
                "spinBox": SpinBox(1.0),
                "monitor": Monitor(),
            },
        )()
        first_parent = type(
            "Parent",
            (),
            {
                "axis_options": {"x": "gate", "y": "current"},
                "plot": Plot(),
                "spinBox": SpinBox(0.2),
            },
        )()
        second_parent = type(
            "Parent",
            (),
            {
                "axis_options": {"x": "gate", "y": "current"},
                "plot": Plot(),
                "spinBox": SpinBox(0.4),
            },
        )()

        first = subplot1d(first_parent, source)
        second = subplot1d(second_parent, source)

        self.assertEqual(source._merged_trace_users, 2)
        self.assertEqual(source.spinBox.value(), 0.4)
        first_parent.spinBox.valueChanged.emit(0.3)
        self.assertEqual(source.spinBox.value(), 0.3)

        first.disconnect_source_updates()
        first.disconnect_source_updates()
        self.assertEqual(source._merged_trace_users, 1)
        self.assertEqual(source.monitor.stop_count, 0)
        first_parent.spinBox.valueChanged.emit(0.1)
        self.assertEqual(source.spinBox.value(), 0.3)

        second.disconnect_source_updates()
        self.assertEqual(source._merged_trace_users, 0)
        self.assertEqual(source.monitor.stop_count, 1)

    def test_register_main_line_replaces_initial_empty_trace(self):
        line = object()
        window = plot1d.__new__(plot1d)
        window.label = "main"
        window.line = line
        window.lines = {"main": None}

        window._register_main_line()

        self.assertIs(window.lines["main"], line)

    def test_same_label_cross_database_trace_does_not_replace_main_line(self):
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
            label = "ID:1 voltage"
            _guid = "shared-guid"
            _dataset_key = DatasetKey("database-b.db", "shared-guid")
            _trace_key = TraceKey(_dataset_key, "voltage")
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
            make_ds = QtCore.pyqtSignal([object])
            remove_dataset = QtCore.pyqtSignal([object])
            get_mergables = QtCore.pyqtSignal()

        host = Host()
        source = SourceWindow()
        made_datasets = []
        removed_datasets = []
        picker_updates = []
        trace_dialog = None

        try:
            host.config = Config()
            host.widget = pg.GraphicsLayoutWidget()
            host.vb = custom_viewbox()
            host.vb.setDefaultPadding(0)
            host.plot = host.widget.addPlot(viewBox=host.vb)
            host.vb.setParent(host.plot)
            host.axes_dock = QDock_context("Data axes", host)
            host.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, host.axes_dock)
            host.lineScroll = qtw.QScrollArea()
            host.scrollWidget = qtw.QWidget()
            host.lineScroll.setWidget(host.scrollWidget)
            host.box_layout = qtw.QVBoxLayout(host.scrollWidget)
            host.box_layout.addStretch()
            host.box_count = 1
            host.right_vb = None
            host.line = host.plot.plot(x=[0.0, 1.0], y=[1.0, 2.0])
            host.label = source.label
            host._guid = "shared-guid"
            host.param = type(
                "Param",
                (),
                {"name": "voltage", "depends_on_": ("gate",)},
            )()
            host._dataset_key = DatasetKey("database-a.db", "shared-guid")
            host._trace_key = TraceKey(host._dataset_key, "voltage")
            host.lines = {host.label: host.line}
            host.axis_options = {"x": "gate", "y": "current"}
            host.mergable = [source]
            host.make_ds.connect(made_datasets.append)
            host.remove_dataset.connect(removed_datasets.append)
            host.get_mergables.connect(lambda: picker_updates.append(True))

            selected_box = picker_1d(
                host,
                host.config,
                [source.label],
                item_data=[source._trace_key],
                )
            selected_box.option_box.setCurrentIndex(0)
            selected_box.axis_side.setCurrentText("Right")
            selected_box.color_box.setColor(host.config.theme.colors[1])
            host.option_boxes = [selected_box]

            host.add_line(source.label, source._trace_key)
            secondary = host.lines[source._trace_key]

            self.assertEqual(made_datasets, [source._dataset_key])
            self.assertEqual(host.mergable, [])
            self.assertIs(host.lines[host.label], host.line)
            self.assertIsNotNone(host.right_vb)
            self.assertEqual(secondary.side, "right")
            self.assertIn(secondary, host.right_vb.addedItems)
            self.assertTrue(host.plot.getAxis("right").style["showValues"])
            self.assertEqual(host.option_boxes[0], selected_box)
            self.assertEqual(len(host.option_boxes), 2)

            host.vb.setGeometry(QtCore.QRectF(10.0, 20.0, 300.0, 200.0))
            self.assertEqual(
                host.right_vb.geometry(),
                host.vb.sceneBoundingRect(),
                )

            selected_box.color_box.selectedColor.emit(QtGui.QColor("#123456"))
            selected_box.axis_side.setCurrentText("Left")

            style = host._trace_styles[source._trace_key]
            self.assertEqual(style.line_color, "#123456")
            self.assertEqual(style.y_axis, "Left")
            self.assertEqual(secondary.opts["pen"].color().name(), style.line_color)
            self.assertEqual(secondary.side, "left")

            trace_dialog = _TraceAppearanceDialog(host)
            host._trace_appearance_dialog = trace_dialog
            trace_dialog.refresh_rows()
            self.assertEqual(trace_dialog.table.rowCount(), 2)

            host.remove_line(source.label, source._trace_key)

            self.assertNotIn(source._trace_key, host.lines)
            self.assertIs(host.lines[host.label], host.line)
            self.assertNotIn(secondary, host.right_vb.addedItems)
            self.assertFalse(host.plot.getAxis("right").style["showValues"])
            self.assertEqual(removed_datasets, [source._dataset_key])
            self.assertEqual(picker_updates, [True])
            self.assertEqual(trace_dialog.table.rowCount(), 1)
            trace_dialog.table.selectRow(0)
            self.assertEqual(trace_dialog._selected_labels(), [host.label])
        finally:
            if trace_dialog is not None:
                trace_dialog.deleteLater()
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

    def test_shift_pan_locks_to_the_initial_dominant_axis(self):
        class Event:
            def __init__(self, position, *, start=False, finish=False):
                self._position = QtCore.QPointF(*position)
                self._start = start
                self._finish = finish

            def button(self):
                return QtCore.Qt.MouseButton.LeftButton

            def modifiers(self):
                return QtCore.Qt.KeyboardModifier.ShiftModifier

            def isStart(self):
                return self._start

            def isFinish(self):
                return self._finish

            def pos(self):
                return self._position

            def buttonDownPos(self, _button):
                return QtCore.QPointF(0.0, 0.0)

        viewbox = custom_viewbox()
        start_event = Event((0.0, 0.0), start=True)
        first_event = Event((2.0, 12.0))
        finish_event = Event((40.0, 13.0), finish=True)

        with patch.object(pg.ViewBox, "mouseDragEvent") as mouse_drag:
            viewbox.mouseDragEvent(start_event)
            viewbox.mouseDragEvent(first_event)
            viewbox.mouseDragEvent(finish_event)

        self.assertEqual(
            mouse_drag.call_args_list,
            [
                ((start_event,), {"axis": None}),
                ((first_event,), {"axis": 1}),
                ((finish_event,), {"axis": 1}),
                ],
            )
        self.assertIsNone(viewbox._shift_pan_axis)

    def test_drag_without_shift_is_unconstrained_by_default(self):
        class Event:
            def button(self):
                return QtCore.Qt.MouseButton.LeftButton

            def modifiers(self):
                return QtCore.Qt.KeyboardModifier.NoModifier

            def isStart(self):
                return False

            def isFinish(self):
                return False

        event = Event()
        viewbox = custom_viewbox()

        with patch.object(pg.ViewBox, "mouseDragEvent") as mouse_drag:
            viewbox.mouseDragEvent(event)

        mouse_drag.assert_called_once_with(event, axis=None)

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

        self.assertIn("X range: 1.00 to 4.00", stats_text)
        self.assertIn("Y range: 2.00 to 6.00", stats_text)

    def test_log_marquee_uses_view_mask_raw_statistics_and_physical_ranges(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[1.0, 10.0, 100.0], y=[1.0, 10.0, 100.0])
        line.setLogMode(True, True)
        plot_item.getAxis("bottom").setLogMode(True)
        plot_item.getAxis("left").setLogMode(True)
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.line = line
        window.lines = {"main": line}
        window._trace_styles = {
            "main": type(
                "Style",
                (),
                {"x_axis": "Bottom", "y_axis": "Left"},
            )(),
        }
        window.marquee = QtCore.QRectF(0.0, 0.0, 2.0, 2.0)
        window.formatNum = lambda value: f"{value:.2f}"

        try:
            np.testing.assert_array_equal(
                window._marquee_line_values(),
                [1.0, 10.0, 100.0],
            )
            stats_text = window._marquee_stats_text()

            self.assertIn("X range: 1.00 to 100.00", stats_text)
            self.assertIn("Y range: 1.00 to 100.00", stats_text)
            self.assertIn("Average: 37.00", stats_text)
            self.assertIn("Max: 100.00", stats_text)
            self.assertNotIn("Average: 1.00", stats_text)
        finally:
            widget.deleteLater()

    def test_log_marquee_geometry_excludes_nonpositive_raw_samples(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(
            x=[-1.0, 0.0, 1.0, 10.0, 100.0],
            y=[-10.0, 0.0, 1.0, 10.0, 100.0],
        )
        line.setLogMode(True, True)
        window = plot1d.__new__(plot1d)
        window.line = line
        window.marquee = QtCore.QRectF(-0.1, -0.1, 2.2, 2.2)

        try:
            view_x, view_y = line.getData()
            self.assertTrue(np.all(np.isnan(view_x[:2])))
            self.assertTrue(np.all(np.isnan(view_y[:2])))
            np.testing.assert_array_equal(
                window._marquee_line_values(),
                [1.0, 10.0, 100.0],
            )
        finally:
            widget.deleteLater()

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
        range_changes = []
        window._view_range_changed_programmatically = lambda: range_changes.append(True)

        self.assertTrue(window.zoom_marquee("xy"))
        self.assertEqual(window.vb.x_range, (1.0, 4.0, 0))
        self.assertEqual(window.vb.y_range, (2.0, 6.0, 0))
        self.assertEqual(range_changes, [True])

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
