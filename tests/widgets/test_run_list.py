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
            "1,000 = 10 x 100"
            )

    def test_format_point_count_uses_setpoint_shape_without_measurement_factor(self):
        self.assertEqual(
            treeWidgets.format_point_count({
                "setpoint_shape": [108, 861],
                "setpoint_count": 92988,
                "point_shape": [108, 861, 2],
                "expected_results": 185976,
                }),
            "92,988 = 108 x 861"
            )

    def test_duration_uses_commas(self):
        self.assertEqual(
            treeWidgets.format_time_taken_seconds({
                "run_timestamp": 100.0,
                "completed_timestamp": 12345.6,
                "is_completed": True,
                }),
            "12,245.6 s"
            )

    def test_setpoints_delegate_uses_normal_text_color_for_selection(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            option = qtw.QStyleOptionViewItem()
            option.widget = run_list
            option.state = qtw.QStyle.State_Selected | qtw.QStyle.State_Enabled

            self.assertEqual(
                delegate._text_color(option),
                option.palette.color(QtGui.QPalette.Text)
                )

            option.state |= qtw.QStyle.State_Active | qtw.QStyle.State_HasFocus
            self.assertEqual(
                delegate._text_color(option),
                option.palette.color(QtGui.QPalette.Text)
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_left_aligns_shape_text(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )

            self.assertEqual(
                int(delegate.right_text_alignment),
                int(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_treats_zero_as_left_count_text(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "sweep-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 100,
                    "setpoint_shape": [10, 10],
                    "result_count": 100,
                    },
                2: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "empty-guid",
                    "sweep_parameters": [],
                    "measure_parameters": ["signal"],
                    "result_count": 0,
                    },
                })
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            items = {
                run_list.topLevelItem(row).guid: run_list.topLevelItem(row)
                for row in range(run_list.topLevelItemCount())
                }
            zero_item = items["empty-guid"]
            setpoints_col = run_list.cols.index("Setpoints")
            metrics = QtGui.QFontMetrics(run_list.font())

            self.assertEqual(zero_item.text(setpoints_col), "0")
            self.assertEqual(delegate._display_sections("0"), ("0", None))
            self.assertGreater(
                delegate._max_right_width(
                    run_list.indexFromItem(zero_item, setpoints_col),
                    metrics,
                    ),
                0
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_uses_widest_shape_text_for_equals_alignment(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "small-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 100,
                    "setpoint_shape": [10, 10],
                    "result_count": 100,
                    },
                2: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "medium-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 10000,
                    "setpoint_shape": [100, 100],
                    "result_count": 10000,
                    },
                3: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "large-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 1000000,
                    "setpoint_shape": [1000, 1000],
                    "result_count": 1000000,
                    },
                })
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            item = run_list.topLevelItem(0)
            setpoints_col = run_list.cols.index("Setpoints")
            metrics = QtGui.QFontMetrics(run_list.font())

            self.assertEqual(
                delegate._max_right_width(
                    run_list.indexFromItem(item, setpoints_col),
                    metrics,
                    ),
                metrics.horizontalAdvance("1,000 x 1,000")
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_resize_columns_prioritises_setpoints_space(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.resize(583, 300)
            run_list.show()
            qtw.QApplication.processEvents()
            run_list._resize_columns()

            widths = {
                name: run_list.columnWidth(col)
                for col, name in enumerate(run_list.cols)
                }

            self.assertGreater(widths["Setpoints"], widths["Started"])
            self.assertLess(widths["Measurements"], 96)
            run_list.hide()
        finally:
            treeWidgets.isfile = old_isfile

    def test_unknown_completion_duration_uses_database_modified_time(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": None,
                "result_count": 185976,
                "expected_results": None,
                }),
            "unknown"
            )
        self.assertEqual(
            treeWidgets.format_time_taken_seconds({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": None,
                "result_count": 185976,
                "expected_results": None,
                "database_modified_timestamp": 12345.6,
                }),
            "12,245.6 s"
            )

    def test_incomplete_duration_uses_database_modified_time(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": False,
                "result_count": 25,
                "expected_results": 100,
                }),
            "25.0%"
            )
        self.assertEqual(
            treeWidgets.format_time_taken_seconds({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": False,
                "result_count": 25,
                "expected_results": 100,
                "database_modified_timestamp": 12345.6,
                }),
            "12,245.6 s"
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
            self.assertRegex(items["unfinished-guid"].text(5), r"^[\d,]+\.\d s$")
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
            self.assertEqual(items["finished-guid"].text(2), "1,000 = 10 x 100")
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


