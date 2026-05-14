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
