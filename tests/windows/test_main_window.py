import sqlite3
import tempfile
import unittest
from pathlib import Path

from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw

from qplot.windows import main as main_window
from qplot.windows import _database_actions as database_actions
from qplot.datahandling import database as database_module
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    DO_NOT_ASK_AGAIN_LABEL,
    add_confirmation_options,
    add_restore_defaults_option,
    ask_confirmation_with_dont_ask_again,
    )


class DatabaseOpenDirectoryTestCase(unittest.TestCase):
    def test_database_open_directory_prefers_current_database_folder(self):
        class Field:
            def __init__(self, text):
                self._text = text

            def text(self):
                return self._text

        class FakeConfig:
            def __init__(self, default_path):
                self.default_path = default_path

            def get(self, key):
                if key == "file.default_load_path":
                    return self.default_path
                raise KeyError(key)

        class Harness:
            database_open_directory = main_window.MainWindow.database_open_directory

            def __init__(self, database_path, default_path):
                self.fileTextbox = Field(database_path)
                self.config = FakeConfig(default_path)

        with (
            tempfile.TemporaryDirectory() as current_dir,
            tempfile.TemporaryDirectory() as default_dir,
        ):
            database_path = str(Path(current_dir) / "current.db")
            Path(database_path).touch()

            harness = Harness(database_path, default_dir)

            self.assertEqual(harness.database_open_directory(), current_dir)

    def test_database_open_directory_falls_back_to_default_load_path(self):
        class Field:
            def text(self):
                return ""

        class FakeConfig:
            def __init__(self, default_path):
                self.default_path = default_path

            def get(self, key):
                if key == "file.default_load_path":
                    return self.default_path
                raise KeyError(key)

        class Harness:
            database_open_directory = main_window.MainWindow.database_open_directory

            def __init__(self, default_path):
                self.fileTextbox = Field()
                self.config = FakeConfig(default_path)

        with tempfile.TemporaryDirectory() as default_dir:
            harness = Harness(default_dir)

            self.assertEqual(harness.database_open_directory(), default_dir)


class CloseAllPlotsTestCase(unittest.TestCase):
    def test_close_all_can_be_cancelled_when_warning_enabled(self):
        old_confirmation = main_window.ask_confirmation_with_dont_ask_again
        confirmation_keys = []
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == CONFIRM_CLOSE_ALL_KEY:
                    return True
                raise KeyError(key)

        class FakeWindow:
            def close(self):
                closed.append(self)

        class Harness:
            closeAll = main_window.MainWindow.closeAll
            close_plot_windows = main_window.MainWindow.close_plot_windows

            def __init__(self):
                self.config = FakeConfig()
                self.windows = [FakeWindow()]
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            def fake_confirmation(window, title, message, config_key, *args):
                confirmation_keys.append(config_key)
                return qtw.QMessageBox.No

            main_window.ask_confirmation_with_dont_ask_again = fake_confirmation
            harness = Harness()
            harness.closeAll()
        finally:
            main_window.ask_confirmation_with_dont_ask_again = old_confirmation

        self.assertEqual(closed, [])
        self.assertEqual(confirmation_keys, [CONFIRM_CLOSE_ALL_KEY])
        self.assertEqual(harness.status_messages[-1][0], "Close all plot windows cancelled.")

    def test_close_all_without_warning_closes_each_window(self):
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == CONFIRM_CLOSE_ALL_KEY:
                    return False
                raise KeyError(key)

        class FakeWindow:
            def close(self):
                closed.append(self)

        class Harness:
            closeAll = main_window.MainWindow.closeAll
            close_plot_windows = main_window.MainWindow.close_plot_windows

            def __init__(self):
                self.config = FakeConfig()
                self.windows = [FakeWindow(), FakeWindow()]
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        harness = Harness()
        harness.closeAll()

        self.assertEqual(closed, harness.windows)
        self.assertEqual(harness.status_messages[-1][0], "Closing plot windows...")

    def test_close_sweeps_from_plot_closes_matching_cut_windows(self):
        closed = []
        source_ds = object()
        source_param = object()

        class Plot:
            def __init__(self, ds, param):
                self.ds = ds
                self.param = param

        class sweeper:
            def __init__(self, ds, param, sweep_id):
                self.ds = ds
                self.param = param
                self.sweep_id = sweep_id

            def close(self):
                closed.append(self)

        class Harness:
            close_sweeps_from_plot = main_window.MainWindow.close_sweeps_from_plot

            def __init__(self, windows):
                self.windows = windows

        source = Plot(source_ds, source_param)
        target = sweeper(source_ds, source_param, 2)
        other_id = sweeper(source_ds, source_param, 3)
        other_plot = sweeper(object(), source_param, 2)
        harness = Harness([target, other_id, other_plot])

        harness.close_sweeps_from_plot(source, (2,))

        self.assertEqual(closed, [target])

    def test_confirmation_dialog_can_disable_future_warning_after_confirm(self):
        old_exec = qtw.QMessageBox.exec_
        updates = []
        labels = []

        class FakeConfig:
            def update(self, key, value):
                updates.append((key, value))

        window = qtw.QMainWindow()
        window.config = FakeConfig()

        def fake_exec(box):
            labels.append(box.checkBox().text())
            box.checkBox().setChecked(True)
            return qtw.QMessageBox.Yes

        try:
            qtw.QMessageBox.exec_ = fake_exec
            reply = ask_confirmation_with_dont_ask_again(
                window,
                "Close All Plot Windows",
                "Close 2 plot windows?",
                CONFIRM_CLOSE_ALL_KEY,
                )
        finally:
            qtw.QMessageBox.exec_ = old_exec
            window.deleteLater()

        self.assertEqual(reply, qtw.QMessageBox.Yes)
        self.assertEqual(labels, [DO_NOT_ASK_AGAIN_LABEL])
        self.assertEqual(updates, [(CONFIRM_CLOSE_ALL_KEY, False)])

    def test_confirmation_dialog_cancel_does_not_disable_future_warning(self):
        old_exec = qtw.QMessageBox.exec_
        updates = []

        class FakeConfig:
            def update(self, key, value):
                updates.append((key, value))

        window = qtw.QMainWindow()
        window.config = FakeConfig()

        def fake_exec(box):
            box.checkBox().setChecked(True)
            return qtw.QMessageBox.No

        try:
            qtw.QMessageBox.exec_ = fake_exec
            reply = ask_confirmation_with_dont_ask_again(
                window,
                "Confirm Exit",
                "Are you sure you want to exit?",
                CONFIRM_QUIT_KEY,
                )
        finally:
            qtw.QMessageBox.exec_ = old_exec
            window.deleteLater()

        self.assertEqual(reply, qtw.QMessageBox.No)
        self.assertEqual(updates, [])

    def test_close_event_can_disable_future_quit_warning_after_confirm(self):
        old_confirmation = main_window.ask_confirmation_with_dont_ask_again
        old_close_all_windows = qtw.QApplication.closeAllWindows
        confirmations = []
        closed_all_windows = []
        updates = []

        class FakeConfig:
            def __init__(self):
                self.values = {CONFIRM_QUIT_KEY: True}

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                updates.append((key, value))
                self.values[key] = value

        class Timer:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Worker:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class Event:
            def __init__(self):
                self.accepted = False
                self.ignored = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.ignored = True

        class Harness:
            closeEvent = main_window.MainWindow.closeEvent

            def __init__(self):
                self.config = FakeConfig()
                self.startupDatabaseTimer = Timer()
                self._database_load_worker = Worker()
                self._database_load_generation = 0
                self._database_load_active = True
                self._database_load_state = {"loading": True}
                self.monitor = Timer()

        def fake_confirmation(window, title, message, config_key, *args):
            confirmations.append((title, message, config_key))
            window.config.update(config_key, False)
            return qtw.QMessageBox.Yes

        try:
            main_window.ask_confirmation_with_dont_ask_again = fake_confirmation
            qtw.QApplication.closeAllWindows = lambda: closed_all_windows.append(True)
            harness = Harness()
            worker = harness._database_load_worker
            event = Event()

            harness.closeEvent(event)
        finally:
            main_window.ask_confirmation_with_dont_ask_again = old_confirmation
            qtw.QApplication.closeAllWindows = old_close_all_windows

        self.assertTrue(event.accepted)
        self.assertFalse(event.ignored)
        self.assertEqual(
            confirmations,
            [("Confirm Exit", "Are you sure you want to exit?", CONFIRM_QUIT_KEY)],
            )
        self.assertEqual(updates, [(CONFIRM_QUIT_KEY, False)])
        self.assertTrue(harness.startupDatabaseTimer.stopped)
        self.assertTrue(worker.cancelled)
        self.assertFalse(harness._database_load_active)
        self.assertIsNone(harness._database_load_state)
        self.assertIsNone(harness._database_load_worker)
        self.assertTrue(harness.monitor.stopped)
        self.assertEqual(closed_all_windows, [True])

    def test_confirmation_options_use_shared_labels_and_config_keys(self):
        updates = []

        class FakeConfig:
            values = {
                "user_preference.confirm_close_all": True,
                "user_preference.confirm_close": False,
                }

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                updates.append((key, value))
                self.values[key] = value

        window = qtw.QMainWindow()
        window.config = FakeConfig()
        menu = qtw.QMenu(window)

        try:
            add_confirmation_options(window, menu)
            actions = [
                action for action in menu.actions()
                if not action.isSeparator()
                ]

            self.assertEqual(
                [action.text() for action in actions],
                [
                    "Confirm Before Closing All Plot Windows",
                    "Confirm Before Quit",
                    ]
                )
            self.assertTrue(actions[0].isChecked())
            self.assertFalse(actions[1].isChecked())

            actions[0].setChecked(False)
            actions[1].setChecked(True)

            self.assertEqual(updates, [
                ("user_preference.confirm_close_all", False),
                ("user_preference.confirm_close", True),
                ])
        finally:
            window.deleteLater()

    def test_restore_defaults_option_requests_main_window_reset(self):
        called = []
        window = qtw.QMainWindow()
        window.restore_default_settings = lambda: called.append(True)
        menu = qtw.QMenu(window)

        try:
            action = add_restore_defaults_option(window, menu)
            self.assertEqual(action.text(), "Restore Default Settings...")

            action.trigger()

            self.assertEqual(called, [True])
        finally:
            window.deleteLater()

    def test_restore_default_settings_can_be_cancelled(self):
        old_question = qtw.QMessageBox.question

        class FakeConfig:
            def __init__(self):
                self.reset_called = False

            def reset_to_defaults(self):
                self.reset_called = True

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self):
                self.config = FakeConfig()
                self.status_messages = []

            def close_plot_windows(self, confirm=True, status=True):
                raise AssertionError("Plot windows should not close after cancelling")

            def close_database(self, status=True):
                raise AssertionError("Database should not close after cancelling")

            def apply_current_settings(self):
                raise AssertionError("Settings should not be applied after cancelling")

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.No
            harness = Harness()
            harness.restore_default_settings()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertFalse(harness.config.reset_called)
        self.assertEqual(harness.status_messages[-1][0], "Default settings restore cancelled.")

    def test_restore_default_settings_resets_and_applies_defaults(self):
        old_question = qtw.QMessageBox.question

        class FakeConfig:
            def __init__(self):
                self.reset_called = False

            def reset_to_defaults(self):
                self.reset_called = True

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self):
                self.config = FakeConfig()
                self.applied = False
                self.closed_plots = []
                self.closed_database = []
                self.status_messages = []

            def close_plot_windows(self, confirm=True, status=True):
                self.closed_plots.append((confirm, status))

            def close_database(self, status=True):
                self.closed_database.append(status)

            def apply_current_settings(self):
                self.applied = True

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.Yes
            harness = Harness()
            harness.restore_default_settings()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertTrue(harness.config.reset_called)
        self.assertTrue(harness.applied)
        self.assertEqual(harness.closed_plots, [(False, False)])
        self.assertEqual(harness.closed_database, [False])
        self.assertEqual(harness.status_messages[-1][0], "Default settings restored.")


    def test_close_database_clears_loaded_database_state(self):
        class Field:
            def __init__(self, text=""):
                self.value = text

            def setText(self, text):
                self.value = text

            def text(self):
                return self.value

        class Timer:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class RunList:
            def __init__(self):
                self.signals_blocked = None
                self.cleared = False
                self.selection_cleared = False
                self.scrolled = False
                self.watching = ["run"]
                self.maxTime = 123

            def blockSignals(self, blocked):
                self.signals_blocked = blocked

            def clearSelection(self):
                self.selection_cleared = True

            def clear(self):
                self.cleared = True

            def scrollToTop(self):
                self.scrolled = True

            def topLevelItemCount(self):
                return 0

        class EmptyState:
            def __init__(self):
                self.visible = None

            def setVisible(self, visible):
                self.visible = visible

        class Preview:
            def __init__(self):
                self.database_runs = None

            def set_database_runs(self, database_path, runs):
                self.database_runs = (database_path, runs)

        class InfoBox:
            def __init__(self):
                self.preview = Preview()
                self.cleared = False
                self.scrolled = False

            def clear(self):
                self.cleared = True

            def scrollToTop(self):
                self.scrolled = True

        class Harness:
            close_database = main_window.MainWindow.close_database
            _sync_empty_state = main_window.MainWindow._sync_empty_state

            def __init__(self):
                self.monitor = Timer()
                self.fileTextbox = Field("test.db")
                self.run_idBox = Field("7")
                self.measurementBox = Field("x")
                self.selected_run_id = 7
                self.ds = object()
                self.localLastFile = "test.db"
                self.dataset_holder = {"guid": {"del_timer": Timer()}}
                self.RunList = RunList()
                self.infoBox = InfoBox()
                self.emptyStateFrame = EmptyState()

            def show_status(self, message, timeout=5000):
                raise AssertionError("Status should not be shown when disabled")

        harness = Harness()
        del_timer = harness.dataset_holder["guid"]["del_timer"]

        harness.close_database(status=False)

        self.assertTrue(harness.monitor.stopped)
        self.assertEqual(harness.fileTextbox.text(), "")
        self.assertEqual(harness.run_idBox.text(), "")
        self.assertEqual(harness.measurementBox.text(), "*")
        self.assertIsNone(harness.selected_run_id)
        self.assertIsNone(harness.ds)
        self.assertIsNone(harness.localLastFile)
        self.assertTrue(del_timer.stopped)
        self.assertEqual(harness.dataset_holder, {})
        self.assertTrue(harness.RunList.selection_cleared)
        self.assertTrue(harness.RunList.cleared)
        self.assertEqual(harness.RunList.watching, [])
        self.assertEqual(harness.RunList.maxTime, 0)
        self.assertTrue(harness.infoBox.cleared)
        self.assertEqual(harness.infoBox.preview.database_runs, ("", {}))
        self.assertTrue(harness.emptyStateFrame.visible)



class DatabaseAccessProbeTestCase(unittest.TestCase):
    def test_database_access_error_returns_none_for_readable_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "readable.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute("PRAGMA user_version")
            finally:
                conn.close()

            self.assertIsNone(database_module.database_access_error(database_path))

    def test_database_access_error_reports_timeout(self):
        old_run = database_module.subprocess.run

        def run(*args, **kwargs):
            raise database_module.subprocess.TimeoutExpired(
                cmd=args[0],
                timeout=kwargs["timeout"],
                )

        database_module.subprocess.run = run
        try:
            error = database_module.database_access_error("locked.db", timeout=0.5)
        finally:
            database_module.subprocess.run = old_run

        self.assertIn("Timed out after 0.5 s", error)
        self.assertIn("locked", error)


class DatabaseLoadUiTestCase(unittest.TestCase):
    class Field:
        def __init__(self, text=""):
            self.value = text

        def setText(self, text):
            self.value = text

        def text(self):
            return self.value

    class Button:
        def __init__(self):
            self.enabled = True
            self.visible = True

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setVisible(self, visible):
            self.visible = visible

    class Frame:
        def __init__(self):
            self.visible = False

        def setVisible(self, visible):
            self.visible = visible

    class SpinBox:
        def __init__(self, value=1.5):
            self._value = value

        def value(self):
            return self._value

        def setValue(self, value):
            self._value = value

    class Label:
        def __init__(self):
            self.text = ""
            self.tooltip = ""

        def setText(self, text):
            self.text = text

        def setToolTip(self, tooltip):
            self.tooltip = tooltip

    class Timer:
        def __init__(self):
            self.started = []

        def start(self, interval):
            self.started.append(interval)

    class RunList:
        def __init__(self):
            self.runs = {}
            self.selection_cleared = False
            self.scrolled = False
            self.watching = ["old"]
            self.maxTime = 9

        def clearSelection(self):
            self.selection_cleared = True

        def clear(self):
            self.runs = {}

        def addRuns(self, runs):
            self.runs = runs

        def scrollToTop(self):
            self.scrolled = True

        def topLevelItemCount(self):
            return len(self.runs)

    class Preview:
        def __init__(self):
            self.database_runs = None

        def set_database_runs(self, database_path, runs):
            self.database_runs = (database_path, runs)

    class InfoBox:
        def __init__(self):
            self.preview = DatabaseLoadUiTestCase.Preview()
            self.cleared = False
            self.scrolled = False

        def clear(self):
            self.cleared = True

        def scrollToTop(self):
            self.scrolled = True

    class Worker:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Harness:
        cancel_database_load = main_window.MainWindow.cancel_database_load
        database_load_status = main_window.MainWindow.database_load_status
        _hide_database_load_panel = main_window.MainWindow._hide_database_load_panel
        _restore_database_load_previous_state = (
            main_window.MainWindow._restore_database_load_previous_state
            )
        _set_database_load_controls_enabled = (
            main_window.MainWindow._set_database_load_controls_enabled
            )
        _show_database_load_panel = main_window.MainWindow._show_database_load_panel
        _sync_empty_state = main_window.MainWindow._sync_empty_state
        _sync_no_database_empty_state = (
            main_window.MainWindow._sync_no_database_empty_state
            )
        _sync_loaded_empty_state = (
            main_window.MainWindow._sync_loaded_empty_state
            )
        _set_empty_state_button_visible = (
            main_window.MainWindow._set_empty_state_button_visible
            )
        _loaded_empty_database_detail = (
            main_window.MainWindow._loaded_empty_database_detail
            )
        _current_refresh_interval = main_window.MainWindow._current_refresh_interval
        _loaded_empty_database_status = (
            main_window.MainWindow._loaded_empty_database_status
            )
        _empty_database_refresh_status = (
            main_window.MainWindow._empty_database_refresh_status
            )
        _main_refresh_interval = main_window.MainWindow._main_refresh_interval

        def __init__(self):
            self._database_load_generation = 2
            self._database_load_active = False
            self._database_load_state = None
            self._database_load_worker = None
            self.fileTextbox = DatabaseLoadUiTestCase.Field()
            self.run_idBox = DatabaseLoadUiTestCase.Field()
            self.measurementBox = DatabaseLoadUiTestCase.Field()
            self.selected_run_id = 3
            self.ds = object()
            self.RunList = DatabaseLoadUiTestCase.RunList()
            self.infoBox = DatabaseLoadUiTestCase.InfoBox()
            self.monitor = DatabaseLoadUiTestCase.Timer()
            self.loadDatabaseButton = DatabaseLoadUiTestCase.Button()
            self.refreshDatabaseButton = DatabaseLoadUiTestCase.Button()
            self.databaseInfoButton = DatabaseLoadUiTestCase.Button()
            self.openDatabaseFolderButton = DatabaseLoadUiTestCase.Button()
            self.databaseLoadFrame = DatabaseLoadUiTestCase.Frame()
            self.databaseLoadLabel = DatabaseLoadUiTestCase.Label()
            self.emptyStateFrame = DatabaseLoadUiTestCase.Frame()
            self.emptyStateTitle = DatabaseLoadUiTestCase.Label()
            self.emptyStateDetail = DatabaseLoadUiTestCase.Label()
            self.emptyStateLoadButton = DatabaseLoadUiTestCase.Button()
            self.emptyStateRefreshButton = DatabaseLoadUiTestCase.Button()
            self.emptyStateHelpButton = DatabaseLoadUiTestCase.Button()
            self.spinBox = DatabaseLoadUiTestCase.SpinBox()
            self.status_messages = []

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

    def test_database_load_status_shows_inline_progress(self):
        harness = self.Harness()
        harness._database_load_active = True

        harness.database_load_status(2, "Waiting for OneDrive sync...")

        self.assertTrue(harness.databaseLoadFrame.visible)
        self.assertEqual(harness.databaseLoadLabel.text, "Waiting for OneDrive sync...")
        self.assertEqual(harness.databaseLoadLabel.tooltip, "Waiting for OneDrive sync...")
        self.assertEqual(
            harness.status_messages,
            [("Waiting for OneDrive sync...", 0)],
            )

    def test_cancel_database_load_cancels_worker_and_restores_previous_view(self):
        previous_runs = {5: {"guid": "guid-5", "run_timestamp": 123.0}}
        worker = self.Worker()
        harness = self.Harness()
        harness._database_load_active = True
        harness._database_load_worker = worker
        harness._database_load_state = {
            "monitorTimer": 1.5,
            "previous_file": "old.db",
            "previous_runs": previous_runs,
            }

        harness.cancel_database_load()

        self.assertTrue(worker.cancelled)
        self.assertEqual(harness._database_load_generation, 3)
        self.assertFalse(harness._database_load_active)
        self.assertIsNone(harness._database_load_state)
        self.assertIsNone(harness._database_load_worker)
        self.assertEqual(harness.fileTextbox.text(), "old.db")
        self.assertEqual(harness.RunList.runs, previous_runs)
        self.assertEqual(
            harness.infoBox.preview.database_runs,
            ("old.db", previous_runs),
            )
        self.assertEqual(harness.RunList.watching, [])
        self.assertEqual(harness.RunList.maxTime, 0)
        self.assertEqual(harness.monitor.started, [1500])
        self.assertFalse(harness.databaseLoadFrame.visible)
        self.assertEqual(harness.databaseLoadLabel.text, "")
        self.assertTrue(harness.loadDatabaseButton.enabled)
        self.assertTrue(harness.refreshDatabaseButton.enabled)
        self.assertFalse(harness.emptyStateFrame.visible)
        self.assertEqual(harness.status_messages[-1], ("Database load cancelled.", 3000))

    def test_empty_state_is_visible_only_without_database_runs_or_loading(self):
        harness = self.Harness()

        harness._sync_empty_state()
        self.assertTrue(harness.emptyStateFrame.visible)
        self.assertEqual(harness.emptyStateTitle.text, "No database loaded")
        self.assertTrue(harness.emptyStateLoadButton.visible)
        self.assertFalse(harness.emptyStateRefreshButton.visible)
        self.assertTrue(harness.emptyStateHelpButton.visible)

        harness.fileTextbox.setText("loaded.db")
        harness._sync_empty_state()
        self.assertTrue(harness.emptyStateFrame.visible)
        self.assertEqual(harness.emptyStateTitle.text, "Waiting for measurements")
        self.assertIn("loaded.db is loaded", harness.emptyStateDetail.text)
        self.assertIn("checking every 1.5 s", harness.emptyStateDetail.text)
        self.assertTrue(harness.emptyStateLoadButton.visible)
        self.assertTrue(harness.emptyStateRefreshButton.visible)
        self.assertFalse(harness.emptyStateHelpButton.visible)

        harness.fileTextbox.setText("")
        harness.RunList.addRuns({1: {"guid": "guid-1"}})
        harness._sync_empty_state()
        self.assertFalse(harness.emptyStateFrame.visible)

        harness.RunList.clear()
        harness._database_load_active = True
        harness._sync_empty_state()
        self.assertFalse(harness.emptyStateFrame.visible)

    def test_loaded_empty_state_reports_manual_refresh(self):
        harness = self.Harness()
        harness.spinBox.setValue(0)

        detail = harness._loaded_empty_database_detail("manual.db")
        status = harness._loaded_empty_database_status("manual.db", 0.25)

        self.assertIn("Refresh is set to manual", detail)
        self.assertIn("refresh manually", status)
        self.assertEqual(
            harness._empty_database_refresh_status(),
            "No measurements found yet.",
            )


class RefreshMainEmptyDatabaseTestCase(unittest.TestCase):
    class RunList:
        def __init__(self):
            self.maxTime = 0
            self.checked_watching = False

        def checkWatching(self):
            self.checked_watching = True

        def topLevelItemCount(self):
            return 0

    class Harness:
        refreshMain = main_window.MainWindow.refreshMain
        _empty_database_refresh_status = (
            main_window.MainWindow._empty_database_refresh_status
            )
        _main_refresh_interval = main_window.MainWindow._main_refresh_interval
        _current_refresh_interval = main_window.MainWindow._current_refresh_interval

        def __init__(self):
            self.fileTextbox = DatabaseLoadUiTestCase.Field("empty.db")
            self.RunList = RefreshMainEmptyDatabaseTestCase.RunList()
            self.spinBox = DatabaseLoadUiTestCase.SpinBox(1.5)
            self.status_messages = []
            self.sync_count = 0

        def _sync_empty_state(self):
            self.sync_count += 1

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

        def show_error(self, title, message, details=None):
            raise AssertionError((title, message, details))

    def test_refresh_empty_database_reports_waiting_state(self):
        old_find_new_runs = database_actions.find_new_runs
        database_actions.find_new_runs = lambda last_time: {}

        try:
            harness = self.Harness()
            harness.refreshMain()
        finally:
            database_actions.find_new_runs = old_find_new_runs

        self.assertTrue(harness.RunList.checked_watching)
        self.assertEqual(harness.sync_count, 1)
        self.assertEqual(
            harness.status_messages,
            [
                ("Checking for new runs...", 0),
                ("No measurements found yet; still waiting for new runs.", 3000),
                ],
            )


class RefreshMainAutoPlotTestCase(unittest.TestCase):
    class RunList:
        def __init__(self):
            self.maxTime = 10.0
            self.checked_watching = False
            self.added_runs = None

        def checkWatching(self):
            self.checked_watching = True

        def addRuns(self, runs):
            self.added_runs = runs

    class Preview:
        def __init__(self):
            self.added_runs = None

        def add_runs(self, runs):
            self.added_runs = runs

    class InfoBox:
        def __init__(self):
            self.preview = RefreshMainAutoPlotTestCase.Preview()

    class AutoPlotBox:
        def __init__(self, checked):
            self.checked = checked

        def isChecked(self):
            return self.checked

    class Harness:
        refreshMain = main_window.MainWindow.refreshMain

        def __init__(self, auto_plot_checked):
            self.fileTextbox = DatabaseLoadUiTestCase.Field("loaded.db")
            self.RunList = RefreshMainAutoPlotTestCase.RunList()
            self.infoBox = RefreshMainAutoPlotTestCase.InfoBox()
            self.autoPlotBox = RefreshMainAutoPlotTestCase.AutoPlotBox(
                auto_plot_checked
                )
            self.status_messages = []
            self.plotted_guids = []
            self.sync_count = 0

        def _sync_empty_state(self):
            self.sync_count += 1

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

        def show_error(self, title, message, details=None):
            raise AssertionError((title, message, details))

        def openPlot(self, guid):
            self.plotted_guids.append(guid)

    def test_refresh_auto_plots_new_runs_when_enabled(self):
        new_runs = {
            1: {"guid": "guid-1", "run_timestamp": 11.0},
            2: {"guid": "guid-2", "run_timestamp": 12.5},
            }
        seen_last_times = []
        old_find_new_runs = database_actions.find_new_runs

        def find_new_runs(last_time):
            seen_last_times.append(last_time)
            return new_runs

        database_actions.find_new_runs = find_new_runs
        try:
            harness = self.Harness(auto_plot_checked=True)
            harness.refreshMain()
        finally:
            database_actions.find_new_runs = old_find_new_runs

        self.assertEqual(seen_last_times, [10.0])
        self.assertTrue(harness.RunList.checked_watching)
        self.assertEqual(harness.RunList.maxTime, 12.5)
        self.assertIs(harness.RunList.added_runs, new_runs)
        self.assertIs(harness.infoBox.preview.added_runs, new_runs)
        self.assertEqual(harness.sync_count, 1)
        self.assertEqual(harness.plotted_guids, ["guid-1", "guid-2"])
        self.assertEqual(
            harness.status_messages,
            [
                ("Checking for new runs...", 0),
                ("Found 2 new runs.", 5000),
                ],
            )

    def test_refresh_does_not_auto_plot_new_runs_when_disabled(self):
        old_find_new_runs = database_actions.find_new_runs
        database_actions.find_new_runs = lambda _last_time: {
            1: {"guid": "guid-1", "run_timestamp": 11.0},
            }
        try:
            harness = self.Harness(auto_plot_checked=False)
            harness.refreshMain()
        finally:
            database_actions.find_new_runs = old_find_new_runs

        self.assertEqual(harness.plotted_guids, [])
        self.assertEqual(
            harness.status_messages[-1],
            ("Found 1 new run.", 5000),
            )


class CloudDatabasePrefetchTestCase(unittest.TestCase):
    def test_prefetch_database_file_reads_file_and_reports_cloud_sync_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "prefetch.db")
            Path(database_path).write_bytes(b"x" * 10)
            statuses = []
            old_label = database_module.database_cloud_storage_label
            database_module.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = database_module.prefetch_database_file(
                    database_path,
                    status_callback=statuses.append,
                    chunk_size=4,
                    status_interval=0,
                    )
            finally:
                database_module.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertTrue(statuses[0].startswith("Waiting for OneDrive sync..."))
        self.assertIn("100% available", statuses[-1])

    def test_prefetch_database_file_with_timeout_uses_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "prefetch-timeout.db")
            Path(database_path).write_bytes(b"x" * 10)
            statuses = []
            old_label = database_module.database_cloud_storage_label
            database_module.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = database_module.prefetch_database_file_with_timeout(
                    database_path,
                    timeout=5,
                    status_callback=statuses.append,
                    )
            finally:
                database_module.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertIn("Waiting for OneDrive sync...", statuses)
        self.assertIn("Waiting for OneDrive sync... 100% available", statuses)

    def test_prefetch_database_file_with_timeout_kills_stalled_process(self):
        old_popen = database_module.subprocess.Popen
        killed = []

        class Pipe:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class Process:
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def poll(self):
                return None

            def kill(self):
                killed.append(True)

            def wait(self):
                self.returncode = -9

        database_module.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(TimeoutError) as caught:
                database_module.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=0.01,
                    status_callback=lambda _message: None,
                    )
        finally:
            database_module.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])
        self.assertIn("Timed out after 0.01 s", str(caught.exception))
        self.assertIn("OneDrive", str(caught.exception))

    def test_prefetch_database_file_with_timeout_stops_when_cancelled(self):
        old_popen = database_module.subprocess.Popen
        killed = []

        class Pipe:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class Process:
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def poll(self):
                return None

            def kill(self):
                killed.append(True)

            def wait(self):
                self.returncode = -9

        database_module.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(InterruptedError):
                database_module.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=5,
                    cancelled_callback=lambda: True,
                    )
        finally:
            database_module.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])


class DatabaseLoadWorkerTestCase(unittest.TestCase):
    def test_database_load_worker_initialises_database_and_returns_runs(self):
        old_access_error = database_module.database_access_error
        old_initialise = database_module.initialise_or_create_database_at
        old_get_runs = database_module.get_runs_via_sql
        calls = []

        def access_error(database_path):
            calls.append(("access", database_path))
            return None

        def initialise(database_path):
            calls.append(("initialise", database_path))

        def get_runs():
            calls.append(("runs", None))
            return {1: {"guid": "guid-1", "run_timestamp": 123.0}}

        database_module.database_access_error = access_error
        database_module.initialise_or_create_database_at = initialise
        database_module.get_runs_via_sql = get_runs
        try:
            worker = main_window.DatabaseLoadWorker(7, "example.db")
            statuses = []
            finished = []
            worker.signals.status.connect(lambda *args: statuses.append(args))
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            database_module.database_access_error = old_access_error
            database_module.initialise_or_create_database_at = old_initialise
            database_module.get_runs_via_sql = old_get_runs

        self.assertEqual(calls, [
            ("access", "example.db"),
            ("initialise", "example.db"),
            ("runs", None),
            ])
        self.assertEqual(statuses, [
            (7, "Checking database access..."),
            (7, "Initialising database..."),
            (7, "Loading run list..."),
            ])
        self.assertEqual(finished, [
            (7, "example.db", {1: {"guid": "guid-1", "run_timestamp": 123.0}}, None)
            ])

    def test_database_load_worker_reports_access_error(self):
        old_access_error = database_module.database_access_error
        old_initialise = database_module.initialise_or_create_database_at

        database_module.database_access_error = lambda _path: "locked database"
        database_module.initialise_or_create_database_at = lambda _path: self.fail(
            "Database should not initialise after an access error"
            )
        try:
            worker = main_window.DatabaseLoadWorker(3, "locked.db")
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            database_module.database_access_error = old_access_error
            database_module.initialise_or_create_database_at = old_initialise

        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][:3], (3, "locked.db", {}))
        self.assertIsInstance(finished[0][3], RuntimeError)
        self.assertIn("locked database", str(finished[0][3]))

    def test_database_load_worker_does_not_start_when_cancelled(self):
        old_placeholder = database_module.database_is_likely_cloud_placeholder
        old_access_error = database_module.database_access_error
        calls = []

        database_module.database_is_likely_cloud_placeholder = lambda _path: calls.append(
            "placeholder"
            )
        database_module.database_access_error = lambda _path: calls.append("access")
        try:
            worker = main_window.DatabaseLoadWorker(4, "example.db")
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.cancel()
            worker.run()
        finally:
            database_module.database_is_likely_cloud_placeholder = old_placeholder
            database_module.database_access_error = old_access_error

        self.assertEqual(calls, [])
        self.assertEqual(finished, [])

    def test_database_load_worker_stops_after_cancelled_prefetch(self):
        old_placeholder = database_module.database_is_likely_cloud_placeholder
        old_prefetch = database_module.prefetch_database_file_with_timeout
        old_access_error = database_module.database_access_error
        calls = []

        def prefetch(
                database_path,
                timeout=None,
                status_callback=None,
                cancelled_callback=None,
                ):
            calls.append(("prefetch", database_path, timeout))
            raise InterruptedError("Database load cancelled.")

        database_module.database_is_likely_cloud_placeholder = lambda _path: True
        database_module.prefetch_database_file_with_timeout = prefetch
        database_module.database_access_error = lambda _path: calls.append("access")
        try:
            worker = main_window.DatabaseLoadWorker(5, "cloud.db", 8)
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            database_module.database_is_likely_cloud_placeholder = old_placeholder
            database_module.prefetch_database_file_with_timeout = old_prefetch
            database_module.database_access_error = old_access_error

        self.assertEqual(calls, [("prefetch", "cloud.db", 8)])
        self.assertEqual(finished, [])

    def test_database_load_worker_ignores_deleted_qt_signals_at_shutdown(self):
        class DeletedSignal:
            def emit(self, *args):
                raise RuntimeError(
                    "wrapped C/C++ object of type DatabaseLoadSignals has been deleted"
                    )

        class DeletedSignals:
            status = DeletedSignal()
            finished = DeletedSignal()

        worker = main_window.DatabaseLoadWorker(6, "example.db")
        worker.signals = DeletedSignals()

        worker._emit_status("Checking database access...")
        worker._emit_finished({}, None)

    def test_database_load_worker_waits_for_cloud_sync_and_retries_probe(self):
        old_access_error = database_module.database_access_error
        old_label = database_module.database_cloud_storage_label
        old_placeholder = database_module.database_is_likely_cloud_placeholder
        old_prefetch = database_module.prefetch_database_file_with_timeout
        old_initialise = database_module.initialise_or_create_database_at
        old_get_runs = database_module.get_runs_via_sql
        calls = []

        access_results = iter(["timed out", None])

        def access_error(database_path):
            calls.append(("access", database_path))
            return next(access_results)

        def prefetch(
                database_path,
                timeout=None,
                status_callback=None,
                cancelled_callback=None,
                ):
            calls.append(("prefetch", database_path, timeout))
            self.assertIsNotNone(cancelled_callback)
            status_callback("Waiting for OneDrive sync... 100% available")
            return 10

        database_module.database_access_error = access_error
        database_module.database_cloud_storage_label = lambda _path: "OneDrive"
        database_module.database_is_likely_cloud_placeholder = lambda _path: False
        database_module.prefetch_database_file_with_timeout = prefetch
        database_module.initialise_or_create_database_at = lambda path: calls.append(
            ("initialise", path)
            )
        database_module.get_runs_via_sql = lambda: {}
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as database:
                worker = main_window.DatabaseLoadWorker(9, database.name, 12)
                statuses = []
                finished = []
                worker.signals.status.connect(lambda *args: statuses.append(args))
                worker.signals.finished.connect(lambda *args: finished.append(args))

                worker.run()
                expected_path = database.name
        finally:
            database_module.database_access_error = old_access_error
            database_module.database_cloud_storage_label = old_label
            database_module.database_is_likely_cloud_placeholder = old_placeholder
            database_module.prefetch_database_file_with_timeout = old_prefetch
            database_module.initialise_or_create_database_at = old_initialise
            database_module.get_runs_via_sql = old_get_runs

        self.assertEqual(calls, [
            ("access", expected_path),
            ("prefetch", expected_path, 12),
            ("access", expected_path),
            ("initialise", expected_path),
            ])
        self.assertIn((9, "Waiting for OneDrive sync... 100% available"), statuses)
        self.assertEqual(finished, [(9, expected_path, {}, None)])


class DatabaseLoadRequestTestCase(unittest.TestCase):
    def test_load_database_path_rejects_missing_file_before_starting_worker(self):
        class Harness:
            load_database_path = main_window.MainWindow.load_database_path

            def __init__(self):
                self.errors = []

            def show_error(self, title, message, details=None):
                self.errors.append((title, message, details))

        harness = Harness()

        self.assertFalse(harness.load_database_path("missing.db"))
        self.assertEqual(harness.errors[0][0], "Database Load Failed")
        self.assertIn("could not be found", harness.errors[0][1])


class DatabaseDropTestCase(unittest.TestCase):
    def test_database_info_report_summarises_qcodes_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "info.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE experiments (exp_id INTEGER PRIMARY KEY, name TEXT, sample_name TEXT)"
                    )
                conn.execute("""
                  CREATE TABLE runs (
                      run_id INTEGER PRIMARY KEY,
                      name TEXT,
                      run_timestamp REAL,
                      completed_timestamp REAL,
                      is_completed INTEGER,
                      guid TEXT
                  )
                """)
                conn.execute(
                    "INSERT INTO experiments (exp_id, name, sample_name) VALUES (1, 'exp', 'sample')"
                    )
                conn.execute(
                    """
                    INSERT INTO runs
                    (run_id, name, run_timestamp, completed_timestamp, is_completed, guid)
                    VALUES (3, 'measurement', 1768129603, 1768129626, 1, 'guid-3')
                    """
                    )
                conn.commit()
            finally:
                conn.close()

            report = main_window.database_info_report(database_path)

        self.assertIn("Runs: 1", report)
        self.assertIn("Experiments: 1", report)
        self.assertIn("Latest run ID: 3", report)
        self.assertIn("Latest run GUID: guid-3", report)
        self.assertIn("Database schema version:", report)
        self.assertIn("Last modified:", report)
        self.assertNotIn("Selected run ID:", report)
        self.assertNotIn("Installed QCoDeS version:", report)
        self.assertNotIn("QCoDeS active database:", report)
        self.assertNotIn("SQLite version:", report)

    def test_database_path_from_mime_data_accepts_one_local_db_file(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            mime_data = QtCore.QMimeData()
            mime_data.setUrls([QtCore.QUrl.fromLocalFile(database.name)])

            self.assertEqual(
                main_window.database_path_from_mime_data(mime_data),
                database.name
                )

    def test_database_path_from_mime_data_rejects_ambiguous_or_non_db_drops(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".db") as database,
            tempfile.NamedTemporaryFile(suffix=".txt") as text_file,
        ):
            text_drop = QtCore.QMimeData()
            text_drop.setUrls([QtCore.QUrl.fromLocalFile(text_file.name)])

            multiple_drop = QtCore.QMimeData()
            multiple_drop.setUrls([
                QtCore.QUrl.fromLocalFile(database.name),
                QtCore.QUrl.fromLocalFile(text_file.name),
                ])

            self.assertIsNone(main_window.database_path_from_mime_data(text_drop))
            self.assertIsNone(main_window.database_path_from_mime_data(multiple_drop))
