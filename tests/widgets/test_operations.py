import unittest
from decimal import Decimal

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.tools.operation_registry import OperationValidationError, operation_specs_for
from qplot.windows._widgets.operations import (
    operations_options_1d,
    operations_options_2d,
    operations_widget,
    rowItem,
)
from qplot.windows._widgets.toolbar import QDock_context


class OperationsPanelTestCase(unittest.TestCase):
    def _panel(self, panel_type):
        main = qtw.QMainWindow()
        main.oper_dock = QDock_context("Data operations", main)
        return main, panel_type(main)

    def _option(self, widget, label):
        for index in range(widget.list_options.count()):
            item = widget.list_options.item(index)
            if item.label == label:
                return item
        raise self.failureException(f"Operation option not found: {label}")

    def test_operations_panel_layout_is_installed_once(self):
        messages = []

        def handler(_mode, _context, message):
            messages.append(message)

        main = qtw.QMainWindow()
        main.oper_dock = QDock_context("Data operations", main)

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

    def test_operations_options_reserve_space_for_overlay_scrollbar(self):
        main, widget = self._panel(operations_options_2d)
        try:
            scrollbar_extent = max(
                widget.list_options.verticalScrollBar().sizeHint().width(),
                widget.style().pixelMetric(
                    qtw.QStyle.PixelMetric.PM_ScrollBarExtent,
                    ),
                )

            self.assertGreaterEqual(
                widget.list_options.viewportMargins().right(),
                scrollbar_extent,
                )
        finally:
            main.deleteLater()

    def test_operations_widget_uses_inherited_plot_kind(self):
        class Plot2DWindow(qtw.QMainWindow):
            operation_kind = "plot2d"

            def __init__(self):
                super().__init__()
                self.oper_dock = QDock_context("Data operations", self)

        class TimedPlot2D(Plot2DWindow):
            pass

        window = TimedPlot2D()
        try:
            widget = operations_widget(window)

            self.assertIsInstance(widget, operations_options_2d)
        finally:
            window.deleteLater()

    def test_operation_registry_lists_common_and_plot_specific_options(self):
        names = [spec.name for spec in operation_specs_for("plot2d")]

        self.assertEqual(names[:2], ["Limit Maximum", "Limit Minimum"])
        self.assertIn("Subtract Row Mean", names)
        self.assertIn("Fill Below", names)

    def test_invalid_integer_operation_input_is_reported(self):
        main, widget = self._panel(operations_options_2d)
        try:
            option = self._option(widget, "Fill Below")
            option.input.setChecked(True)
            operation = option.operation_row
            operation.input.setText("1.5")
            self.assertIsInstance(operation.input.validator(), QtGui.QIntValidator)
            self.assertFalse(operation.input.hasAcceptableInput())
            with self.assertRaisesRegex(
                    OperationValidationError,
                    "Fill Below: enter a valid value",
                    ):
                widget.get_data()
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
            self.assertEqual(operations[0].name, "Limit Maximum")
            result = operations[0]({
                "x": np.array([0.0, 1.0]),
                "y": np.array([5.0, 15.0]),
                "z": None,
            })
            np.testing.assert_array_equal(result["y"], [5.0, 10.0])
        finally:
            main.deleteLater()

    def test_disabled_fill_discards_text_and_reuses_placeholder_default(self):
        main, widget = self._panel(operations_options_2d)
        try:
            option = self._option(widget, "Fill Below")
            row = option.operation_row
            option.input.setChecked(True)
            self.assertEqual(row.output(), "")
            self.assertEqual(row.validated_output(), 10)
            row.input.setText("23")
            self.assertEqual(row.validated_output(), 23)

            option.input.setChecked(False)
            self.assertEqual(widget.get_data(), [])
            option.input.setChecked(True)
            self.assertEqual(row.editor_value(), "")
            self.assertEqual(row.validated_output(), 10)
        finally:
            main.deleteLater()

    def test_missing_input_is_reported_without_changing_editor_text(self):
        main, widget = self._panel(operations_options_1d)
        try:
            option = self._option(widget, "Limit Maximum")
            option.input.setChecked(True)
            with self.assertRaisesRegex(
                OperationValidationError, "Limit Maximum: a value is required"
            ):
                widget.get_data()
            self.assertEqual(option.operation_row.editor_value(), "")
        finally:
            main.deleteLater()

    def test_compiled_operations_keep_each_parsed_value_after_editor_changes(self):
        main, widget = self._panel(operations_options_1d)
        try:
            maximum = self._option(widget, "Limit Maximum")
            minimum = self._option(widget, "Limit Minimum")
            maximum.input.setChecked(True)
            minimum.input.setChecked(True)
            maximum.operation_row.input.setText("2.25")
            minimum.operation_row.input.setText("1.125")
            operations = widget.get_data()
            maximum.operation_row.input.setText("100")
            minimum.operation_row.input.setText("-100")

            data = {"x": np.arange(3), "y": np.array([0., 2., 3.]), "z": None}
            for operation in operations:
                data.update(operation(data))
            np.testing.assert_array_equal(data["y"], [1.125, 2., 2.25])
        finally:
            main.deleteLater()

    def test_row_editors_keep_raw_output_and_reset_contracts(self):
        for input_type, default, edited, expected, cleared, resolved in (
            (bool, True, False, False, False, False),
            (
                Decimal, "1.25", "9007199254740993.125",
                Decimal("9007199254740993.125"), "", Decimal("1.25"),
            ),
            (["first", "second"], "first", "second", "second", "", "first"),
            (None, "", None, None, None, None),
        ):
            with self.subTest(input_type=input_type):
                row = rowItem("Custom", None, input_type, default)
                try:
                    row.restore_editor_value(edited)
                    self.assertEqual(row.output(), expected)
                    row.reset()
                    self.assertEqual(row.output(), cleared)
                    self.assertEqual(row.validated_output(), resolved)
                finally:
                    row.row_widget.deleteLater()

    def test_promotion_state_uses_keys_and_preserves_order_text_and_apply_generation(self):
        source_main, source = self._panel(operations_options_2d)
        target_main, target = self._panel(operations_options_2d)
        try:
            for label, text in (
                ("Fill Right", ""),
                ("Limit Maximum", "9007199254740993.125"),
                ("Subtract Row Mean", None),
                ("dz/dx", None),
            ):
                option = self._option(source, label)
                option.input.setChecked(True)
                if text is not None:
                    option.operation_row.input.setText(text)
                option.operation_row._label.setText("A changed display label")
            source._apply_generation = 7
            # Restoration must also replace pre-existing enabled/hidden order.
            old = self._option(target, "Limit Minimum")
            old.input.setChecked(True)
            old.operation_row.input.setText("-1e2")
            state = source.capture_state(transpose=True)
            target.restore_state(state)

            self.assertEqual(target.capture_state(), state)
            self.assertEqual(
                [entry.key for entry in state.entries],
                ["fill_below", "limit_maximum", "column_mean", "derivative_y"],
            )
            self.assertEqual(
                [operation.name for operation in target.get_data()],
                ["Fill Below", "Limit Maximum", "Subtract Column Mean", "dz/dy"],
            )
            self.assertEqual(self._option(target, "Limit Maximum").operation_row.input.text(),
                             "9007199254740993.125")
            self.assertEqual(self._option(target, "Fill Below").operation_row.input.text(), "")
            self.assertEqual(target._apply_generation, 7)
            self.assertFalse(self._option(target, "Limit Minimum").input.isChecked())
            self.assertEqual(target.capture_state(transpose=True), source.capture_state())

            source.hide_all()
            target.restore_state(source.capture_state())
            self.assertEqual(target.get_data(), [])
        finally:
            source_main.deleteLater()
            target_main.deleteLater()
