import unittest

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._commands import (
    command_spec,
    create_action,
    plot_measurement_command_spec,
    shortcut_help_html,
    toolbar_toggle_command_spec,
)


class CommandRegistryTestCase(unittest.TestCase):
    def test_create_action_applies_registered_metadata(self):
        window = qtw.QMainWindow()

        try:
            action = create_action("database.load", window)

            self.assertEqual(action.text(), "&Load Database...")
            self.assertEqual(action.shortcut().toString(), "Ctrl+L")
            self.assertEqual(action.statusTip(), "Load a QCoDeS database")
            self.assertEqual(
                action.shortcutContext(),
                QtCore.Qt.ShortcutContext.WindowShortcut,
            )
            self.assertIn(
                command_spec("database.load").shortcut_display_text(),
                action.toolTip(),
            )
        finally:
            window.deleteLater()

    def test_dynamic_measurement_command_uses_expected_number(self):
        spec = plot_measurement_command_spec(2)

        self.assertEqual(spec.text, "Plot Measurement 3 in Selected Run")
        self.assertEqual(spec.resolved_shortcuts()[0].toString(), "Ctrl+3")
        self.assertEqual(spec.status_tip, "Plot measurement 3 in the selected run")

    def test_toolbar_toggle_lookup_uses_registered_shortcuts(self):
        spec = toolbar_toggle_command_spec("Operations")

        self.assertIsNotNone(spec)
        self.assertEqual(spec.resolved_shortcuts()[0].toString(), "Ctrl+Alt+O")
        self.assertEqual(spec.status_tip, "Show or hide the operations dock")

    def test_operations_panel_has_one_canonical_shortcut(self):
        self.assertEqual(command_spec("plot.toggle_operations").resolved_shortcuts(), [])
        self.assertEqual(
            command_spec("toolbar.operations").resolved_shortcuts()[0].toString(),
            "Ctrl+Alt+O",
            )

    def test_shortcut_help_is_generated_from_registered_commands(self):
        html = shortcut_help_html()

        self.assertIn(command_spec("database.load").status_tip, html)
        self.assertIn(command_spec("plot.export").help_text, html)
        self.assertIn(command_spec("heatmap.horizontal_cut").status_tip, html)
        self.assertIn("Ctrl+1 to Ctrl+9", html)
