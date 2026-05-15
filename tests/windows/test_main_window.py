import sqlite3
import tempfile
import unittest
from pathlib import Path

from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw

from qplot.windows import main as main_window
from qplot.windows._window_controls import (
    add_confirmation_options,
    add_restore_defaults_option,
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
        old_question = qtw.QMessageBox.question
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == "user_preference.confirm_close_all":
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
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.No
            harness = Harness()
            harness.closeAll()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertEqual(closed, [])
        self.assertEqual(harness.status_messages[-1][0], "Close all plot windows cancelled.")

    def test_close_all_without_warning_closes_each_window(self):
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == "user_preference.confirm_close_all":
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



class DatabaseAccessProbeTestCase(unittest.TestCase):
    def test_database_access_error_returns_none_for_readable_database(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            conn = sqlite3.connect(database.name)
            try:
                conn.execute("PRAGMA user_version")
            finally:
                conn.close()

            self.assertIsNone(main_window.database_access_error(database.name))

    def test_database_access_error_reports_timeout(self):
        old_run = main_window.subprocess.run

        def run(*args, **kwargs):
            raise main_window.subprocess.TimeoutExpired(
                cmd=args[0],
                timeout=kwargs["timeout"],
                )

        main_window.subprocess.run = run
        try:
            error = main_window.database_access_error("locked.db", timeout=0.5)
        finally:
            main_window.subprocess.run = old_run

        self.assertIn("Timed out after 0.5 s", error)
        self.assertIn("locked", error)


class CloudDatabasePrefetchTestCase(unittest.TestCase):
    def test_prefetch_database_file_reads_file_and_reports_cloud_sync_progress(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database.write(b"x" * 10)
            database.flush()
            statuses = []
            old_label = main_window.database_cloud_storage_label
            main_window.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = main_window.prefetch_database_file(
                    database.name,
                    status_callback=statuses.append,
                    chunk_size=4,
                    status_interval=0,
                    )
            finally:
                main_window.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertTrue(statuses[0].startswith("Waiting for OneDrive sync..."))
        self.assertIn("100% available", statuses[-1])

    def test_prefetch_database_file_with_timeout_uses_subprocess(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database.write(b"x" * 10)
            database.flush()
            statuses = []
            old_label = main_window.database_cloud_storage_label
            main_window.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = main_window.prefetch_database_file_with_timeout(
                    database.name,
                    timeout=5,
                    status_callback=statuses.append,
                    )
            finally:
                main_window.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertIn("Waiting for OneDrive sync...", statuses)
        self.assertIn("Waiting for OneDrive sync... 100% available", statuses)

    def test_prefetch_database_file_with_timeout_kills_stalled_process(self):
        old_popen = main_window.subprocess.Popen
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

        main_window.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(TimeoutError) as caught:
                main_window.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=0.01,
                    status_callback=lambda _message: None,
                    )
        finally:
            main_window.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])
        self.assertIn("Timed out after 0.01 s", str(caught.exception))
        self.assertIn("OneDrive", str(caught.exception))


class DatabaseLoadWorkerTestCase(unittest.TestCase):
    def test_database_load_worker_initialises_database_and_returns_runs(self):
        old_access_error = main_window.database_access_error
        old_initialise = main_window.initialise_or_create_database_at
        old_get_runs = main_window.get_runs_via_sql
        calls = []

        def access_error(database_path):
            calls.append(("access", database_path))
            return None

        def initialise(database_path):
            calls.append(("initialise", database_path))

        def get_runs():
            calls.append(("runs", None))
            return {1: {"guid": "guid-1", "run_timestamp": 123.0}}

        main_window.database_access_error = access_error
        main_window.initialise_or_create_database_at = initialise
        main_window.get_runs_via_sql = get_runs
        try:
            worker = main_window.DatabaseLoadWorker(7, "example.db")
            statuses = []
            finished = []
            worker.signals.status.connect(lambda *args: statuses.append(args))
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            main_window.database_access_error = old_access_error
            main_window.initialise_or_create_database_at = old_initialise
            main_window.get_runs_via_sql = old_get_runs

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
        old_access_error = main_window.database_access_error
        old_initialise = main_window.initialise_or_create_database_at

        main_window.database_access_error = lambda _path: "locked database"
        main_window.initialise_or_create_database_at = lambda _path: self.fail(
            "Database should not initialise after an access error"
            )
        try:
            worker = main_window.DatabaseLoadWorker(3, "locked.db")
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            main_window.database_access_error = old_access_error
            main_window.initialise_or_create_database_at = old_initialise

        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][:3], (3, "locked.db", {}))
        self.assertIsInstance(finished[0][3], RuntimeError)
        self.assertIn("locked database", str(finished[0][3]))

    def test_database_load_worker_waits_for_cloud_sync_and_retries_probe(self):
        old_access_error = main_window.database_access_error
        old_label = main_window.database_cloud_storage_label
        old_placeholder = main_window.database_is_likely_cloud_placeholder
        old_prefetch = main_window.prefetch_database_file_with_timeout
        old_initialise = main_window.initialise_or_create_database_at
        old_get_runs = main_window.get_runs_via_sql
        calls = []

        access_results = iter(["timed out", None])

        def access_error(database_path):
            calls.append(("access", database_path))
            return next(access_results)

        def prefetch(database_path, timeout=None, status_callback=None):
            calls.append(("prefetch", database_path, timeout))
            status_callback("Waiting for OneDrive sync... 100% available")
            return 10

        main_window.database_access_error = access_error
        main_window.database_cloud_storage_label = lambda _path: "OneDrive"
        main_window.database_is_likely_cloud_placeholder = lambda _path: False
        main_window.prefetch_database_file_with_timeout = prefetch
        main_window.initialise_or_create_database_at = lambda path: calls.append(
            ("initialise", path)
            )
        main_window.get_runs_via_sql = lambda: {}
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
            main_window.database_access_error = old_access_error
            main_window.database_cloud_storage_label = old_label
            main_window.database_is_likely_cloud_placeholder = old_placeholder
            main_window.prefetch_database_file_with_timeout = old_prefetch
            main_window.initialise_or_create_database_at = old_initialise
            main_window.get_runs_via_sql = old_get_runs

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


