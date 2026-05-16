import unittest

from qplot.windows import main as main_window
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    )


class FakeConfig:
    def __init__(self):
        self.values = {
            "user_preference.theme": "light",
            "GUI.preview_size": 200,
            "file.default_load_path": "",
            "user_preference.default_refresh_rate": 1.0,
            CONFIRM_CLOSE_ALL_KEY: True,
            CONFIRM_QUIT_KEY: True,
            "runtime_settings.max_threads": 4,
            "runtime_settings.del_grace_period": 10.0,
            "runtime_settings.cloud_sync_timeout": 120.0,
            }
        self.updates = []

    def get(self, key):
        return self.values[key]

    def update(self, key, value):
        self.updates.append((key, value))
        self.values[key] = value


class PreferencesDialogTestCase(unittest.TestCase):
    def test_dialog_loads_current_config_values(self):
        cfg = FakeConfig()
        cfg.values.update({
            "user_preference.theme": "dark",
            "GUI.preview_size": 300,
            "file.default_load_path": "C:/qcodes",
            "user_preference.default_refresh_rate": 2.5,
            CONFIRM_CLOSE_ALL_KEY: False,
            CONFIRM_QUIT_KEY: False,
            "runtime_settings.max_threads": 8,
            "runtime_settings.del_grace_period": 15.5,
            "runtime_settings.cloud_sync_timeout": 240.0,
            })

        dialog = PreferencesDialog(cfg)
        try:
            self.assertEqual(dialog.themeCombo.currentData(), "dark")
            self.assertEqual(dialog.previewSizeSpin.value(), 300)
            self.assertEqual(dialog.defaultLoadPathEdit.text(), "C:/qcodes")
            self.assertEqual(dialog.refreshRateSpin.value(), 2.5)
            self.assertFalse(dialog.confirmCloseAllCheck.isChecked())
            self.assertFalse(dialog.confirmQuitCheck.isChecked())
            self.assertEqual(dialog.maxThreadsSpin.value(), 8)
            self.assertEqual(dialog.delGracePeriodSpin.value(), 15.5)
            self.assertEqual(dialog.cloudSyncTimeoutSpin.value(), 240.0)
        finally:
            dialog.deleteLater()

    def test_apply_preferences_writes_changed_values_and_emits_signal(self):
        cfg = FakeConfig()
        applied = []
        dialog = PreferencesDialog(cfg)

        try:
            dialog.preferencesApplied.connect(lambda: applied.append(True))
            dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("pyqt"))
            dialog.previewSizeSpin.setValue(500)
            dialog.defaultLoadPathEdit.setText("C:/measurements")
            dialog.refreshRateSpin.setValue(3.5)
            dialog.confirmCloseAllCheck.setChecked(False)
            dialog.confirmQuitCheck.setChecked(False)
            dialog.maxThreadsSpin.setValue(9)
            dialog.delGracePeriodSpin.setValue(20.0)
            dialog.cloudSyncTimeoutSpin.setValue(300.0)

            self.assertTrue(dialog.apply_preferences())

            self.assertEqual(cfg.updates, [
                ("user_preference.theme", "pyqt"),
                ("GUI.preview_size", 500),
                ("file.default_load_path", "C:/measurements"),
                ("user_preference.default_refresh_rate", 3.5),
                (CONFIRM_CLOSE_ALL_KEY, False),
                (CONFIRM_QUIT_KEY, False),
                ("runtime_settings.max_threads", 9),
                ("runtime_settings.del_grace_period", 20.0),
                ("runtime_settings.cloud_sync_timeout", 300.0),
                ])
            self.assertEqual(applied, [True])
        finally:
            dialog.deleteLater()

    def test_apply_preferences_skips_unchanged_values(self):
        cfg = FakeConfig()
        dialog = PreferencesDialog(cfg)

        try:
            self.assertTrue(dialog.apply_preferences())

            self.assertEqual(cfg.updates, [])
        finally:
            dialog.deleteLater()


class PreferencesIntegrationTestCase(unittest.TestCase):
    def test_thread_pool_setting_is_synced_from_config(self):
        class ThreadPool:
            def __init__(self):
                self.max_thread_counts = []

            def setMaxThreadCount(self, value):
                self.max_thread_counts.append(value)

        class Config:
            def get(self, key):
                if key == "runtime_settings.max_threads":
                    return 7
                raise KeyError(key)

        class Harness:
            _sync_thread_pool_settings = (
                main_window.MainWindow._sync_thread_pool_settings
                )

            def __init__(self):
                self.config = Config()
                self.threadPool = ThreadPool()

        harness = Harness()
        harness._sync_thread_pool_settings()

        self.assertEqual(harness.threadPool.max_thread_counts, [7])
