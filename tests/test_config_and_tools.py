import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.configuration.scripts import sysHandle, try_as_num
from qplot.configuration.themes import dark, light
from qplot.windows import main as main_window
from qplot.windows.plot1d import plot1d
from qplot.windows._window_controls import (
    add_confirmation_options,
    add_restore_defaults_option,
    )
from qplot.windows._widgets.operations import operations_options_1d
from qplot.windows._widgets.toolbar import QDock_context
from qplot.windows._widgets import treeWidgets
from qplot.windows._widgets.preview import (
    PREVIEW_SIZE,
    PreviewTab,
    generate_run_previews,
    render_heatmap_preview,
    render_sparkline_preview,
    )
from qplot.datahandling import readSQL
from qplot.tools.general import data2matrix
from qplot.tools.plot_tools import differentiate, pass_filter, subtract_mean


class TemporaryConfigTestCase(unittest.TestCase):
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


class SnapToTraceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qtw.QApplication.instance() or qtw.QApplication([])

    def test_nearest_trace_point_uses_plotted_data_point(self):
        widget = pg.GraphicsLayoutWidget()
        plot_item = widget.addPlot()
        line = plot_item.plot(x=[0.0, 1.0, 2.0], y=[0.0, 1.0, 4.0])
        window = plot1d.__new__(plot1d)
        window.plot = plot_item
        window.right_vb = None
        window.lines = {"main": line}

        scene_pos = plot_item.vb.mapViewToScene(QtCore.QPointF(2.1, 3.8))

        label, x_value, y_value, viewbox = window._nearest_trace_point(scene_pos)

        self.assertEqual(label, "main")
        self.assertEqual(x_value, 2.0)
        self.assertEqual(y_value, 4.0)
        self.assertIs(viewbox, plot_item.vb)

    def test_register_main_line_replaces_initial_empty_trace(self):
        line = object()
        window = plot1d.__new__(plot1d)
        window.label = "main"
        window.line = line
        window.lines = {"main": None}

        window._register_main_line()

        self.assertIs(window.lines["main"], line)


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
            "measure_parameters": ["dmm_v1"],
            "run_timestamp": 100.0,
            "completed_timestamp": None,
            "is_completed": False,
            "result_count": 25,
            "expected_results": 100,
            })

        self.assertIn("<table", tooltip)
        self.assertIn("<td>Sweep</td><td>&nbsp;</td><td>(dac_ch1, dac_ch2)</td>", tooltip)
        self.assertIn("<td>Measure</td><td>&nbsp;</td><td>(dmm_v1)</td>", tooltip)
        self.assertNotIn("<td>Incomplete</td>", tooltip)
        self.assertNotIn("<td>Time taken</td>", tooltip)

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
                ["ID", "Measurements", "Setpoints", "Started", "Complete", "Time taken", "Storage"]
                )
            self.assertIsInstance(
                run_list.itemDelegateForColumn(2),
                treeWidgets.EqualsAlignedDelegate
                )
            items = {
                run_list.topLevelItem(row).guid: run_list.topLevelItem(row)
                for row in range(run_list.topLevelItemCount())
                }

            self.assertEqual([item.guid for item in run_list.watching], ["unfinished-guid"])
            self.assertEqual(items["unfinished-guid"].text(1), "1")
            self.assertEqual(items["unfinished-guid"].text(2), r"10 = 10")
            self.assertEqual(items["unfinished-guid"].text(4), "10.0%")
            self.assertRegex(items["unfinished-guid"].text(5), r"^\d+\.\d s$")
            self.assertEqual(items["unfinished-guid"].text(6), "100 KB")
            self.assertEqual(items["finished-guid"].text(1), "2")
            self.assertEqual(items["finished-guid"].text(2), "1,000 = 10 × 100")
            self.assertEqual(items["finished-guid"].text(4), "✓")
            self.assertEqual(items["finished-guid"].text(5), "10.0 s")
            self.assertEqual(
                items["finished-guid"].textAlignment(0),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
            self.assertEqual(
                items["finished-guid"].textAlignment(2),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
            self.assertEqual(
                items["finished-guid"].textAlignment(4),
                QtCore.Qt.AlignCenter
                )
            self.assertEqual(
                items["finished-guid"].textAlignment(5),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
            self.assertEqual(
                items["finished-guid"].textAlignment(6),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
            self.assertIn("<td>Measure</td><td>&nbsp;</td><td>(y)</td>", items["unfinished-guid"].toolTip(0))
            self.assertIn("Complete", items["finished-guid"].toolTip(0))
        finally:
            treeWidgets.isfile = old_isfile


class RunStorageTestCase(unittest.TestCase):
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
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            conn = sqlite3.connect(database.name)
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
                "Time taken",
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
        self.assertEqual(widget.parameters.rowCount(), 2)
        self.assertEqual(widget.parameters.item(0, 3).text(), "-1")
        self.assertEqual(widget.parameters.item(0, 4).text(), "1")
        self.assertEqual(widget.parameters.item(0, 5).text(), "5")
        self.assertEqual(widget.parameters.item(0, 6).text(), "")
        self.assertEqual(widget.parameters.item(0, 7).text(), "dac")
        self.assertTrue(widget.parameters.item(0, 0).font().bold())
        self.assertFalse(widget.parameters.item(0, 0).font().italic())
        self.assertTrue(widget.parameters.item(0, 0).toolTip().startswith("Setpoint parameter"))
        self.assertEqual(widget.parameters.item(1, 0).text(), "dmm_v1")
        self.assertEqual(widget.parameters.item(1, 3).text(), "")
        self.assertEqual(widget.parameters.item(1, 4).text(), "")
        self.assertEqual(widget.parameters.item(1, 5).text(), "")
        self.assertEqual(widget.parameters.item(1, 6).text(), "")
        self.assertEqual(widget.parameters.item(1, 7).text(), "dmm")
        self.assertFalse(widget.parameters.item(1, 0).font().bold())
        self.assertTrue(widget.parameters.item(1, 0).font().italic())
        self.assertTrue(widget.parameters.item(1, 0).toolTip().startswith("Measured parameter"))
        self.assertEqual(
            widget.overview.item(2, 1).text(),
            "23.10 s\t(0d 0h 0m 23s; 0.115 s/point)"
            )
        self.assertLessEqual(len(widget.metadata.topLevelItem(0).text(1)), 180)
        self.assertEqual(
            widget.parameters.horizontalHeader().sectionResizeMode(7),
            qtw.QHeaderView.Stretch
            )
        widget.parameters.selectRow(1)
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
        widget.parameters.setCurrentCell(1, 0)
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
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            conn = sqlite3.connect(database.name)
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
                conn.close()

            previews = generate_run_previews(database.name, {
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

    def test_generate_run_previews_reads_1d_and_2d_sql_data(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            conn = sqlite3.connect(database.name)
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
                conn.close()

            previews = generate_run_previews(database.name, {
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
        self.assertTrue(all(preview["image"].width() == PREVIEW_SIZE for preview in previews))


if __name__ == "__main__":
    unittest.main()
