import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.windows import main as main_window
from qplot.windows._database_actions import DatabaseActionsMixin
from qplot.windows._plot2d_colorbar_dialog import ColorbarScaleDialogMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._run_controls import AUTO_PLOT_KEY, RunControlsMixin
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    add_config_checkbox_action,
    ask_confirmation_with_dont_ask_again,
)
from qplot.windows.plot2d import plot2d


class TransactionalGuiTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_default_path = config.default_path
        self.old_default_file = config.default_file
        config.default_path = str(Path(self.temp_dir.name) / ".qplot")
        config.default_file = str(Path(config.default_path) / config.config_file_name)
        self.config = config()

    def tearDown(self):
        config.default_path = self.old_default_path
        config.default_file = self.old_default_file
        self.temp_dir.cleanup()

    def disk_value(self, key):
        return config().get(key)

    @staticmethod
    def failing_save(config_object):
        return patch.object(
            config_object,
            "save_config",
            side_effect=OSError("simulated config write failure"),
            )

    def test_refresh_interval_rolls_back_widget_and_exact_timer_state(self):
        class Harness:
            monitorIntervalChanged = RunControlsMixin.monitorIntervalChanged
            _save_refresh_interval = RunControlsMixin._save_refresh_interval
            _apply_refresh_interval = RunControlsMixin._apply_refresh_interval

            def __init__(self, config_object):
                self.config = config_object
                self.spinBox = qtw.QDoubleSpinBox()
                self.spinBox.setValue(1.0)
                self.monitor = QtCore.QTimer()
                self.errors = []
                self.empty_state_syncs = 0

            def show_error(self, *args):
                self.errors.append(args)

            def _sync_empty_state(self):
                self.empty_state_syncs += 1

        harness = Harness(self.config)
        observed_values = []
        harness.spinBox.valueChanged.connect(
            lambda value: harness.monitorIntervalChanged(value)
            )
        harness.spinBox.valueChanged.connect(observed_values.append)
        harness.monitor.start(1000)

        try:
            with self.failing_save(self.config):
                harness.spinBox.setValue(2.0)

            self.assertEqual(harness.spinBox.value(), 1.0)
            self.assertTrue(harness.monitor.isActive())
            self.assertEqual(harness.monitor.interval(), 1000)
            self.assertEqual(self.config.get("user_preference.default_refresh_rate"), 1.0)
            self.assertEqual(self.disk_value("user_preference.default_refresh_rate"), 1.0)
            self.assertEqual(observed_values, [2.0])
            self.assertEqual(len(harness.errors), 1)

            harness.spinBox.setValue(2.0)
            self.assertEqual(harness.monitor.interval(), 2000)
            self.assertTrue(harness.monitor.isActive())
            self.assertEqual(self.disk_value("user_preference.default_refresh_rate"), 2.0)

            with self.failing_save(self.config):
                harness.spinBox.setValue(0.0)
            self.assertEqual(harness.spinBox.value(), 2.0)
            self.assertTrue(harness.monitor.isActive())
            self.assertEqual(harness.monitor.interval(), 2000)
            self.assertEqual(len(harness.errors), 2)

            harness.spinBox.setValue(0.0)
            self.assertFalse(harness.monitor.isActive())
            inactive_interval = harness.monitor.interval()

            with self.failing_save(self.config):
                harness.spinBox.setValue(1.0)
            self.assertEqual(harness.spinBox.value(), 0.0)
            self.assertFalse(harness.monitor.isActive())
            self.assertEqual(harness.monitor.interval(), inactive_interval)
            self.assertEqual(self.disk_value("user_preference.default_refresh_rate"), 0.0)
            self.assertEqual(len(harness.errors), 3)
        finally:
            harness.monitor.stop()
            harness.spinBox.deleteLater()

    def test_auto_plot_rolls_back_without_runtime_action_and_retry_succeeds(self):
        class Harness:
            _auto_plot_changed = RunControlsMixin._auto_plot_changed

            def __init__(self, config_object):
                self.config = config_object
                self.autoPlotBox = qtw.QCheckBox()
                self.errors = []
                self.auto_plot_calls = 0

            def show_error(self, *args):
                self.errors.append(args)

            def _auto_plot_current_running_run(self):
                self.auto_plot_calls += 1

        harness = Harness(self.config)
        toggles = []
        harness.autoPlotBox.toggled.connect(
            lambda checked: harness._auto_plot_changed(checked)
            )
        harness.autoPlotBox.toggled.connect(toggles.append)

        try:
            with self.failing_save(self.config):
                harness.autoPlotBox.setChecked(True)

            self.assertFalse(harness.autoPlotBox.isChecked())
            self.assertFalse(self.config.get(AUTO_PLOT_KEY))
            self.assertFalse(self.disk_value(AUTO_PLOT_KEY))
            self.assertEqual(harness.auto_plot_calls, 0)
            self.assertEqual(toggles, [True])
            self.assertEqual(len(harness.errors), 1)

            harness.autoPlotBox.setChecked(True)
            self.assertTrue(harness.autoPlotBox.isChecked())
            self.assertTrue(self.config.get(AUTO_PLOT_KEY))
            self.assertTrue(self.disk_value(AUTO_PLOT_KEY))
            self.assertEqual(harness.auto_plot_calls, 1)
        finally:
            harness.autoPlotBox.deleteLater()

    def test_theme_and_preview_runtime_commit_only_after_persistence(self):
        class ThemeHarness:
            change_theme = main_window.MainWindow.change_theme

            def __init__(self, config_object):
                self.config = config_object
                self.windows = []
                self.styles = ["previous stylesheet"]
                self.statuses = []
                self.errors = []

            def setStyleSheet(self, stylesheet):
                self.styles.append(stylesheet)

            def show_status(self, *args):
                self.statuses.append(args)

            def show_error(self, *args):
                self.errors.append(args)

        theme_harness = ThemeHarness(self.config)
        group = QtGui.QActionGroup(None)
        light_action = group.addAction(QtGui.QAction("Light"))
        dark_action = group.addAction(QtGui.QAction("Dark"))
        for action in (light_action, dark_action):
            action.setCheckable(True)
        light_action.setChecked(True)
        theme_harness.themes = [light_action, dark_action]
        theme_calls = []
        dark_action.triggered.connect(
            lambda: (
                theme_calls.append("dark"),
                theme_harness.change_theme("dark", dark_action),
                )
            )

        with self.failing_save(self.config):
            dark_action.trigger()

        self.assertEqual(theme_calls, ["dark"])
        self.assertTrue(light_action.isChecked())
        self.assertFalse(dark_action.isChecked())
        self.assertEqual(theme_harness.styles, ["previous stylesheet"])
        self.assertEqual(self.config.get("user_preference.theme"), "light")
        self.assertEqual(self.disk_value("user_preference.theme"), "light")
        self.assertEqual(len(theme_harness.errors), 1)

        dark_action.trigger()
        self.assertTrue(dark_action.isChecked())
        self.assertEqual(self.disk_value("user_preference.theme"), "dark")
        self.assertEqual(theme_harness.styles[-1], self.config.theme.main)

        class InfoBox:
            def __init__(self):
                self.preview_size = 200

            def set_preview_size(self, value):
                self.preview_size = value

        class PreviewHarness:
            change_preview_size = main_window.MainWindow.change_preview_size
            _save_preview_size = main_window.MainWindow._save_preview_size
            _configured_preview_size = RunControlsMixin._configured_preview_size

            def __init__(self, config_object):
                self.config = config_object
                self.preview_size = 200
                self.infoBox = InfoBox()
                self.errors = []
                self.statuses = []

            def show_error(self, *args):
                self.errors.append(args)

            def show_status(self, *args):
                self.statuses.append(args)

            def _prioritize_preview_runs(self):
                pass

        preview_harness = PreviewHarness(self.config)
        preview_group = QtGui.QActionGroup(None)
        preview_200 = preview_group.addAction(QtGui.QAction("200 px"))
        preview_300 = preview_group.addAction(QtGui.QAction("300 px"))
        for action, value in ((preview_200, 200), (preview_300, 300)):
            action.setCheckable(True)
            action.setData(value)
        preview_200.setChecked(True)
        preview_harness.previewSizeActions = [preview_200, preview_300]
        preview_calls = []
        preview_300.triggered.connect(
            lambda: (
                preview_calls.append(300),
                preview_harness.change_preview_size(300),
                )
            )

        with self.failing_save(self.config):
            preview_300.trigger()

        self.assertEqual(preview_calls, [300])
        self.assertTrue(preview_200.isChecked())
        self.assertFalse(preview_300.isChecked())
        self.assertEqual(preview_harness.preview_size, 200)
        self.assertEqual(preview_harness.infoBox.preview_size, 200)
        self.assertEqual(self.config.get("GUI.preview_size"), 200)
        self.assertEqual(self.disk_value("GUI.preview_size"), 200)
        self.assertEqual(len(preview_harness.errors), 1)

        preview_300.trigger()
        self.assertEqual(preview_harness.preview_size, 300)
        self.assertEqual(preview_harness.infoBox.preview_size, 300)
        self.assertEqual(self.disk_value("GUI.preview_size"), 300)

    def test_confirmation_actions_rollback_without_recursion_or_duplicate_error(self):
        window = qtw.QMainWindow()
        window.config = self.config
        errors = []
        window.show_error = lambda *args: errors.append(args)
        menu = qtw.QMenu(window)
        action = add_config_checkbox_action(
            window,
            menu,
            "Confirm",
            CONFIRM_CLOSE_ALL_KEY,
            "Confirm closes",
            )
        toggles = []
        action.toggled.connect(toggles.append)

        try:
            with self.failing_save(self.config):
                action.setChecked(False)

            self.assertTrue(action.isChecked())
            self.assertTrue(self.config.get(CONFIRM_CLOSE_ALL_KEY))
            self.assertTrue(self.disk_value(CONFIRM_CLOSE_ALL_KEY))
            self.assertEqual(toggles, [False])
            self.assertEqual(len(errors), 1)

            action.setChecked(False)
            self.assertFalse(action.isChecked())
            self.assertFalse(self.disk_value(CONFIRM_CLOSE_ALL_KEY))

            self.config.update(CONFIRM_CLOSE_ALL_KEY, True)

            def accept_and_disable(box):
                box.checkBox().setChecked(True)
                return qtw.QMessageBox.StandardButton.Yes

            with (
                patch.object(qtw.QMessageBox, "exec", accept_and_disable),
                self.failing_save(self.config),
                ):
                reply = ask_confirmation_with_dont_ask_again(
                    window,
                    "Confirm",
                    "Continue?",
                    CONFIRM_CLOSE_ALL_KEY,
                    )

            self.assertEqual(reply, qtw.QMessageBox.StandardButton.Yes)
            self.assertTrue(self.config.get(CONFIRM_CLOSE_ALL_KEY))
            self.assertTrue(self.disk_value(CONFIRM_CLOSE_ALL_KEY))
            self.assertEqual(len(errors), 2)
        finally:
            window.deleteLater()

    def test_mouse_mode_rolls_back_menu_and_viewbox_then_retries(self):
        class MouseHarness(qtw.QMainWindow):
            change_mouse_mode = plotWidget.change_mouse_mode
            apply_mouse_mode_preference = plotWidget.apply_mouse_mode_preference
            _configured_mouse_mode = plotWidget._configured_mouse_mode
            _connect_mouse_mode_menu_to_preferences = (
                plotWidget._connect_mouse_mode_menu_to_preferences
                )

            def __init__(self, config_object):
                super().__init__()
                self.config = config_object
                self.vb = pg.ViewBox()
                self.vbMenu = self.vb.menu
                self.mouseModeAction = next(
                    action for action in self.vb.menu.actions()
                    if action.text() == "Mouse Mode"
                    )
                self.errors = []

            def show_error(self, *args):
                self.errors.append(args)

        window = MouseHarness(self.config)
        window.apply_mouse_mode_preference()
        window._connect_mouse_mode_menu_to_preferences()
        pan_action, rect_action = window.vbMenu.mouseModes
        triggers = []
        rect_action.triggered.connect(lambda: triggers.append("rect"))

        try:
            with self.failing_save(self.config):
                rect_action.trigger()

            self.assertEqual(triggers, ["rect"])
            self.assertTrue(pan_action.isChecked())
            self.assertFalse(rect_action.isChecked())
            self.assertEqual(
                window.vb.getState(copy=False)["mouseMode"],
                pg.ViewBox.PanMode,
                )
            self.assertEqual(self.config.get("user_preference.mouse_mode"), "pan")
            self.assertEqual(self.disk_value("user_preference.mouse_mode"), "pan")
            self.assertEqual(len(window.errors), 1)

            rect_action.trigger()
            self.assertTrue(rect_action.isChecked())
            self.assertEqual(
                window.vb.getState(copy=False)["mouseMode"],
                pg.ViewBox.RectMode,
                )
            self.assertEqual(self.disk_value("user_preference.mouse_mode"), "rect")
        finally:
            window.vb.deleteLater()
            window.deleteLater()

    def test_color_map_filter_and_selection_roll_back_then_retry(self):
        class FilterHarness(qtw.QMainWindow):
            _set_colorbar_filter_setting = (
                ColorbarScaleDialogMixin._set_colorbar_filter_setting
                )
            _colorbar_include_local_changed = (
                ColorbarScaleDialogMixin._colorbar_include_local_changed
                )
            _sync_colorbar_filter_controls = (
                ColorbarScaleDialogMixin._sync_colorbar_filter_controls
                )

            def __init__(self, config_object):
                super().__init__()
                self.config = config_object
                self.colorbar_include_cet_check = qtw.QCheckBox()
                self.colorbar_include_matplotlib_check = qtw.QCheckBox()
                self.colorbar_include_local_check = qtw.QCheckBox()
                self.colorbar_include_custom_check = qtw.QCheckBox()
                self.colorbar_cet_subtype_checks = {}
                self.colorbar_matplotlib_subtype_checks = {}
                for widget in (
                        self.colorbar_include_cet_check,
                        self.colorbar_include_matplotlib_check,
                        self.colorbar_include_local_check,
                        self.colorbar_include_custom_check,
                        ):
                    widget.setChecked(True)
                self.errors = []
                self.rebuilds = 0

            def show_error(self, *args):
                self.errors.append(args)

            def _populate_colorbar_colormap_table(self):
                self.rebuilds += 1

            def _sync_colorbar_scale_controls(self):
                self._sync_colorbar_filter_controls()

        window = FilterHarness(self.config)
        toggles = []
        window.colorbar_include_local_check.toggled.connect(
            window._colorbar_include_local_changed
            )
        window.colorbar_include_local_check.toggled.connect(toggles.append)

        try:
            with self.failing_save(self.config):
                window.colorbar_include_local_check.setChecked(False)

            self.assertTrue(window.colorbar_include_local_check.isChecked())
            self.assertTrue(
                self.config.get("user_preference.bar_colour_include_local")
                )
            self.assertTrue(
                self.disk_value("user_preference.bar_colour_include_local")
                )
            self.assertEqual(toggles, [False])
            self.assertEqual(window.rebuilds, 0)
            self.assertEqual(len(window.errors), 1)

            window.colorbar_include_local_check.setChecked(False)
            self.assertFalse(window.colorbar_include_local_check.isChecked())
            self.assertFalse(
                self.disk_value("user_preference.bar_colour_include_local")
                )
            self.assertEqual(window.rebuilds, 1)
        finally:
            window.deleteLater()

        class Colorbar:
            def __init__(self):
                self.color_map = "previous color map"

            def setColorMap(self, color_map):
                self.color_map = color_map

        plot = plot2d.__new__(plot2d)
        qtw.QMainWindow.__init__(plot)
        plot.config = self.config
        plot.bar = Colorbar()
        plot._colorbar_colormap_name = "viridis"
        plot.colorbar_colormap_table = None
        plot_errors = []
        plot.show_error = lambda *args: plot_errors.append(args)

        try:
            with self.failing_save(self.config):
                self.assertFalse(plot.setColorbarColorMap("Purples"))

            self.assertEqual(plot._colorbar_colormap_name, "viridis")
            self.assertEqual(plot.bar.color_map, "previous color map")
            self.assertEqual(self.config.get("user_preference.bar_colour"), "viridis")
            self.assertEqual(self.disk_value("user_preference.bar_colour"), "viridis")
            self.assertEqual(len(plot_errors), 1)

            self.assertTrue(plot.setColorbarColorMap("Purples"))
            self.assertEqual(plot._colorbar_colormap_name, "Purples")
            self.assertIsInstance(plot.bar.color_map, pg.ColorMap)
            self.assertEqual(self.disk_value("user_preference.bar_colour"), "Purples")
        finally:
            plot.deleteLater()

    def test_default_recent_and_last_database_paths_are_atomic(self):
        class Harness:
            change_default_file = DatabaseActionsMixin.change_default_file
            recent_database_paths = DatabaseActionsMixin.recent_database_paths
            remember_recent_database = DatabaseActionsMixin.remember_recent_database
            remember_loaded_database = DatabaseActionsMixin.remember_loaded_database

            def __init__(self, config_object):
                self.config = config_object
                self.errors = []
                self.statuses = []
                self.menu_refreshes = 0

            def show_error(self, *args):
                self.errors.append(args)

            def show_status(self, *args):
                self.statuses.append(args)

            def refresh_recent_database_menu(self):
                self.menu_refreshes += 1

        harness = Harness(self.config)
        folder = self.temp_dir.name

        with (
            patch.object(qtw.QFileDialog, "getExistingDirectory", return_value=folder),
            self.failing_save(self.config),
            ):
            self.assertFalse(harness.change_default_file())

        self.assertEqual(self.config.get("file.default_load_path"), "")
        self.assertEqual(self.disk_value("file.default_load_path"), "")
        self.assertEqual(len(harness.errors), 1)

        with patch.object(qtw.QFileDialog, "getExistingDirectory", return_value=folder):
            self.assertTrue(harness.change_default_file())
        self.assertEqual(self.disk_value("file.default_load_path"), folder)

        recent_path = os.path.abspath("recent.db")
        with self.failing_save(self.config):
            self.assertFalse(harness.remember_recent_database(recent_path))
        self.assertEqual(self.config.get("file.recent_file_paths"), [])
        self.assertEqual(self.disk_value("file.recent_file_paths"), [])
        self.assertEqual(harness.menu_refreshes, 0)
        self.assertEqual(len(harness.errors), 2)

        self.assertTrue(harness.remember_recent_database(recent_path))
        self.assertEqual(self.disk_value("file.recent_file_paths"), [recent_path])
        self.assertEqual(harness.menu_refreshes, 1)

        previous_last = os.path.abspath("previous.db")
        self.config.update_many({
            "file.last_file_path": previous_last,
            "file.recent_file_paths": [previous_last, recent_path],
            })
        new_path = os.path.abspath("new.db")
        with (
            patch.object(self.config, "update_many", wraps=self.config.update_many) as update,
            self.failing_save(self.config),
            ):
            self.assertFalse(harness.remember_loaded_database(new_path))
            self.assertEqual(update.call_count, 1)
            self.assertEqual(
                set(update.call_args.args[0]),
                {"file.last_file_path", "file.recent_file_paths"},
                )

        self.assertEqual(self.config.get("file.last_file_path"), previous_last)
        self.assertEqual(
            self.config.get("file.recent_file_paths"),
            [previous_last, recent_path],
            )
        self.assertEqual(self.disk_value("file.last_file_path"), previous_last)
        self.assertEqual(len(harness.errors), 3)

        self.assertTrue(harness.remember_loaded_database(new_path))
        self.assertEqual(self.disk_value("file.last_file_path"), new_path)
        self.assertEqual(
            self.disk_value("file.recent_file_paths"),
            [new_path, previous_last, recent_path],
            )

    def test_gui_reset_waits_for_persistence_and_can_retry(self):
        self.config.update("user_preference.theme", "dark")

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self, config_object):
                self.config = config_object
                self.errors = []
                self.closed_plots = 0
                self.applied = 0
                self.closed_database = 0
                self.statuses = []

            def show_error(self, *args):
                self.errors.append(args)

            def show_status(self, *args):
                self.statuses.append(args)

            def close_plot_windows(self, **_kwargs):
                self.closed_plots += 1

            def apply_current_settings(self):
                self.applied += 1

            def close_database(self, **_kwargs):
                self.closed_database += 1

        harness = Harness(self.config)
        with (
            patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.Yes,
                ),
            self.failing_save(self.config),
            ):
            self.assertFalse(harness.restore_default_settings())

        self.assertEqual(self.config.get("user_preference.theme"), "dark")
        self.assertEqual(self.disk_value("user_preference.theme"), "dark")
        self.assertEqual(harness.closed_plots, 0)
        self.assertEqual(harness.applied, 0)
        self.assertEqual(harness.closed_database, 0)
        self.assertEqual(len(harness.errors), 1)

        with patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.Yes,
                ):
            self.assertTrue(harness.restore_default_settings())

        self.assertEqual(self.disk_value("user_preference.theme"), "light")
        self.assertEqual(harness.closed_plots, 1)
        self.assertEqual(harness.applied, 1)
        self.assertEqual(harness.closed_database, 1)

    def test_preferences_failure_rolls_back_widgets_and_success_still_applies(self):
        dialog = PreferencesDialog(self.config)
        notifications = []
        applied = []
        dialog.preferencesApplied.connect(lambda: applied.append(True))
        dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("dark"))
        dialog.previewSizeSpin.setValue(300)

        try:
            with (
                patch.object(
                    qtw.QMessageBox,
                    "exec",
                    lambda box: notifications.append(box.text()) or 0,
                    ),
                self.failing_save(self.config),
                ):
                self.assertFalse(dialog.apply_preferences())

            self.assertEqual(dialog.themeCombo.currentData(), "light")
            self.assertEqual(dialog.previewSizeSpin.value(), 200)
            self.assertEqual(self.config.get("user_preference.theme"), "light")
            self.assertEqual(self.disk_value("GUI.preview_size"), 200)
            self.assertEqual(applied, [])
            self.assertEqual(len(notifications), 1)
            self.assertIn("previous setting is still active", notifications[0])

            dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("dark"))
            dialog.previewSizeSpin.setValue(300)
            self.assertTrue(dialog.apply_preferences())
            self.assertEqual(self.disk_value("user_preference.theme"), "dark")
            self.assertEqual(self.disk_value("GUI.preview_size"), 300)
            self.assertEqual(applied, [True])
        finally:
            dialog.deleteLater()
