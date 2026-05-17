import unittest

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.configuration.themes import dark, light


class ThemeStylesheetTestCase(unittest.TestCase):
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


