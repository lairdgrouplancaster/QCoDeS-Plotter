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


