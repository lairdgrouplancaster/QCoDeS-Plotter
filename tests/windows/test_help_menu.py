import unittest

from PyQt5 import QtWidgets as qtw

from qplot.windows._help import (
    add_help_menu,
    copy_diagnostic_log_path,
    show_keyboard_shortcuts,
    show_quick_start,
    )


class HelpMenuTestCase(unittest.TestCase):
    def test_help_menu_adds_quick_start_and_shortcuts_actions(self):
        window = qtw.QMainWindow()

        try:
            menu = add_help_menu(window)
            actions = {
                action.objectName(): action
                for action in menu.actions()
                if action.objectName()
                }

            self.assertEqual(menu.title(), "&Help")
            self.assertEqual(actions["quickStartHelpAction"].text(), "&Quick Start")
            self.assertEqual(
                actions["quickStartHelpAction"].shortcut().toString(),
                "F1",
                )
            self.assertEqual(
                actions["keyboardShortcutsHelpAction"].text(),
                "&Keyboard Shortcuts",
                )
            self.assertEqual(
                actions["copyDiagnosticLogPathAction"].text(),
                "Copy &Diagnostic Log Path",
                )
        finally:
            window.deleteLater()

    def test_copy_diagnostic_log_path_copies_path_and_reports_status(self):
        class Window(qtw.QMainWindow):
            def __init__(self):
                super().__init__()
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        window = Window()

        try:
            path = copy_diagnostic_log_path(window)
            self.assertEqual(qtw.QApplication.clipboard().text(), path)
            self.assertIn("qplot.log", path)
            self.assertEqual(window.status_messages[-1][1], 5000)
        finally:
            window.deleteLater()

    def test_quick_start_dialog_contains_basic_workflow(self):
        window = qtw.QMainWindow()
        dialog = None

        try:
            dialog = show_quick_start(window)
            browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")

            self.assertEqual(dialog.objectName(), "qplotQuickStartDialog")
            self.assertIn("Load a database", browser.toPlainText())
            self.assertIn("Open a plot", browser.toPlainText())
            self.assertIn(dialog, window._help_dialogs)
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            window.deleteLater()

    def test_keyboard_shortcuts_dialog_contains_general_and_plot_shortcuts(self):
        window = qtw.QMainWindow()
        dialog = None

        try:
            dialog = show_keyboard_shortcuts(window)
            browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")
            text = browser.toPlainText()

            self.assertEqual(dialog.objectName(), "qplotKeyboardShortcutsDialog")
            self.assertIn("Ctrl+L", text)
            self.assertIn("Ctrl+E", text)
            self.assertIn("H", text)
            self.assertIn(dialog, window._help_dialogs)
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            window.deleteLater()
