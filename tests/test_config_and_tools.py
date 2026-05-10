import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.configuration.scripts import sysHandle, try_as_num
from qplot.configuration.themes import dark, light
from qplot.windows import main as main_window
from qplot.windows._widgets.operations import operations_options_1d
from qplot.windows._widgets.toolbar import QDock_context
from qplot.windows._widgets import treeWidgets
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
            sysHandle("-set_value", "GUI.main_frame_size", "[900, 600]")

        cfg = config()
        self.assertEqual(cfg.get("user_preference.theme"), "dark")
        self.assertFalse(cfg.get("user_preference.confirm_close"))
        self.assertEqual(cfg.get("GUI.main_frame_size"), [900, 600])


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

            def get_parameters(self):
                return [
                    Param("dac_ch1", "Gate ch1", "V"),
                    Param("dmm_v1", "Gate v1", "V", ("dac_ch1",)),
                    ]

            def run_timestamp(self):
                return 1768129603

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
                         ["Overview", "Parameters", "Metadata", "Raw"])
        self.assertEqual(widget.parameters.rowCount(), 2)
        self.assertEqual(widget.parameters.item(0, 3).text(), "Setpoint")
        self.assertEqual(widget.parameters.item(1, 3).text(), "Measured")
        self.assertEqual(widget.parameters.item(1, 4).text(), "dac_ch1")
        self.assertEqual(widget.parameters.item(1, 5).text(), "-0.0048")
        self.assertLessEqual(len(widget.metadata.topLevelItem(0).text(1)), 180)
        self.assertEqual(
            widget.parameters.horizontalHeader().sectionResizeMode(7),
            qtw.QHeaderView.Stretch
            )
        widget.parameters.selectRow(1)
        widget.parameters.copySelection()
        self.assertEqual(
            qtw.QApplication.clipboard().text(),
            "dmm_v1\tGate v1\tV\tMeasured\tdac_ch1\t-0.0048\tdmm\t<Numbers -800<=v<=400>"
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


if __name__ == "__main__":
    unittest.main()
