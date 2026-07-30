import unittest

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.tools.operation_registry import operation_specs_for
from qplot.windows._widgets.operations import (
    operations_options_1d,
    operations_options_2d,
)
from qplot.windows._widgets.toolbar import QDock_context


class OperationsPanelTestCase(unittest.TestCase):
    def _panel(self, panel_type):
        main = qtw.QMainWindow()
        main.oper_dock = QDock_context("Operations", main)
        return main, panel_type(main)

    def _option(self, widget, label):
        for index in range(widget.list_options.count()):
            item = widget.list_options.item(index)
            if item.label == label:
                return item
        self.fail(f"Operation option not found: {label}")

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

    def test_invalid_integer_operation_input_does_not_raise_from_callback(self):
        main, widget = self._panel(operations_options_2d)
        try:
            option = self._option(widget, "Fill Below")
            option.input.setChecked(True)
            operation = option.operation_row
            operation.input.setText("1.5")
            callback_errors = []
            callback_results = []

            def refresh_callback():
                try:
                    callback_results.append(widget.get_data())
                except Exception as error:
                    callback_errors.append(error)

            widget.apply_but.clicked.connect(refresh_callback)
            widget.apply_but.click()

            self.assertIsInstance(operation.input.validator(), QtGui.QIntValidator)
            self.assertFalse(operation.input.hasAcceptableInput())
            self.assertEqual(callback_errors, [])
            self.assertEqual(callback_results, [[]])
        finally:
            main.deleteLater()

    def test_float_operation_input_preserves_scientific_notation(self):
        main, widget = self._panel(operations_options_1d)
        try:
            option = self._option(widget, "Limit Maximum")
            option.input.setChecked(True)
            option.operation_row.input.setText("1e1")

            operations = widget.get_data()

            self.assertEqual(len(operations), 1)
            result = operations[0]({
                "x": np.array([0.0, 1.0]),
                "y": np.array([5.0, 15.0]),
                "z": None,
            })
            np.testing.assert_array_equal(result["y"], [5.0, 10.0])
        finally:
            main.deleteLater()
