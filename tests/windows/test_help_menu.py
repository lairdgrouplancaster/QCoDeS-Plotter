import unittest

from PyQt6 import QtWidgets as qtw

from qplot.windows._commands import command_spec
from qplot.windows._help import (
    add_help_menu,
    copy_diagnostic_log_path,
    show_about,
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
            self.assertEqual(actions["aboutQplotAction"].text(), "&About qPlot")
            self.assertEqual(
                actions["aboutQplotAction"].menuRole(),
                actions["aboutQplotAction"].MenuRole.NoRole,
            )
        finally:
            window.deleteLater()

    def test_about_action_shows_version_and_development_provenance(self):
        from unittest.mock import patch

        window = qtw.QMainWindow()
        try:
            window.show()
            window.activateWindow()
            qtw.QApplication.processEvents()
            with patch("qplot.windows._help.version_info", return_value={
                "version": "2.0.0", "commit": "a" * 40,
                "released": False, "dirty": True, "pull_request": "123",
            }):
                menu = add_help_menu(window)
                next(a for a in menu.actions() if a.objectName() == "aboutQplotAction").trigger()
            dialog = window._help_dialogs[-1]
            text = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser").toPlainText()
            self.assertEqual(dialog.windowTitle(), "About qPlot")
            self.assertIn("Version: 2.0.0", text)
            self.assertIn("Unreleased development build (local changes)", text)
            self.assertIn("a" * 40, text)
            self.assertIn("Pull request: #123", text)
            dialog.close()
        finally:
            window.close()
            window.deleteLater()

    def test_each_help_menu_has_one_about_action_after_closing_another_window(self):
        from PyQt6 import sip

        first = qtw.QMainWindow()
        second = qtw.QMainWindow()
        try:
            menus = [add_help_menu(window) for window in (first, second)]
            actions = [
                next(a for a in menu.actions() if a.objectName() == "aboutQplotAction")
                for menu in menus
            ]
            for menu, action, window in zip(menus, actions, (first, second), strict=True):
                self.assertEqual(
                    sum(a.objectName() == "aboutQplotAction" for a in menu.actions()), 1,
                )
                self.assertIs(action.parent(), window)
                self.assertEqual(action.menuRole(), action.MenuRole.NoRole)
            sip.delete(first)
            second.show()
            second.activateWindow()
            qtw.QApplication.processEvents()
            actions[1].trigger()
            dialog = second._help_dialogs[-1]
            self.assertEqual(dialog.windowTitle(), "About qPlot")
            self.assertIs(dialog.parent(), second)
            dialog.close()
        finally:
            if not sip.isdeleted(first):
                first.deleteLater()
            second.close()
            second.deleteLater()

    def test_about_without_provenance_still_shows_release_number(self):
        from unittest.mock import patch

        window = qtw.QMainWindow()
        try:
            with patch("qplot.windows._help.version_info", return_value={"version": "2.0.0"}):
                dialog = show_about(window)
            text = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser").toPlainText()
            self.assertIn("Version: 2.0.0", text)
            self.assertNotIn("Unreleased", text)
            self.assertNotIn("Commit:", text)
            dialog.close()
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
            self.assertIn(command_spec("database.load").shortcut_display_text(), text)
            self.assertIn(command_spec("plot.export").shortcut_display_text(), text)
            self.assertIn(command_spec("plot.print").status_tip, text)
            self.assertIn(command_spec("plot.print").help_shortcut, text)
            self.assertIn("H", text)
            self.assertIn(dialog, window._help_dialogs)
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            window.deleteLater()
