import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.windows import main as main_window
from qplot.windows._database_actions import DatabaseActionsMixin
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._run_controls import AUTO_PLOT_KEY, RunControlsMixin
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    add_config_checkbox_action,
    ask_confirmation_with_dont_ask_again,
)
from qplot.windows.plot2d import plot2d
from tests.support.windows.plot_integration import build_line_database, wait_for
from tests.support.windows.typed_run_keyboard_launch import (
    typed_run_window as typed_run_window,
)


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
                # Refresh settings may arm the timer only for an accepted
                # database instance; the persistence test supplies one.
                self._loaded_database_instance = type(
                    "CommittedInstance",
                    (),
                    {"identity": (1, 1)},
                )()
                self._database_load_generation = 0
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
            _apply_preview_size = main_window.MainWindow._apply_preview_size
            _configured_preview_size = RunControlsMixin._configured_preview_size

            def __init__(self, config_object):
                self.config = config_object
                self.preview_size = 200
                self.infoBox = InfoBox()
                self.errors = []
                self.statuses = []
                self.preview_prioritizations = 0

            def show_error(self, *args):
                self.errors.append(args)

            def show_status(self, *args):
                self.statuses.append(args)

            def _prioritize_preview_runs(self):
                self.preview_prioritizations += 1

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
        self.assertEqual(preview_harness.preview_prioritizations, 0)

        preview_300.trigger()
        self.assertEqual(preview_harness.preview_size, 300)
        self.assertEqual(preview_harness.infoBox.preview_size, 300)
        self.assertEqual(self.disk_value("GUI.preview_size"), 300)
        self.assertEqual(preview_harness.preview_prioritizations, 1)

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

    def test_default_colour_map_rolls_back_then_retries(self):
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
            self.assertTrue(plot.setColorbarColorMap("Purples"))
            preferences = PreferencesDialog(self.config)
            preferences.show_error = lambda *args: plot_errors.append(args)
            preferences.defaultColourMapButton.setValue("Purples")
            with self.failing_save(self.config):
                self.assertFalse(preferences.apply_preferences())

            self.assertEqual(plot._colorbar_colormap_name, "Purples")
            self.assertIsInstance(plot.bar.color_map, pg.ColorMap)
            self.assertEqual(self.config.get("user_preference.bar_colour"), "viridis")
            self.assertEqual(self.disk_value("user_preference.bar_colour"), "viridis")
            self.assertEqual(len(plot_errors), 1)

            self.assertTrue(plot.setColorbarColorMap("Purples"))
            preferences.defaultColourMapButton.setValue("Purples")
            self.assertTrue(preferences.apply_preferences())
            preferences.close()
            self.assertEqual(plot._colorbar_colormap_name, "Purples")
            self.assertIsInstance(plot.bar.color_map, pg.ColorMap)
            self.assertEqual(self.disk_value("user_preference.bar_colour"), "Purples")
        finally:
            plot.deleteLater()

    def test_default_database_path_change_is_atomic(self):
        class Harness:
            change_default_file = DatabaseActionsMixin.change_default_file

            def __init__(self, config_object):
                self.config = config_object
                self.errors = []
                self.statuses = []

            def show_error(self, *args):
                self.errors.append(args)

            def show_status(self, *args):
                self.statuses.append(args)

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
                self.recentDatabaseNotice = Mock()
                self.refresh_recent_database_menu = Mock()

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
                main_window,
                "confirm_action",
                return_value=True,
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
                main_window,
                "confirm_action",
                return_value=True,
                ):
            self.assertTrue(harness.restore_default_settings())

        self.assertEqual(self.disk_value("user_preference.theme"), "light")
        self.assertEqual(harness.closed_plots, 1)
        self.assertEqual(harness.applied, 1)
        self.assertEqual(harness.closed_database, 1)

    def test_preferences_keep_complete_draft_when_ok_or_apply_fails(self):
        from qplot.configuration.fonts import FONT_SIZE_KEY

        dialog = PreferencesDialog(self.config)
        dialog.show()
        applied = []
        dialog.preferencesApplied.connect(lambda: applied.append(True))
        font = dialog.fontSizeSpin.value()
        threads = dialog.maxThreadsSpin.value()
        dialog.tabs.setCurrentIndex(1)
        dialog.fontSizeSpin.setValue(font + 1)
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.fontSizeSpin.setValue(font)
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.fontSizeSpin.setValue(font + 1)
        dialog.tabs.setCurrentIndex(3)
        dialog.maxThreadsSpin.setValue(threads + 1)
        draft = dialog.preference_values()
        try:
            with patch.object(qtw.QMessageBox, "exec", return_value=0) as notice, self.failing_save(self.config):
                for button in (qtw.QDialogButtonBox.StandardButton.Apply, qtw.QDialogButtonBox.StandardButton.Ok):
                    dialog.buttonBox.button(button).click()
                    self.assertTrue(dialog.isVisible())
                    self.assertEqual(dialog.preference_values(), draft)
                    self.assertEqual(dialog.apply_button.text(), "Retry")
                    self.assertTrue(dialog.apply_button.isEnabled())
                    self.assertEqual(self.config.get(FONT_SIZE_KEY), font)
                    self.assertEqual(self.disk_value("runtime_settings.max_threads"), threads)
                    self.assertEqual(applied, [])
                self.assertEqual(notice.call_count, 2)
            dialog.apply_button.click()
            self.assertEqual(self.disk_value(FONT_SIZE_KEY), font + 1)
            self.assertEqual(self.config.get("runtime_settings.max_threads"), threads + 1)
            self.assertFalse(dialog.apply_button.isEnabled())
            dialog._accept_preferences()
            self.assertFalse(dialog.isVisible())
            self.assertEqual(applied, [True])
        finally:
            dialog.deleteLater()


def test_recent_database_save_failure_keeps_menu_and_load_usable(typed_run_window, tmp_path):
    window, _runs = typed_run_window
    notice = window.recentDatabaseNotice
    original = window.fileTextbox.text()
    second = str(tmp_path / "second.db")
    third = str(tmp_path / "third.db")
    for filename in (second, third):
        build_line_database(Path(filename), 5)
    key = "file.recent_file_paths"
    before = Path(window.config.default_file).read_bytes()

    with patch.object(window.config, "save_config", side_effect=PermissionError("Access denied")):
        assert window.load_database_path(second)
        wait_for(lambda: notice.isVisible() and not window._database_load_active)
        assert window.RunList.topLevelItemCount() == 1
        assert window.fileTextbox.text() == second
        assert "Loaded 1 run from second.db" in window.status_label.text()
        assert window.plotRunButton.isEnabled()
        assert qtw.QApplication.activeModalWidget() is None
        assert not notice.details.isVisible()
        notice.details_button.click()
        assert notice.details.isVisible()
        assert window.config.default_file in notice.details.text()
        assert "Access denied" in notice.details.text()

        next(button for button in notice.findChildren(qtw.QPushButton)
             if button.text() == "Dismiss").click()
        assert window.load_database_path(third)
        wait_for(lambda: window.fileTextbox.text() == third and not window._database_load_active)
        assert not notice.isVisible()
        assert [action.toolTip() for action in window.recentDatabaseMenu.actions()] == [
            third, second, original,
        ]
        assert window.config.get(key) == [original]
        assert Path(window.config.default_file).read_bytes() == before

    window.recentDatabaseMenu.actions()[0].trigger()
    assert config().get(key) == [third, second, original]
    assert not notice.isVisible()
    assert not notice.details.text()


def test_recent_database_menu_disambiguates_only_shared_names(typed_run_window, tmp_path):
    window, _runs = typed_run_window
    paths = [tmp_path / "first" / "raw" / "data.db", tmp_path / "next" / "raw" / "data.db",
             tmp_path / "other" / "data.db", tmp_path / "one.db"]
    window._session_recent_database_paths = list(map(str, paths))
    window.refresh_recent_database_menu()
    actions = window.recentDatabaseMenu.actions()
    labels = [Path(action.text().split(". ", 1)[1]) for action in actions]
    assert labels == [Path("first/raw/data.db"), Path("next/raw/data.db"), Path("other/data.db"), Path("one.db")]
    assert [action.toolTip() for action in actions] == list(map(str, paths))


def test_reset_only_discards_session_history_after_successful_save(typed_run_window):
    window, _runs = typed_run_window
    current = window.fileTextbox.text()
    key = "file.recent_file_paths"
    window.config.update(key, [])
    with patch.object(main_window, "confirm_action", return_value=True):
        with (
            patch.object(window.config, "save_config", side_effect=PermissionError("Access denied")),
            patch.object(window, "show_error") as explicit_save_error,
        ):
            assert window.load_database_path(current)
            assert window.recentDatabaseNotice.isVisible()
            explicit_save_error.assert_not_called()
            assert not window.restore_default_settings()
            explicit_save_error.assert_called_once()
            assert window.recent_database_paths() == [current]
            assert window.recentDatabaseNotice.isVisible()

        assert window.restore_default_settings()
        assert not window.recentDatabaseNotice.isVisible()
        assert not window.recentDatabaseMenu.isEnabled()
        assert window.recent_database_paths() == config().get(key) == []
