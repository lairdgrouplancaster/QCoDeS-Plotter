import unittest

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.configuration.themes import dark, light
from qplot.configuration.themes._base import ThemePalette, build_stylesheet


class ThemeStylesheetTestCase(unittest.TestCase):
    def test_stylesheet_builder_substitutes_theme_tokens(self):
        palette = ThemePalette(
            window_bg="#010101",
            panel_bg="#020202",
            panel_alt_bg="#030303",
            field_bg="#040404",
            field_alt_bg="#050505",
            button_bg="#060606",
            button_hover_bg="#070707",
            button_pressed_bg="#080808",
            text="#090909",
            text_strong="#101010",
            muted_text="#111111",
            disabled_text="#121212",
            selection_bg="#131313",
            selection_text="#141414",
            accent="#151515",
            accent_hover="#161616",
            accent_soft="#171717",
            accent_pressed="#181818",
            border="#191919",
            border_strong="#202020",
            menu_hover_bg="#212121",
            tab_bg="#222222",
            tab_selected_bg="#232323",
            tab_hover_bg="#242424",
            table_bg="#252525",
            table_alt_bg="#262626",
            table_hover_bg="#272727",
            header_bg="#282828",
            status_text="#292929",
            progress_text="#303030",
            splitter="#313131",
            scrollbar_bg="#323232",
            scrollbar_handle="#333333",
            scrollbar_handle_hover="#343434",
            danger="#353535",
            danger_bg="#363636",
            danger_pressed_bg="#373737",
        )

        stylesheet = build_stylesheet(palette)

        self.assertIn("background-color: #010101", stylesheet)
        self.assertIn("border: 1px solid #151515", stylesheet)
        self.assertNotIn("$", stylesheet)

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

    def test_light_and_dark_share_theme_surface(self):
        for theme in (light, dark):
            self.assertIsInstance(theme.main, str)
            self.assertGreater(len(theme.main), 1000)
            self.assertEqual(len(theme.colors), 6)
            self.assertTrue(hasattr(theme, "style_plotItem"))

