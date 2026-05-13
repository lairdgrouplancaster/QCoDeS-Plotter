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


