import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from PyQt5 import QtWidgets as qtw

import qplot.__main__ as qplot_main
from qplot import __version__
from qplot.configuration.config import config
from qplot.configuration.scripts import scripts, sysHandle
from qplot.configuration.themes import dark
from qplot.windows import main as main_window
from qplot.windows._run_controls import AUTO_PLOT_KEY


class TemporaryConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_default_path = config.default_path
        self.old_default_file = config.default_file

        config.default_path = str(Path(self.temp_dir.name) / ".qplot")
        config.default_file = str(Path(config.default_path) / config.config_file_name)

    def tearDown(self):
        config.default_path = self.old_default_path
        config.default_file = self.old_default_file
        self.temp_dir.cleanup()

    def test_config_update_writes_and_reloads_value(self):
        cfg = config()

        cfg.update("user_preference.theme", "dark")

        self.assertEqual(config().get("user_preference.theme"), "dark")
        self.assertIs(config().theme, dark)

    def test_config_accepts_extra_color_map_preferences(self):
        cfg = config()

        cfg.update("user_preference.bar_colour", "CET-L1")

        self.assertEqual(config().get("user_preference.bar_colour"), "CET-L1")

    def test_config_update_rejects_unknown_key(self):
        cfg = config()

        with self.assertRaises(KeyError):
            cfg.update("user_preference.missing", True)

    def test_config_cli_set_value_converts_values(self):
        with redirect_stdout(io.StringIO()):
            sysHandle("-set_value", "user_preference.theme", "dark")
            sysHandle("-set_value", "user_preference.confirm_close", "false")
            sysHandle("-set_value", "user_preference.confirm_close_all", "false")
            sysHandle("-set_value", "GUI.main_frame_size", "[900, 600]")
            sysHandle("-set_value", "GUI.preview_size", "300")

        cfg = config()
        self.assertEqual(cfg.get("user_preference.theme"), "dark")
        self.assertFalse(cfg.get("user_preference.confirm_close"))
        self.assertFalse(cfg.get("user_preference.confirm_close_all"))
        self.assertEqual(cfg.get("GUI.main_frame_size"), [900, 600])
        self.assertEqual(cfg.get("GUI.preview_size"), 300)

    def test_config_load_adds_new_defaults_to_existing_file(self):
        cfg = config()
        stored_config = cfg.config
        del stored_config["user_preference"]["confirm_close_all"]
        del stored_config["user_preference"]["auto_plot"]
        with open(config.default_file, "w") as fp:
            json.dump(stored_config, fp)

        reloaded = config()

        self.assertTrue(reloaded.get("user_preference.confirm_close_all"))
        self.assertFalse(reloaded.get(AUTO_PLOT_KEY))

    def test_config_repr_returns_readable_json(self):
        cfg = config()

        self.assertEqual(repr(cfg), str(cfg))
        self.assertIn('"user_preference"', repr(cfg))

    def test_invalid_config_is_backed_up_and_reset_without_prompt(self):
        cfg = config()
        cfg.update("user_preference.theme", "dark")

        invalid_config = cfg.config
        invalid_config["user_preference"]["theme"] = "missing-theme"
        with open(config.default_file, "w") as fp:
            json.dump(invalid_config, fp)

        reloaded = config()

        self.assertEqual(reloaded.get("user_preference.theme"), "light")
        self.assertTrue(os.path.isfile(reloaded.invalid_config_backup_file))
        with open(reloaded.invalid_config_backup_file) as fp:
            backup = json.load(fp)
        self.assertEqual(backup["user_preference"]["theme"], "missing-theme")

    def test_scripts_without_arguments_shows_command_info(self):
        old_argv = sys.argv
        sys.argv = ["qplot-cfg"]
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                scripts()
        finally:
            sys.argv = old_argv

        self.assertIn("Valid Commands", output.getvalue())

    def test_config_cli_version_prints_package_version(self):
        output = io.StringIO()
        with redirect_stdout(output):
            sysHandle("-version")

        self.assertEqual(output.getvalue().strip(), __version__)

    def test_database_path_from_arguments_finds_database_file_argument(self):
        self.assertEqual(
            qplot_main._database_path_from_arguments([
                "-style",
                "Fusion",
                "/tmp/example.db",
                "/tmp/notes.txt",
                ]),
            "/tmp/example.db",
            )
        self.assertIsNone(
            qplot_main._database_path_from_arguments(["-style", "Fusion", "notes.txt"])
            )

    def test_main_window_uses_configured_default_refresh_rate(self):
        cfg = config()
        cfg.update("user_preference.default_refresh_rate", 3.5)
        window = main_window.MainWindow()

        try:
            self.assertEqual(window.spinBox.value(), 3.5)
        finally:
            window.monitor.stop()
            window.deleteLater()

    def test_main_window_refresh_interval_changes_are_persistent(self):
        window = main_window.MainWindow()

        try:
            window.spinBox.setValue(2.5)

            self.assertEqual(config().get("user_preference.default_refresh_rate"), 2.5)
        finally:
            window.monitor.stop()
            window.deleteLater()

    def test_main_window_uses_configured_auto_plot(self):
        cfg = config()
        cfg.update(AUTO_PLOT_KEY, True)
        window = main_window.MainWindow()

        try:
            self.assertTrue(window.autoPlotBox.isChecked())
        finally:
            window.monitor.stop()
            window.deleteLater()

    def test_main_window_auto_plot_changes_are_persistent(self):
        window = main_window.MainWindow()

        try:
            window.autoPlotBox.setChecked(True)

            self.assertTrue(config().get(AUTO_PLOT_KEY))
        finally:
            window.monitor.stop()
            window.deleteLater()

    def test_main_window_loads_last_database_on_startup_when_available(self):
        cfg = config()
        calls = []
        old_load_database_path = main_window.MainWindow.load_database_path

        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            cfg.update("file.last_file_path", database.name)

            def load_database_path(window, filename):
                calls.append(filename)
                window.fileTextbox.setText(filename)
                return True

            main_window.MainWindow.load_database_path = load_database_path
            window = None
            try:
                window = main_window.MainWindow()
                qtw.QApplication.processEvents()
                self.assertEqual(calls, [os.path.abspath(database.name)])
            finally:
                main_window.MainWindow.load_database_path = old_load_database_path
                if window is not None:
                    window.monitor.stop()
                    window.deleteLater()

    def test_main_window_loads_startup_database_argument_before_last_database(self):
        cfg = config()
        calls = []
        old_load_database_path = main_window.MainWindow.load_database_path

        with (
            tempfile.NamedTemporaryFile(suffix=".db") as startup_database,
            tempfile.NamedTemporaryFile(suffix=".db") as last_database,
        ):
            cfg.update("file.last_file_path", last_database.name)

            def load_database_path(window, filename):
                calls.append(filename)
                window.fileTextbox.setText(filename)
                return True

            main_window.MainWindow.load_database_path = load_database_path
            window = None
            try:
                window = main_window.MainWindow(
                    startup_database_path=startup_database.name
                    )
                qtw.QApplication.processEvents()
                self.assertEqual(calls, [startup_database.name])
            finally:
                main_window.MainWindow.load_database_path = old_load_database_path
                if window is not None:
                    window.monitor.stop()
                    window.deleteLater()

    def test_main_window_ignores_missing_last_database_on_startup(self):
        cfg = config()
        missing_database = str(Path(self.temp_dir.name) / "missing.db")
        cfg.update("file.last_file_path", missing_database)
        calls = []
        old_load_database_path = main_window.MainWindow.load_database_path

        def load_database_path(window, filename):
            calls.append(filename)
            return True

        main_window.MainWindow.load_database_path = load_database_path
        window = None
        try:
            window = main_window.MainWindow()
            qtw.QApplication.processEvents()
            self.assertEqual(calls, [])
        finally:
            main_window.MainWindow.load_database_path = old_load_database_path
            if window is not None:
                window.monitor.stop()
                window.deleteLater()

    def test_main_window_has_database_info_button(self):
        window = main_window.MainWindow()

        try:
            self.assertEqual(
                window.databaseInfoButton.accessibleName(),
                "Show database information"
                )
        finally:
            window.monitor.stop()
            window.deleteLater()

    def test_default_refresh_rate_is_one_second(self):
        cfg = config()

        self.assertEqual(cfg.get("user_preference.default_refresh_rate"), 1)

    def test_default_auto_plot_is_disabled(self):
        cfg = config()

        self.assertFalse(cfg.get(AUTO_PLOT_KEY))

    def test_cloud_sync_timeout_default_is_two_minutes(self):
        cfg = config()

        self.assertEqual(cfg.get("runtime_settings.cloud_sync_timeout"), 120)

    def test_default_refresh_rate_allows_zero_to_disable_auto_refresh(self):
        cfg = config()

        cfg.update("user_preference.default_refresh_rate", 0.0)

        self.assertEqual(config().get("user_preference.default_refresh_rate"), 0.0)


