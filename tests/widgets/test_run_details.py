import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets import treeWidgets
from qplot.windows._widgets.preview import (
    PREVIEW_BACKGROUND_COLOR,
    PREVIEW_SELECTED_PROPERTY,
    PREVIEW_SIZE,
    DraggablePreviewImageLabel,
    PreviewTab,
    generate_run_previews,
    render_heatmap_preview,
    render_sparkline_preview,
)


class RunDetailsTabsTestCase(unittest.TestCase):
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
            run_timestamp_raw = 1_768_129_603
            completed_timestamp_raw = 1_768_129_626.1

            def get_parameters(self):
                return [
                    Param("dac_ch1", "Gate ch1", "V"),
                    Param("dmm_v1", "Gate v1", "V", ("dac_ch1",)),
                    ]

            def run_timestamp(self):
                return 1_768_129_603

            def completed_timestamp(self):
                return 1_768_129_626.1

            def get_parameter_data(self, name):
                raise AssertionError("Details pane should not load parameter data")

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
        self.assertEqual(widget.parameters.item(1, 3).text(), "25")
        self.assertEqual(widget.parameters.item(1, 4).text(), "25")
        self.assertEqual(widget.parameters.item(1, 5).text(), "")
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
        self.assertEqual(widget.metadata.textElideMode(), QtCore.Qt.TextElideMode.ElideNone)
        self.assertEqual(widget.raw.textElideMode(), QtCore.Qt.TextElideMode.ElideNone)
        self.assertEqual(
            widget.metadata.horizontalScrollBarPolicy(),
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
        self.assertIsInstance(
            widget.raw.itemDelegateForColumn(1),
            treeWidgets.WrappedValueDelegate
            )
        self.assertEqual(
            widget.metadata.header().sectionResizeMode(1),
            qtw.QHeaderView.ResizeMode.Stretch
            )
        self.assertEqual(
            widget.raw.header().sectionResizeMode(1),
            qtw.QHeaderView.ResizeMode.Stretch
            )
        self.assertEqual(
            widget.parameters.horizontalHeader().sectionResizeMode(7),
            qtw.QHeaderView.ResizeMode.Stretch
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
            qtw.QHeaderView.ResizeMode.Stretch
            )

    def test_run_details_populates_set_parameter_sweep_summary_from_result_table(self):
        class Param:
            def __init__(self, name, label, unit, axes=()):
                self.name = name
                self.label = label
                self.unit = unit
                self.depends_on_ = axes

        class Dataset:
            table_name = "results-1-1"
            running = False

            def get_parameters(self):
                return [
                    Param("dac_ch1", "Gate ch1", "V"),
                    Param("dac_ch2", "Gate ch2", "V"),
                    Param("dmm_v1", "Gate v1", "V", ("dac_ch1", "dac_ch2")),
                    ]

            def get_parameter_data(self, name):
                raise AssertionError("Details pane should not load parameter data")

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "details.db")
            conn = sqlite3.connect(database_path)
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute("""
                  CREATE TABLE "results-1-1" (
                      dac_ch1 REAL,
                      dac_ch2 REAL,
                      dmm_v1 REAL
                  )
                """)
                cursor.executemany(
                    'INSERT INTO "results-1-1" VALUES (?, ?, ?)',
                    [
                        (-1.0, -2.0, 1.0),
                        (-1.0, -0.5, 2.0),
                        (-1.0, 1.0, 3.0),
                        (0.0, -2.0, 4.0),
                        (0.0, -0.5, 5.0),
                        (0.0, 1.0, 6.0),
                        (1.0, -2.0, 7.0),
                        (1.0, -0.5, 8.0),
                        (1.0, 1.0, 9.0),
                        ]
                    )
                conn.commit()
            finally:
                if cursor is not None:
                    cursor.close()
                conn.close()

            widget = treeWidgets.moreInfo()
            widget.setInfo(
                {
                    "Data Structure": {
                        "Data points": 9,
                        "dac_ch1": {"unit": "V", "label": "Gate ch1"},
                        "dac_ch2": {"unit": "V", "label": "Gate ch2"},
                        "dmm_v1": {
                            "unit": "V",
                            "label": "Gate v1",
                            "axes": ["dac_ch1", "dac_ch2"],
                            },
                        },
                    "Snapshot": {
                        "station": {},
                        "parameters": {
                            "dac_ch1": {
                                "full_name": "dac_ch1",
                                "post_delay": 0.02,
                                "instrument_name": "dac",
                                },
                            "dac_ch2": {
                                "full_name": "dac_ch2",
                                "post_delay": 0.03,
                                "instrument_name": "dac",
                                },
                            },
                        },
                    },
                Dataset(),
                run_metadata={"result_table_name": "results-1-1"},
                database_path=database_path,
                )

        self.assertEqual(widget.parameters.item(1, 0).text(), "dac_ch1")
        self.assertEqual(widget.parameters.item(1, 3).text(), "-1")
        self.assertEqual(widget.parameters.item(1, 4).text(), "1")
        self.assertEqual(widget.parameters.item(1, 5).text(), "3")
        self.assertEqual(widget.parameters.item(1, 6).text(), "0.02")
        self.assertEqual(widget.parameters.item(1, 7).text(), "dac")
        self.assertEqual(widget.parameters.item(2, 0).text(), "dac_ch2")
        self.assertEqual(widget.parameters.item(2, 3).text(), "-2")
        self.assertEqual(widget.parameters.item(2, 4).text(), "1")
        self.assertEqual(widget.parameters.item(2, 5).text(), "3")
        self.assertEqual(widget.parameters.item(2, 6).text(), "0.03")
        self.assertEqual(widget.parameters.item(2, 7).text(), "dac")
        self.assertEqual(widget.parameters.item(4, 0).text(), "dmm_v1")
        self.assertEqual(widget.parameters.item(4, 3).text(), "")
        self.assertEqual(widget.parameters.item(4, 5).text(), "")

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

    def test_heatmap_preview_uses_full_setpoint_shape_for_partial_data(self):
        heatmap = render_heatmap_preview(
            np.array([0, 1], dtype=float),
            np.array([0, 0], dtype=float),
            np.array([1, 2], dtype=float),
            size=40,
            grid_shape=(2, 4),
            )

        measured = QtGui.QColor(heatmap.pixel(15, 30))
        empty_future_column = QtGui.QColor(heatmap.pixel(35, 30))
        empty_future_row = QtGui.QColor(heatmap.pixel(15, 5))

        self.assertNotEqual(measured, QtGui.QColor(230, 230, 230))
        self.assertEqual(empty_future_column, QtGui.QColor(230, 230, 230))
        self.assertEqual(empty_future_row, QtGui.QColor(230, 230, 230))

    def test_heatmap_preview_does_not_allocate_oversized_grid_shape(self):
        old_max_cells = preview_module.MAX_PREVIEW_GRID_CELLS
        preview_module.MAX_PREVIEW_GRID_CELLS = 1
        try:
            heatmap = render_heatmap_preview(
                np.array([0, 1, 0, 1], dtype=float),
                np.array([0, 0, 1, 1], dtype=float),
                np.array([0, 255, 0, 0], dtype=float),
                size=20,
                grid_shape=(1000, 1000),
                )
        finally:
            preview_module.MAX_PREVIEW_GRID_CELLS = old_max_cells

        self.assertEqual(heatmap.width(), 20)
        self.assertEqual(heatmap.height(), 20)

    def test_heatmap_preview_downsamples_grid_by_averaging(self):
        grid = np.array([
            [0.0, 0.0],
            [100.0, 100.0],
            [0.0, 0.0],
            [100.0, 100.0],
            ])

        display_grid = preview_module._prepare_heatmap_display_grid(grid, size=2)

        np.testing.assert_allclose(display_grid, np.full((2, 2), 50.0))

    def test_heatmap_preview_fills_small_rendering_gaps_only(self):
        mostly_complete = np.arange(16, dtype=float).reshape(4, 4)
        mostly_complete[1, 1] = np.nan
        sparse = np.full((4, 4), np.nan)
        sparse[0, 0] = 1.0
        sparse[0, 1] = 2.0

        filled = preview_module._prepare_heatmap_display_grid(
            mostly_complete,
            size=4,
            )
        sparse_display = preview_module._prepare_heatmap_display_grid(
            sparse,
            size=4,
            )

        self.assertTrue(np.isfinite(filled).all())
        self.assertTrue(np.isnan(sparse_display[1:, :]).all())

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

    def test_generate_2d_preview_uses_metadata_setpoint_shape_for_partial_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "preview.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE results (slow_y REAL, fast_x REAL, signal REAL)")
                cursor.executemany(
                    "INSERT INTO results VALUES (?, ?, ?)",
                    [
                        (0.0, 0.0, 1.0),
                        (0.0, 1.0, 2.0),
                        ]
                    )
                conn.commit()
            finally:
                cursor.close()
                conn.close()

            previews = generate_run_previews(database_path, {
                "run_id": 8,
                "result_table_name": "results",
                "result_count": 2,
                "setpoint_shape": [2, 4],
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
                }, size=40)

        heatmap = previews[0]["image"]
        measured = QtGui.QColor(heatmap.pixel(15, 30))
        empty_future_column = QtGui.QColor(heatmap.pixel(35, 30))
        empty_future_row = QtGui.QColor(heatmap.pixel(15, 5))

        self.assertNotEqual(measured, QtGui.QColor(230, 230, 230))
        self.assertEqual(empty_future_column, QtGui.QColor(230, 230, 230))
        self.assertEqual(empty_future_row, QtGui.QColor(230, 230, 230))

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
        for card, title in zip(
                cards,
                ["signal vs x", "image vs x and y"],
                strict=False,
                ):
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

    def test_preview_tab_requeues_cached_preview_when_run_metadata_changes(self):
        preview = PreviewTab(preview_size=100)
        preview.database_path = "previews.db"
        preview._start_next = lambda: None

        old_metadata = {
            "guid": "run-guid",
            "run_id": 7,
            "result_table_name": "results",
            "result_count": 1,
            "is_completed": False,
            }
        preview.run_metadata = {"run-guid": old_metadata}
        preview.metadata_signatures = {
            "run-guid": preview._metadata_signature(old_metadata)
            }
        preview.cache = {"run-guid": ["stale preview"]}
        preview.errors = {"run-guid": "stale error"}

        preview.add_runs({
            7: {
                **old_metadata,
                "result_count": 100,
                "is_completed": True,
                "completed_timestamp": 123.0,
                },
            })

        self.assertNotIn("run-guid", preview.cache)
        self.assertNotIn("run-guid", preview.errors)
        self.assertIn("run-guid", preview.queue)

    def test_preview_tab_can_update_metadata_without_queueing_preview(self):
        preview = PreviewTab(preview_size=100)
        preview.database_path = "previews.db"
        preview._start_next = lambda: None

        old_metadata = {
            "guid": "run-guid",
            "run_id": 7,
            "result_table_name": "results",
            "result_count": 1,
            }
        preview.run_metadata = {"run-guid": old_metadata}
        preview.metadata_signatures = {
            "run-guid": preview._metadata_signature(old_metadata)
            }

        preview.add_runs({
            7: {
                **old_metadata,
                "result_count": 100,
                },
            }, queue_previews=False)

        self.assertEqual(preview.queue, {})
        self.assertEqual(preview.run_metadata["run-guid"]["result_count"], 100)

    def test_preview_tab_queues_every_run_on_database_load(self):
        preview = PreviewTab(preview_size=100)
        preview._start_next = lambda: None
        generation_changes = []
        preview.previewGenerationChanged.connect(
            lambda *args: generation_changes.append(args)
            )

        preview.set_database_runs("previews.db", {
            1: {"guid": "guid-1", "run_timestamp": 100.0},
            2: {"guid": "guid-2", "run_timestamp": 101.0},
            })

        self.assertEqual(preview.queue, {
            "guid-1": preview_module.PREVIEW_REMAINING_PRIORITY,
            "guid-2": preview_module.PREVIEW_REMAINING_PRIORITY,
            })
        self.assertEqual(generation_changes, [])

    def test_preview_tab_marks_only_active_worker_as_generating(self):
        preview = PreviewTab(preview_size=100)
        preview._schedule_start_next = lambda: None
        generation_changes = []
        started_workers = []
        preview.previewGenerationChanged.connect(
            lambda *args: generation_changes.append(args)
            )

        class ThreadPool:
            def start(self, worker):
                started_workers.append(worker)

        preview.thread_pool = ThreadPool()
        preview.set_database_runs("previews.db", {
            1: {"guid": "guid-1", "run_timestamp": 100.0},
            2: {"guid": "guid-2", "run_timestamp": 101.0},
            })

        preview._start_next()

        self.assertEqual(generation_changes, [("guid-2", True)])
        self.assertEqual(started_workers[0].guid, "guid-2")

        preview._start_next = lambda: None
        preview._worker_finished(preview.generation, "guid-2", [], None)

        self.assertIn(("guid-2", False), generation_changes)

    def test_stale_preview_callback_keeps_current_generation_active(self):
        preview = PreviewTab(preview_size=100)
        preview._schedule_start_next = lambda: None
        generation_changes = []
        started_workers = []
        preview.previewGenerationChanged.connect(
            lambda *args: generation_changes.append(args)
            )

        class ThreadPool:
            def start(self, worker):
                started_workers.append(worker)

        preview.thread_pool = ThreadPool()
        runs = {1: {"guid": "shared-guid", "run_timestamp": 100.0}}

        preview.set_database_runs("old.db", runs)
        preview._start_next()
        stale_generation = preview.generation

        preview.set_database_runs("current.db", runs)
        preview._start_next()
        current_generation = preview.generation

        preview._worker_finished(stale_generation, "shared-guid", [], None)

        self.assertEqual(len(started_workers), 2)
        self.assertEqual(
            preview.active,
            {(current_generation, "shared-guid")},
            )
        self.assertEqual(
            generation_changes,
            [("shared-guid", True), ("shared-guid", True)],
            )

        preview._start_next = lambda: None
        preview._worker_finished(current_generation, "shared-guid", [], None)

        self.assertEqual(preview.active, set())
        self.assertEqual(generation_changes[-1], ("shared-guid", False))

    def test_preview_tab_prioritizes_selected_then_visible_then_remaining_runs(self):
        preview = PreviewTab(preview_size=100)
        preview._start_next = lambda: None
        preview.set_database_runs("previews.db", {
            1: {"guid": "guid-1", "run_timestamp": 100.0},
            2: {"guid": "guid-2", "run_timestamp": 101.0},
            3: {"guid": "guid-3", "run_timestamp": 102.0},
            })

        preview.prioritize_runs(
            selected_run_ids=[2],
            visible_run_ids=[1, 2],
            )

        self.assertEqual(
            preview.queue["guid-2"],
            preview_module.PREVIEW_SELECTED_PRIORITY,
            )
        self.assertEqual(
            preview.queue["guid-1"],
            preview_module.PREVIEW_VISIBLE_PRIORITY,
            )
        self.assertEqual(
            preview.queue["guid-3"],
            preview_module.PREVIEW_REMAINING_PRIORITY,
            )

        preview.prioritize_runs(visible_run_ids=[3])

        self.assertEqual(
            preview.queue["guid-1"],
            preview_module.PREVIEW_REMAINING_PRIORITY,
            )
        self.assertEqual(
            preview.queue["guid-3"],
            preview_module.PREVIEW_VISIBLE_PRIORITY,
            )

    def test_preview_sampling_is_limited_without_result_count(self):
        old_max_preview_rows = preview_module.MAX_PREVIEW_ROWS
        preview_module.MAX_PREVIEW_ROWS = 3
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, signal REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?)",
                [(float(index), float(index * 10)) for index in range(10)]
                )

            x, signal = preview_module._select_arrays(
                cursor,
                "results",
                ["x", "signal"],
                {},
                )

            self.assertLessEqual(x.size, 3)
            self.assertEqual(x.tolist(), [0.0, 4.0, 8.0])
            self.assertEqual(signal.tolist(), [0.0, 40.0, 80.0])
        finally:
            conn.close()
            preview_module.MAX_PREVIEW_ROWS = old_max_preview_rows

    def test_2d_preview_reads_complete_modest_known_grid(self):
        old_max_preview_rows = preview_module.MAX_PREVIEW_ROWS
        old_max_grid_cells = preview_module.MAX_PREVIEW_GRID_CELLS
        preview_module.MAX_PREVIEW_ROWS = 3
        preview_module.MAX_PREVIEW_GRID_CELLS = 4
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, y REAL, signal REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?, ?)",
                [
                    (0.0, 0.0, 1.0),
                    (1.0, 0.0, 2.0),
                    (0.0, 1.0, 3.0),
                    (1.0, 1.0, 4.0),
                    ],
                )

            x, y, signal = preview_module._select_arrays(
                cursor,
                "results",
                ["x", "y", "signal"],
                {"result_count": 4},
                max_rows=preview_module._preview_2d_row_limit((2, 2)),
                sampling="stratified",
                )

            self.assertEqual(x.size, 4)
            self.assertEqual(y.size, 4)
            self.assertEqual(signal.size, 4)
        finally:
            conn.close()
            preview_module.MAX_PREVIEW_ROWS = old_max_preview_rows
            preview_module.MAX_PREVIEW_GRID_CELLS = old_max_grid_cells

    def test_large_known_grid_preview_uses_spatial_means_independent_of_row_order(self):
        old_max_preview_rows = preview_module.MAX_PREVIEW_ROWS
        old_max_grid_cells = preview_module.MAX_PREVIEW_GRID_CELLS
        preview_module.MAX_PREVIEW_ROWS = 16
        preview_module.MAX_PREVIEW_GRID_CELLS = 4
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            values = [
                (float(row), float(column), float(column % 2))
                for row in range(4)
                for column in range(40)
                ]
            metadata = {
                "result_count": len(values),
                "setpoint_shape": [4, 40],
                "measure_parameters": ["signal"],
                "sweep_parameters": ["slow_y", "fast_x"],
                }
            grids = []
            with patch.object(
                    preview_module,
                    "render_heatmap_grid_preview",
                    side_effect=lambda grid, size: np.array(grid, copy=True),
                    ):
                for table_name, table_values in (
                        ("forward_results", values),
                        ("reverse_results", list(reversed(values))),
                        ):
                    cursor.execute(
                        f"CREATE TABLE {table_name} "
                        "(slow_y REAL, fast_x REAL, signal REAL)"
                        )
                    cursor.executemany(
                        f"INSERT INTO {table_name} VALUES (?, ?, ?)",
                        table_values,
                        )
                    preview = preview_module._preview_2d(
                        cursor,
                        table_name,
                        metadata,
                        "signal",
                        ["slow_y", "fast_x"],
                        size=4,
                        )
                    grids.append(preview["image"])
        finally:
            conn.close()
            preview_module.MAX_PREVIEW_ROWS = old_max_preview_rows
            preview_module.MAX_PREVIEW_GRID_CELLS = old_max_grid_cells

        for grid in grids:
            self.assertEqual(grid.shape, (4, 4))
            np.testing.assert_allclose(grid, np.full((4, 4), 0.5))
        np.testing.assert_array_equal(grids[0], grids[1])

    def test_large_partial_preview_preserves_unmeasured_grid_area(self):
        old_max_preview_rows = preview_module.MAX_PREVIEW_ROWS
        old_max_grid_cells = preview_module.MAX_PREVIEW_GRID_CELLS
        preview_module.MAX_PREVIEW_ROWS = 16
        preview_module.MAX_PREVIEW_GRID_CELLS = 4
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE results "
                "(slow_y REAL, fast_x REAL, signal REAL)"
                )
            values = [
                (0.0, float(column), float(column % 2))
                for column in range(40)
                ]
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?, ?)",
                values,
                )
            metadata = {
                "result_count": len(values),
                "setpoint_shape": [4, 40],
                "measure_parameters": ["signal"],
                "sweep_parameters": ["slow_y", "fast_x"],
                }
            with patch.object(
                    preview_module,
                    "render_heatmap_grid_preview",
                    side_effect=lambda grid, size: np.array(grid, copy=True),
                    ):
                preview = preview_module._preview_2d(
                    cursor,
                    "results",
                    metadata,
                    "signal",
                    ["slow_y", "fast_x"],
                    size=4,
                    )
        finally:
            conn.close()
            preview_module.MAX_PREVIEW_ROWS = old_max_preview_rows
            preview_module.MAX_PREVIEW_GRID_CELLS = old_max_grid_cells

        grid = preview["image"]
        self.assertEqual(grid.shape, (4, 4))
        np.testing.assert_allclose(grid[0], np.full(4, 0.5))
        self.assertTrue(np.isnan(grid[1:]).all())

    def test_large_preview_without_shape_uses_spatial_means(self):
        old_max_preview_rows = preview_module.MAX_PREVIEW_ROWS
        old_max_grid_cells = preview_module.MAX_PREVIEW_GRID_CELLS
        preview_module.MAX_PREVIEW_ROWS = 16
        preview_module.MAX_PREVIEW_GRID_CELLS = 4
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            values = [
                (float(row), float(column), float(column % 2))
                for row in range(4)
                for column in range(40)
                ]
            metadata = {
                "result_count": len(values),
                "measure_parameters": ["signal"],
                "sweep_parameters": ["slow_y", "fast_x"],
                }
            grids = []
            with patch.object(
                    preview_module,
                    "render_heatmap_grid_preview",
                    side_effect=lambda grid, size: np.array(grid, copy=True),
                    ):
                for table_name, table_values in (
                        ("forward_results", values),
                        ("reverse_results", list(reversed(values))),
                        ):
                    cursor.execute(
                        f"CREATE TABLE {table_name} "
                        "(slow_y REAL, fast_x REAL, signal REAL)"
                        )
                    cursor.executemany(
                        f"INSERT INTO {table_name} VALUES (?, ?, ?)",
                        table_values,
                        )
                    preview = preview_module._preview_2d(
                        cursor,
                        table_name,
                        metadata,
                        "signal",
                        ["slow_y", "fast_x"],
                        size=4,
                        )
                    grids.append(preview["image"])
        finally:
            conn.close()
            preview_module.MAX_PREVIEW_ROWS = old_max_preview_rows
            preview_module.MAX_PREVIEW_GRID_CELLS = old_max_grid_cells

        for grid in grids:
            self.assertEqual(grid.shape, (4, 4))
            np.testing.assert_allclose(grid, np.full((4, 4), 0.5))
        np.testing.assert_array_equal(grids[0], grids[1])

    def test_large_preview_falls_back_to_streaming_spatial_means(self):
        old_max_preview_rows = preview_module.MAX_PREVIEW_ROWS
        old_max_grid_cells = preview_module.MAX_PREVIEW_GRID_CELLS
        preview_module.MAX_PREVIEW_ROWS = 16
        preview_module.MAX_PREVIEW_GRID_CELLS = 4
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            values = [
                (float(row), float(column), float(column % 2))
                for row in range(4)
                for column in range(40)
                ]
            metadata = {
                "result_count": len(values),
                "setpoint_shape": [4, 40],
                "measure_parameters": ["signal"],
                "sweep_parameters": ["slow_y", "fast_x"],
                }
            grids = []
            with (
                    patch.object(
                        preview_module,
                        "_spatial_mean_preview_grid",
                        side_effect=sqlite3.OperationalError("unsupported"),
                        ),
                    patch.object(preview_module, "log_exception") as logged,
                    patch.object(
                        preview_module,
                        "render_heatmap_grid_preview",
                        side_effect=lambda grid, size: np.array(grid, copy=True),
                        ),
                    ):
                for table_name, table_values in (
                        ("forward_results", values),
                        ("reverse_results", list(reversed(values))),
                        ):
                    cursor.execute(
                        f"CREATE TABLE {table_name} "
                        "(slow_y REAL, fast_x REAL, signal REAL)"
                        )
                    cursor.executemany(
                        f"INSERT INTO {table_name} VALUES (?, ?, ?)",
                        table_values,
                        )
                    preview = preview_module._preview_2d(
                        cursor,
                        table_name,
                        metadata,
                        "signal",
                        ["slow_y", "fast_x"],
                        size=4,
                        )
                    grids.append(preview["image"])
        finally:
            conn.close()
            preview_module.MAX_PREVIEW_ROWS = old_max_preview_rows
            preview_module.MAX_PREVIEW_GRID_CELLS = old_max_grid_cells

        self.assertEqual(logged.call_count, 2)
        for grid in grids:
            self.assertEqual(grid.shape, (4, 4))
            np.testing.assert_allclose(grid, np.full((4, 4), 0.5))
        np.testing.assert_array_equal(grids[0], grids[1])

    def test_sampled_2d_preview_bins_to_avoid_sparse_grid_artifacts(self):
        old_samples_per_cell = preview_module.PREVIEW_SAMPLES_PER_CELL
        preview_module.PREVIEW_SAMPLES_PER_CELL = 4
        try:
            grid = preview_module._binned_heatmap_grid(
                np.arange(20, dtype=float),
                np.zeros(20, dtype=float),
                np.arange(20, dtype=float),
                size=20,
                grid_shape=(20, 20),
                )
        finally:
            preview_module.PREVIEW_SAMPLES_PER_CELL = old_samples_per_cell

        self.assertLessEqual(grid.size, 5)
        self.assertTrue(np.isfinite(grid).all())

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
            QtCore.QEvent.Type.MouseButtonDblClick,
            QtCore.QPointF(10, 10),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            )
        qtw.QApplication.sendEvent(image, event)

        self.assertEqual(requested, ["dmm_v2"])

    def test_right_clicking_preview_can_request_export(self):
        old_exec = qtw.QMenu.exec
        captured_actions = []
        preview = PreviewTab(preview_size=100)
        requested = []
        preview.exportRequested.connect(requested.append)

        def capture_menu(menu, *_args, **_kwargs):
            captured_actions.extend(menu.actions())

        try:
            qtw.QMenu.exec = capture_menu
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
                QtGui.QContextMenuEvent.Reason.Mouse,
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
            qtw.QMenu.exec = old_exec

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
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QPointF(10, 10),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            )
        second_press = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QPointF(10, 10),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
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
