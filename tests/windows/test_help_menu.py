import re
import sys
import unittest
from html import unescape
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

import qplot
from qplot.windows._commands import command_spec
from qplot.windows._help import (
    add_help_menu,
    copy_diagnostic_log_path,
    show_about,
    show_keyboard_shortcuts,
    show_quick_start,
)


@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.parametrize("opened", [False, True])
def test_about_opens_running_locations_with_literal_paths(
    qapplication, monkeypatch, tmp_path, editable, opened,
):
    environment = tmp_path / "Environment & measurements #1 %20" / ".venv"
    package = (
        tmp_path / ("Source 測定 " + "long" * 40) / "src" / "qplot"
        if editable else environment / "Lib" / "site-packages" / "qplot"
    )
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(qplot, "__file__", str(package / "__init__.py"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "another-environment"))
    monkeypatch.setattr("qplot.windows._help.version_info", lambda: {"version": "2.0"})
    open_url = Mock(return_value=opened)
    warning = Mock()
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(qtw.QMessageBox, "warning", warning)
    dialog = show_about()
    try:
        browser = dialog.findChild(qtw.QTextBrowser, "aboutTechnicalDetails")
        assert not browser.isVisible()
        dialog.findChild(qtw.QToolButton).click()
        assert browser.isVisible()
        original = browser.toPlainText()
        copy = dialog.findChild(qtw.QPushButton, "aboutCopyDetails")
        assert copy.isVisible()
        copy.click()
        assert qapplication.clipboard().text() == original
        assert str(environment) in original
        assert str(package) in original
        assert "another-environment" not in original
        qapplication.processEvents()
        assert browser.horizontalScrollBar().maximum() == 0
        urls = [QtCore.QUrl(unescape(href))
                for href in re.findall(r'href="([^"]+)"', browser.toHtml())]
        folders = [url for url in urls if url.isLocalFile()]
        assert [Path(url.toLocalFile()) for url in folders] == [environment, package]
        assert not browser.openLinks()
        assert not browser.openExternalLinks()
        for url in urls:
            open_url.reset_mock()
            warning.reset_mock()
            browser.anchorClicked.emit(url)
            open_url.assert_called_once_with(url)
            assert browser.toPlainText() == original
            if url.isLocalFile() and not opened:
                warning.assert_called_once()
                assert url.toLocalFile() in warning.call_args.args[2]
            else:
                warning.assert_not_called()
    finally:
        sip.delete(dialog)


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
            self.assertEqual(actions["quickStartHelpAction"].text(), "&Quick start")
            self.assertEqual(
                actions["quickStartHelpAction"].shortcut().toString(),
                "F1",
                )
            self.assertEqual(
                actions["keyboardShortcutsHelpAction"].text(),
                "&Keyboard and mouse controls",
                )
            self.assertEqual(
                actions["copyDiagnosticLogPathAction"].text(),
                "Copy path to &diagnostic log",
                )
            self.assertIn("Show diagnostic log", actions["showDiagnosticLogAction"].text())
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
                "commit_date": "2026-11-14T12:42:15+02:00",
                "released": False, "dirty": True, "pull_request": "123",
            }):
                menu = add_help_menu(window)
                next(a for a in menu.actions() if a.objectName() == "aboutQplotAction").trigger()
            dialog = window._help_dialogs[-1]
            text = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser").toPlainText()
            self.assertEqual(dialog.windowTitle(), "About qPlot")
            self.assertIn("Version: 2.0.0", text)
            self.assertNotIn("Commit:", text)
            text = dialog.findChild(qtw.QTextBrowser, "aboutTechnicalDetails").toPlainText()
            self.assertIn("Unreleased development build (local changes)", text)
            self.assertIn("a" * 40, text)
            self.assertIn("Committed: 14 November 2026, 10:42:15 UTC", text)
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
            self.assertNotIn("Committed:", text)
            dialog.close()
        finally:
            window.deleteLater()

    def test_about_omits_unavailable_or_invalid_commit_date(self):
        from unittest.mock import patch

        window = qtw.QMainWindow()
        try:
            for date in (None, "invalid", "2026-11-14T10:42:15", ""):
                with self.subTest(date=date), patch("qplot.windows._help.version_info", return_value={
                    "version": "2.0.0", "commit": "a" * 40, "commit_date": date,
                }):
                    dialog = show_about(window)
                text = dialog.findChild(qtw.QTextBrowser, "aboutTechnicalDetails").toPlainText()
                self.assertIn("Commit: " + "a" * 40, text)
                self.assertNotIn("Committed:", text)
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

    def test_show_diagnostic_log_action_reveals_log_and_reports_failure(self):
        from unittest.mock import Mock, patch

        window = qtw.QMainWindow()
        window.show_status = Mock()
        window.show_error = Mock()
        try:
            menu = add_help_menu(window)
            action = next(a for a in menu.actions() if a.objectName() == "showDiagnosticLogAction")
            for opened in (True, False):
                with self.subTest(opened=opened), patch(
                    "qplot.windows._file_manager.reveal_file_in_file_manager",
                    return_value=opened,
                ) as reveal:
                    window.show_status.reset_mock()
                    window.show_error.reset_mock()
                    action.trigger()
                    path = reveal.call_args.args[0]
                    self.assertTrue(path.endswith("qplot.log"))
                    reveal.assert_called_once_with(path)
                    if opened:
                        window.show_status.assert_called_once()
                        window.show_error.assert_not_called()
                    else:
                        window.show_status.assert_not_called()
                        window.show_error.assert_called_once_with(
                            "Show diagnostic log failed",
                            "Could not open the diagnostic log location in the file manager.",
                            path,
                        )
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
            menu = add_help_menu(window)
            next(
                action for action in menu.actions()
                if action.objectName() == "keyboardShortcutsHelpAction"
            ).trigger()
            dialog = window._help_dialogs[-1]
            browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")
            text = browser.toPlainText()

            self.assertEqual(dialog.objectName(), "qplotKeyboardShortcutsDialog")
            self.assertIn(command_spec("database.load").shortcut_display_text(), text)
            self.assertIn(command_spec("plot.export").shortcut_display_text(), text)
            self.assertIn(command_spec("plot.print").status_tip, text)
            self.assertIn(command_spec("plot.print").shortcut_display_text(), text)
            self.assertIn("H", text)
            self.assertIn("Main and All Plot Windows", text)
            self.assertIn("Main Window — Run Details Focused", text)
            self.assertIn("Main Window — Measurement Preview Focused", text)
            self.assertIn("All Plot Windows", text)
            self.assertIn(command_spec("plot.snap_to_trace").status_tip, text)
            self.assertIn("2D Heatmap Windows", text)
            self.assertIn("Escape", text)
            self.assertIn("Return / Enter", text)
            from PyQt6 import QtGui

            qtw.QApplication.processEvents()
            for frame in browser.document().rootFrame().childFrames():
                if isinstance(frame, QtGui.QTextTable):
                    for row in range(frame.rows()):
                        block = frame.cellAt(row, 0).firstCursorPosition().block()
                        self.assertEqual(block.layout().lineCount(), 1, block.text())
            self.assertIn(dialog, window._help_dialogs)
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            window.deleteLater()


@pytest.mark.parametrize(("plot_kind", "expected_section"), [
    (None, "Main Window"), ("plot2d", "2D Heatmap Windows"), ("sweeper", "Cut Windows"),
])
def test_shortcut_help_prioritises_window_and_filters(qapplication, plot_kind, expected_section):
    window = qtw.QMainWindow()
    window.operation_kind = plot_kind
    dialog = show_keyboard_shortcuts(window)
    try:
        search = dialog.findChild(qtw.QLineEdit, "shortcutSearch")
        scope = dialog.findChild(qtw.QComboBox)
        browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")
        headings = browser.toPlainText().splitlines()
        assert headings[1] == expected_section
        scope.setCurrentText("This window")
        headings = browser.toPlainText().splitlines()
        assert ("Main Window" in headings) == (plot_kind is None)
        assert ("Cut Windows" in headings) == (plot_kind == "sweeper")
        assert ("2D Heatmap Windows" in headings) == (plot_kind == "plot2d")
        scope.setCurrentText("All windows")
        search.setText("Horizontal CUT")
        assert "Open a horizontal cut" in browser.toPlainText()
        assert "Open a vertical cut" not in browser.toPlainText()
        search.setText("unmatched search")
        assert "No matching controls" in browser.toPlainText()
        search.clear()
        assert "Open a vertical cut" in browser.toPlainText()
    finally:
        sip.delete(dialog)
        sip.delete(window)


def test_reopening_shortcut_help_restores_the_owning_windows_search(qapplication):
    plot = qtw.QMainWindow()
    other = qtw.QMainWindow()
    plot.operation_kind = "plot2d"
    try:
        dialog = show_keyboard_shortcuts(plot)
        search = dialog.findChild(qtw.QLineEdit, "shortcutSearch")
        scope = dialog.findChild(qtw.QComboBox)
        search.setText("zoom")
        scope.setCurrentText("This window")
        separate = show_keyboard_shortcuts(other)
        assert separate is not dialog
        assert separate.findChild(qtw.QLineEdit, "shortcutSearch").text() == ""

        dialog.showMinimized()
        qapplication.processEvents()
        assert show_keyboard_shortcuts(plot) is dialog
        assert not dialog.isMinimized()
        assert search.text() == "zoom"
        assert scope.currentText() == "This window"

        dialog.close()
        fresh = show_keyboard_shortcuts(plot)
        assert fresh is not dialog
        assert fresh.findChild(qtw.QLineEdit, "shortcutSearch").text() == ""
        assert show_keyboard_shortcuts(other) is separate
    finally:
        sip.delete(other)
        sip.delete(plot)
