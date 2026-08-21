import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.file_identity import database_instance
from qplot.windows._database_actions import DatabaseActionsMixin
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._run_controls import RunControlsMixin


class _Config:
    def __init__(self, refresh_rate=0.1):
        self.values = {
            "user_preference.default_refresh_rate": refresh_rate,
            "user_preference.auto_plot": False,
            "user_preference.theme": "light",
            "user_preference.colorbar_width": 15,
            "user_preference.axis_tick_width": 2.0,
            "user_preference.axis_major_tick_count": 3,
            "GUI.preview_size": 200,
            "user_preference.mouse_mode": "pan",
            "user_preference.copy_plot_image_resolution": "screen",
            "file.default_load_path": "",
            "user_preference.confirm_close_all": True,
            "user_preference.confirm_close": True,
            "runtime_settings.max_threads": 2,
            "runtime_settings.max_full_heatmap_points": 1000,
            "runtime_settings.del_grace_period": 1.0,
            "runtime_settings.cloud_sync_timeout": 1.0,
        }
        self.schema = {"properties": {}}
        for key, value in self.values.items():
            section, name = key.split(".")
            section_schema = self.schema["properties"].setdefault(
                section,
                {"properties": {}},
            )
            section_schema["properties"][name] = {"default": value}

    def get(self, key):
        return self.values[key]

    def update(self, key, value):
        self.values[key] = value

    def update_many(self, values):
        self.values.update(values)


class _RefreshHarness(qtw.QMainWindow):
    initRefresh = RunControlsMixin.initRefresh
    monitorIntervalChanged = RunControlsMixin.monitorIntervalChanged
    _apply_refresh_interval = RunControlsMixin._apply_refresh_interval
    _save_refresh_interval = RunControlsMixin._save_refresh_interval
    _sync_refresh_interval = RunControlsMixin._sync_refresh_interval
    _current_refresh_interval = RunControlsMixin._current_refresh_interval
    _committed_refresh_database_instance = (
        RunControlsMixin._committed_refresh_database_instance
    )
    _automatic_refresh_should_run = RunControlsMixin._automatic_refresh_should_run
    _stop_automatic_refresh_timer = RunControlsMixin._stop_automatic_refresh_timer
    _update_automatic_refresh_timer = RunControlsMixin._update_automatic_refresh_timer
    _automatic_refresh_timeout = RunControlsMixin._automatic_refresh_timeout

    def __init__(self):
        super().__init__()
        self.config = _Config()
        self.monitor = QtCore.QTimer(self)
        self._database_load_generation = 0
        self._database_load_active = False
        self._database_view_released_for_generation = False
        self._loaded_database_instance = None
        self._loaded_database_identity = None
        self.automatic_refreshes = []
        self.reloads = []
        self.status_messages = []
        self.initRefresh()

    def refreshMain(self, automatic=False):
        self.automatic_refreshes.append(automatic)

    def _reload_replaced_database(self, database_path):
        self.reloads.append(database_path)

    def _sync_empty_state(self):
        pass

    def show_error(self, *_args):
        raise AssertionError("Unexpected preferences persistence error")

    def show_status(self, message, timeout=5000):
        self.status_messages.append((message, timeout))

    def closeAll(self):
        pass

    def _auto_plot_changed(self, _checked):
        pass


class RefreshTimerLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.window = _RefreshHarness()

    def tearDown(self):
        self.window.monitor.stop()
        self.window.deleteLater()

    def _accept_database(self, path):
        instance = database_instance(path)
        self.assertIsNotNone(instance.identity)
        self.window._loaded_database_instance = instance
        self.window._loaded_database_identity = instance.identity
        return instance

    def test_positive_spinner_and_preferences_intervals_stay_inactive_without_database(self):
        self.window.spinBox.setValue(0.1)
        self.assertFalse(self.window.monitor.isActive())

        dialog = PreferencesDialog(self.window.config, self.window)
        try:
            dialog.preferencesApplied.connect(self.window._sync_refresh_interval)
            dialog.refreshRateSpin.setValue(0.2)
            self.assertTrue(dialog.apply_preferences())
        finally:
            dialog.deleteLater()

        self.assertEqual(self.window.config.get("user_preference.default_refresh_rate"), 0.2)
        self.assertFalse(self.window.monitor.isActive())
        self.assertEqual(self.window.status_messages, [])

    def test_committed_database_starts_timer_and_timeout_is_event_loop_delivered_once(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            source.write_bytes(b"qplot refresh source")
            self._accept_database(source)
            self.window._apply_refresh_interval(0.1)
            timeout_spy = QtTest.QSignalSpy(self.window.monitor.timeout)

            self.assertTrue(self.window.monitor.isActive())
            self.assertEqual(self.window.monitor.interval(), 100)
            self.assertTrue(timeout_spy.wait(250))
            self.window.monitor.stop()

        self.assertEqual(self.window.automatic_refreshes, [True])

    def test_disabled_invalidated_replaced_and_shutdown_timeouts_are_silent(self):
        with TemporaryDirectory() as directory:
            source_a = Path(directory) / "source-a.db"
            source_b = Path(directory) / "source-b.db"
            source_a.write_bytes(b"a")
            source_b.write_bytes(b"b")
            instance_a = self._accept_database(source_a)
            self.window._apply_refresh_interval(0.1)

            # Simulate an event already queued just before interval zero.
            self.window._apply_refresh_interval(0)
            self.window.monitor.timeout.emit()
            self.assertEqual(self.window.automatic_refreshes, [])

            self.window._loaded_database_instance = instance_a
            self.window._database_load_generation += 1
            self.window._apply_refresh_interval(0.1)
            self.window._loaded_database_instance = database_instance(source_b)
            self.window._database_load_generation += 1
            self.window.monitor.timeout.emit()
            self.assertEqual(self.window.automatic_refreshes, [])
            self.assertFalse(self.window.monitor.isActive())

            self.window._loaded_database_instance = instance_a
            self.window._database_load_generation += 1
            self.window._apply_refresh_interval(0.1)
            self.window._automatic_refresh_shutdown = True
            self.window.monitor.timeout.emit()

        self.assertEqual(self.window.automatic_refreshes, [])
        self.assertFalse(self.window.monitor.isActive())

    def test_load_pause_failure_cancel_and_unload_follow_committed_state(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            source.write_bytes(b"source")
            self._accept_database(source)
            self.window._apply_refresh_interval(0.1)
            self.assertTrue(self.window.monitor.isActive())

            # Starting a replacement/load pauses refresh. A failed or
            # cancelled request can resume only while the old instance remains
            # committed; unload cannot.
            self.window._database_load_active = True
            self.window._update_automatic_refresh_timer()
            self.assertFalse(self.window.monitor.isActive())
            self.window._database_load_active = False
            self.window._update_automatic_refresh_timer()
            self.assertTrue(self.window.monitor.isActive())
            self.window._loaded_database_instance = None
            self.window._update_automatic_refresh_timer()

        self.assertFalse(self.window.monitor.isActive())

    def test_repeated_setting_changes_do_not_multiply_timeout_callbacks_or_touch_source(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            sidecars = [
                source,
                Path(f"{source}-wal"),
                Path(f"{source}-shm"),
                Path(f"{source}-journal"),
            ]
            for index, path in enumerate(sidecars):
                path.write_bytes(f"source-{index}".encode())
            before = {path: path.read_bytes() for path in sidecars}
            self._accept_database(source)

            for interval in (0.1, 0.2, 0.1, 0.3):
                self.window._apply_refresh_interval(interval)
            self.window.monitor.timeout.emit()
            self.window.monitor.stop()

            after = {path: path.read_bytes() for path in sidecars}

        self.assertEqual(self.window.automatic_refreshes, [True])
        self.assertEqual(after, before)

    def test_manual_refresh_without_database_keeps_the_single_useful_message(self):
        class ManualRefreshHarness:
            refreshMain = DatabaseActionsMixin.refreshMain

            def __init__(self):
                self.fileTextbox = qtw.QLineEdit()
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        harness = ManualRefreshHarness()
        harness.refreshMain()

        self.assertEqual(
            harness.status_messages,
            [("Load a database before refreshing.", 5000)],
        )
