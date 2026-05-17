import unittest

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.tools.operation_registry import operation_specs_for
from qplot.windows._widgets.operations import operations_options_1d
from qplot.windows._widgets.toolbar import QDock_context


class OperationsPanelTestCase(unittest.TestCase):
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

    def test_operation_registry_lists_common_and_plot_specific_options(self):
        names = [spec.name for spec in operation_specs_for("plot2d")]

        self.assertEqual(names[:2], ["Limit Maximum", "Limit Minimum"])
        self.assertIn("Subtract Row Mean", names)
        self.assertIn("Fill Below", names)


