import os
from time import perf_counter

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QDesktopServices
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling import find_new_runs
from qplot.datahandling.database import (
    DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS,
    DatabaseDetailWorker,
    DatabaseExpensiveDetailWorker,
    DatabaseLoadWorker,
    database_info_rows,
)
from qplot.datahandling.readonly import set_qcodes_database_location
from qplot.diagnostics import log_event, log_exception

from ._widgets.details_tables import (
    CopyableTableWidget,
    copy_to_clipboard,
    format_value,
)


class DatabaseInfoDialog(qtw.QDialog):
    """
    Copyable table dialog for current database diagnostics.

    """

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self._rows = [(format_value(label), format_value(value)) for label, value in rows]
        self.setObjectName("databaseInfoDialog")
        self.setWindowTitle("Database Information")
        self.setMinimumSize(520, 260)
        self.resize(720, 360)

        layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        content_layout = qtw.QHBoxLayout()
        content_layout.setSpacing(12)

        icon_label = qtw.QLabel(self)
        icon = self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_MessageBoxInformation)
        icon_label.setPixmap(icon.pixmap(32, 32))
        icon_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignHCenter
            | QtCore.Qt.AlignmentFlag.AlignTop
            )
        content_layout.addWidget(icon_label)

        self.table = CopyableTableWidget(self)
        self.table.setObjectName("databaseInfoTable")
        self._setup_table()
        content_layout.addWidget(self.table, 1)
        layout.addLayout(content_layout, 1)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close, self)
        self.copyButton = buttons.addButton(
            "Copy",
            qtw.QDialogButtonBox.ButtonRole.ActionRole,
            )
        self.copyButton.setObjectName("databaseInfoCopyButton")
        self.copyButton.clicked.connect(self.copyAll)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


    def _setup_table(self):
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Field", "Value"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setMinimumSectionSize(16)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.horizontalHeader().setFixedHeight(22)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.table.setWordWrap(False)
        self.table.setRowCount(len(self._rows))

        for row, (label, value) in enumerate(self._rows):
            self.table.setItem(row, 0, self._table_item(label))
            self.table.setItem(row, 1, self._table_item(value))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, 20)


    def _table_item(self, value):
        item = qtw.QTableWidgetItem(value)
        item.setToolTip(value)
        return item


    def copyAll(self):
        copy_to_clipboard(_database_info_rows_clipboard_text(self._rows))


def _database_info_rows_clipboard_text(rows):
    return "\n".join("\t".join(row) for row in rows)


class DatabaseActionsMixin:
    """
    Database loading, refresh, recent-file, and database-status actions.

    The mixin expects the owning window to provide the widgets and state created
    by MainWindow, plus show_status(), show_error(), and openPlot().
    """

    def load_startup_database(self):
        """
        Load the highest-priority available startup database.

        An explicit path is always attempted so invalid command-line paths keep
        their visible error. Missing saved paths fall back to QCoDeS' current
        database when it exists.

        """
        startup_database_path = getattr(self, "startup_database_path", None)
        if startup_database_path:
            return self.load_database_path(startup_database_path)

        try:
            last_file = self.config.get("file.last_file_path")
        except KeyError:
            last_file = None

        if last_file:
            last_file = os.path.abspath(last_file)
            if os.path.isfile(last_file):
                return self.load_database_path(last_file)

        qcodes_database = get_DB_location()
        if qcodes_database and os.path.isfile(qcodes_database):
            return self.load_database_path(qcodes_database)

        return False


    @QtCore.pyqtSlot()
    def open_database_location(self):
        """
        Opens the current database folder in the system file browser.

        """
        database_path = self.fileTextbox.text()
        if not database_path:
            self.show_status("No database is loaded.", 5000)
            return

        folder = os.path.dirname(database_path)
        if not os.path.isdir(folder):
            self.show_error(
                "Database Location Not Found",
                "The current database folder could not be found.",
                database_path,
            )
            return

        opened = QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
        if opened:
            self.show_status(f"Opened database folder: {folder}", 5000)
        else:
            self.show_error(
                "Open Folder Failed",
                "The database folder could not be opened.",
                folder,
            )


    @QtCore.pyqtSlot()
    def copy_database_path(self):
        """
        Copies the full current database path to the clipboard.

        """
        database_path = self.fileTextbox.text()
        if not database_path:
            self.show_status("No database path to copy.", 3000)
            return

        qtw.QApplication.clipboard().setText(database_path)
        self.show_status("Copied database path.", 3000)


    def close_database(self, status=True):
        """
        Clears the current database from the main window state.

        """
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()
        cancel_detail_load = getattr(self, "_cancel_database_detail_load", None)
        if callable(cancel_detail_load):
            cancel_detail_load()

        self._database_load_generation = getattr(self, "_database_load_generation", 0) + 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        if hasattr(self, "_set_database_load_controls_enabled"):
            self._set_database_load_controls_enabled(True)
        if hasattr(self, "_hide_database_load_panel"):
            self._hide_database_load_panel()

        self.monitor.stop()
        self.fileTextbox.setText("")
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None
        self._selected_dataset_key = None
        self.localLastFile = None

        for holder in self.dataset_holder.values():
            holder.cancel_delete_timer()
        self.dataset_holder.clear()

        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxRunId = 0
        self.RunList.blockSignals(False)
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.preview.set_database_runs("", {})
        self.infoBox.scrollToTop()
        self._sync_empty_state()

        if status:
            self.show_status("Database closed.", 3000)


    @QtCore.pyqtSlot()
    def show_database_info(self):
        """
        Shows a diagnostic report for the current database.

        """
        database_path = self.fileTextbox.text()
        if not database_path:
            self.show_status("No database is loaded.", 5000)
            return

        try:
            rows = database_info_rows(database_path)
        except Exception as err:
            log_exception("Database information failed", err, __name__)
            self.show_error(
                "Database Information Failed",
                "Could not read database information.",
                str(err),
            )
            return

        dialog = DatabaseInfoDialog(rows, parent=self)
        dialog.copyButton.clicked.connect(
            lambda: self.show_status("Copied database information.", 3000)
            )
        dialog.exec()


    @QtCore.pyqtSlot()
    def refreshMain(self):
        """
        On self.monitor timer or force refresh, check for new runs in Database

        """
        if not self.fileTextbox.text():
            self.show_status("Load a database before refreshing.", 5000)
            return

        self.show_status("Checking for new runs...", 0)

        try:
            newRuns = find_new_runs(self.RunList.maxRunId)
            updatedRuns = self.RunList.checkWatching()
            if updatedRuns:
                self.infoBox.preview.add_runs(updatedRuns)
                prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
                if callable(prioritize_previews):
                    prioritize_previews()
        except Exception as err:
            log_exception("Main-window refresh failed", err, __name__)
            self.show_error("Refresh Failed", "Could not refresh the run list.", str(err))
            return

        if newRuns:
            self.RunList.maxRunId = max(self.RunList.maxRunId, max(newRuns))

        if not newRuns:
            self._sync_empty_state()
            if self.RunList.topLevelItemCount() == 0:
                self.show_status(self._empty_database_refresh_status(), 3000)
            else:
                self.show_status("No new runs found.", 3000)
            return

        self.RunList.addRuns(newRuns)
        self.infoBox.preview.add_runs(newRuns)
        prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
        if callable(prioritize_previews):
            prioritize_previews()
        self._sync_empty_state()
        count = len(newRuns)
        noun = "run" if count == 1 else "runs"
        self.show_status(f"Found {count} new {noun}.", 5000)

        if self.autoPlotBox.isChecked():
            for run in newRuns.values():
                self.openPlot(run["guid"])


    @QtCore.pyqtSlot()
    def getfile(self):
        """
        Handles event for load action in file menu to load new database.

        """
        filename = qtw.QFileDialog.getOpenFileName(
            self,
            "Open file",
            self.database_open_directory(),
            "Data Base File (*.db)",
        )[0]

        if os.path.isfile(filename):
            self.load_database_path(filename)
        else:
            self.show_status("Database load cancelled.", 3000)


    def database_open_directory(self):
        """
        Returns the directory the database-open dialog should start in.

        """
        current_database = self.fileTextbox.text()
        if current_database:
            current_directory = os.path.dirname(os.path.abspath(current_database))
            if os.path.isdir(current_directory):
                return current_directory

        try:
            default_load_path = self.config.get("file.default_load_path")
        except KeyError:
            default_load_path = ""

        if os.path.isdir(default_load_path):
            return default_load_path

        return os.getcwd()


    @QtCore.pyqtSlot()
    def change_default_file(self):
        """
        Chooses the default database-open location.

        """
        if os.path.isdir(self.config.get("file.default_load_path")):
            openDir = self.config.get("file.default_load_path")
        else:
            openDir = os.getcwd()

        foldername = qtw.QFileDialog.getExistingDirectory(
            self,
            "Select Folder",
            openDir,
        )

        if os.path.isdir(foldername):
            self.config.update("file.default_load_path", foldername)
            self.show_status(f"Default load folder set to {foldername}", 5000)
        else:
            self.show_status("Default load folder unchanged.", 3000)


    @QtCore.pyqtSlot(str)
    def load_database_path(self, filename):
        """
        Load a database path chosen from the file dialog or dropped by the user.

        """
        load_started_at = perf_counter()
        log_event("Database load requested: %s", filename, logger_name=__name__)

        if not os.path.isfile(filename):
            self.show_error(
                "Database Load Failed",
                "The selected database file could not be found.",
                str(filename),
            )
            return False

        abspath = os.path.abspath(filename)
        if not abspath.lower().endswith(".db"):
            self.show_error(
                "Database Load Failed",
                "qPlot can only load QCoDeS .db database files.",
                abspath,
            )
            return False

        return self.load_file(abspath, load_started_at)


    def recent_database_paths(self):
        """
        Returns recent database paths, newest first.

        """
        try:
            paths = list(self.config.get("file.recent_file_paths"))
        except KeyError:
            paths = []

        try:
            last_file = self.config.get("file.last_file_path")
        except KeyError:
            last_file = ""

        if last_file:
            paths.insert(0, last_file)

        deduped = []
        seen = set()
        for path in paths:
            abspath = os.path.abspath(path)
            if abspath in seen:
                continue
            seen.add(abspath)
            deduped.append(abspath)

        return deduped[:10]


    def remember_recent_database(self, filename):
        """
        Stores a database path in the recent database list.

        """
        abspath = os.path.abspath(filename)
        paths = [path for path in self.recent_database_paths() if path != abspath]
        paths.insert(0, abspath)
        paths = paths[:10]

        try:
            current_paths = list(self.config.get("file.recent_file_paths"))
        except KeyError:
            current_paths = []

        if current_paths == paths:
            return

        self.config.config.setdefault("file", {})["recent_file_paths"] = paths
        self.config.save_config(self.config.default_file)
        self.refresh_recent_database_menu()


    def remember_loaded_database(self, filename):
        """
        Persists the successfully loaded database path.

        """
        abspath = os.path.abspath(filename)
        try:
            current_last_file = self.config.get("file.last_file_path")
        except KeyError:
            current_last_file = None

        try:
            if current_last_file != abspath:
                self.config.update("file.last_file_path", abspath)
            self.remember_recent_database(abspath)
        except Exception as err:
            log_exception("Remember database path failed", err, __name__)


    def refresh_recent_database_menu(self):
        """
        Rebuilds the File -> Load Recent Database menu.

        """
        if not hasattr(self, "recentDatabaseMenu"):
            return

        self.recentDatabaseMenu.clear()
        paths = self.recent_database_paths()
        self.recentDatabaseMenu.setEnabled(bool(paths))

        if not paths:
            empty_action = QtGui.QAction("No Recent Databases", self)
            empty_action.setEnabled(False)
            self.recentDatabaseMenu.addAction(empty_action)
            return

        for index, path in enumerate(paths, start=1):
            label = f"{index}. {os.path.basename(path) or path}"
            action = QtGui.QAction(label, self)
            action.setToolTip(path)
            action.setStatusTip(path)
            action.setEnabled(os.path.isfile(path))
            action.triggered.connect(
                lambda _, filename=path: self.load_database_path(filename)
            )
            self.recentDatabaseMenu.addAction(action)


    def load_file(self, abspath, load_started_at=None):
        """
        Updates the database for RunList display and loading datasets.

        """
        if load_started_at is None:
            load_started_at = perf_counter()
        log_event("Loading database file: %s", abspath, logger_name=__name__)

        if self._database_load_active:
            self.show_status("Wait for the current database load to finish.", 5000)
            return False

        if abspath == get_DB_location() and self.fileTextbox.text() == abspath:
            if not self.infoBox.preview.has_database(abspath):
                self.infoBox.preview.set_database_runs(
                    abspath,
                    self.RunList.all_run_metadata(),
                )
            elapsed = perf_counter() - load_started_at
            self.show_status(f"Database is already loaded ({elapsed:.2f} s).", 3000)
            self.remember_loaded_database(abspath)
            return True

        load_message = f"Loading database {os.path.basename(abspath)}..."

        self._database_load_generation += 1
        generation = self._database_load_generation
        self._database_load_active = True
        self._database_load_state = {
            "abspath": abspath,
            "load_started_at": load_started_at,
        }

        self._show_database_load_panel(load_message)

        try:
            cloud_sync_timeout = self.config.get("runtime_settings.cloud_sync_timeout")
        except KeyError:
            cloud_sync_timeout = DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS

        worker = DatabaseLoadWorker(generation, abspath, cloud_sync_timeout)
        self._database_load_worker = worker
        worker.signals.status.connect(self.database_load_status)
        worker.signals.finished.connect(self.database_load_finished)
        self.databaseLoadThreadPool.start(worker)
        return True


    def _prepare_database_load_ui(self, abspath):
        """
        Replaces the main-window state with a successfully loaded database.

        """
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None
        self._selected_dataset_key = None

        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxRunId = 0
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.scrollToTop()

        if self.fileTextbox.text() and self.fileTextbox.text() != self.localLastFile:
            self.localLastFile = self.fileTextbox.text()

        self.fileTextbox.setText(abspath)


    def _set_database_load_controls_enabled(self, enabled):
        """
        Enables or disables controls that start overlapping database actions.

        """
        for attr in (
            "loadDatabaseButton",
            "refreshDatabaseButton",
            "databaseInfoButton",
            "openDatabaseFolderButton",
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setEnabled(enabled)


    def _show_database_load_panel(self, message):
        """
        Shows the inline database-load progress panel.

        """
        if hasattr(self, "databaseLoadLabel"):
            self.databaseLoadLabel.setText(message)
            self.databaseLoadLabel.setToolTip(message)
        if hasattr(self, "databaseLoadFrame"):
            self.databaseLoadFrame.setVisible(True)
        self.show_status(message, 0)


    def _hide_database_load_panel(self):
        """
        Hides the inline database-load progress panel.

        """
        if hasattr(self, "databaseLoadLabel"):
            self.databaseLoadLabel.setText("")
            self.databaseLoadLabel.setToolTip("")
        if hasattr(self, "databaseLoadFrame"):
            self.databaseLoadFrame.setVisible(False)


    @QtCore.pyqtSlot()
    def cancel_database_load(self):
        """
        Cancels the pending database load without changing the current view.

        """
        if not getattr(self, "_database_load_active", False):
            self._hide_database_load_panel()
            return

        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()

        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)

        self._hide_database_load_panel()
        self.show_status("Database load cancelled.", 3000)


    @QtCore.pyqtSlot(int, str)
    def database_load_status(self, generation, message):
        """
        Shows progress from the active database load.

        """
        if generation != self._database_load_generation or not self._database_load_active:
            return

        self._show_database_load_panel(message)


    @QtCore.pyqtSlot(int, str, object, object)
    def database_load_finished(self, generation, abspath, runs, error):
        """
        Applies the background database load result on the GUI thread.

        """
        if generation != self._database_load_generation:
            return
        if not getattr(self, "_database_load_active", False):
            return

        state = self._database_load_state or {}
        if state.get("abspath") != abspath:
            return

        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)
        self._hide_database_load_panel()
        load_started_at = state.get("load_started_at") or perf_counter()

        if error is not None:
            log_exception("Database load failed", error, __name__)
            self.show_error(
                "Database Load Failed",
                f"Could not load database {abspath}.",
                str(error),
            )
            return

        self._cancel_database_detail_load()
        set_qcodes_database_location(abspath)
        runs = runs or {}
        run_id_signals_blocked = self.run_idBox.blockSignals(True)
        run_list_signals_blocked = self.RunList.blockSignals(True)
        try:
            self._prepare_database_load_ui(abspath)
            self.RunList.addRuns(runs)
            self.infoBox.preview.set_database_runs(abspath, runs)
        finally:
            self.RunList.blockSignals(run_list_signals_blocked)
            self.run_idBox.blockSignals(run_id_signals_blocked)
        self.select_default_run()
        prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
        if callable(prioritize_previews):
            prioritize_previews()
        self._sync_empty_state()
        apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
        if callable(apply_refresh_interval):
            apply_refresh_interval(self._current_refresh_interval())

        elapsed = perf_counter() - load_started_at
        self.remember_loaded_database(abspath)
        run_count = self.RunList.topLevelItemCount()
        if run_count == 0:
            status = self._loaded_empty_database_status(abspath, elapsed)
        else:
            status = (
                f"Loaded {run_count} runs from "
                f"{os.path.basename(abspath)} in {elapsed:.2f} s."
            )
        self.show_status(status, 5000)
        log_event(
            "Loaded %s runs from %s in %.2f s",
            run_count,
            abspath,
            elapsed,
            logger_name=__name__,
        )
        self._start_database_detail_load(abspath, runs)


    def _cancel_database_detail_load(self):
        worker = getattr(self, "_database_detail_worker", None)
        if worker is not None:
            worker.cancel()
        expensive_worker = getattr(self, "_database_expensive_detail_worker", None)
        if expensive_worker is not None:
            expensive_worker.cancel()

        self._database_detail_generation = (
            getattr(self, "_database_detail_generation", 0) + 1
            )
        self._database_detail_active = False
        self._database_detail_worker = None
        self._database_expensive_detail_generation = (
            getattr(self, "_database_expensive_detail_generation", 0) + 1
            )
        self._database_expensive_detail_active = False
        self._database_expensive_detail_worker = None


    def _start_database_detail_load(self, abspath, runs):
        run_ids = self._database_detail_run_order(runs)
        if not run_ids:
            return

        self._database_detail_generation = (
            getattr(self, "_database_detail_generation", 0) + 1
            )
        generation = self._database_detail_generation
        self._database_detail_active = True
        self._database_expensive_detail_generation = (
            getattr(self, "_database_expensive_detail_generation", 0) + 1
            )
        expensive_generation = self._database_expensive_detail_generation
        self._database_expensive_detail_active = True

        worker = DatabaseDetailWorker(generation, abspath, run_ids, batch_size=10)
        self._database_detail_worker = worker
        priority_run_ids = self._database_detail_priority_run_ids()
        worker.prioritize_run_ids(priority_run_ids)
        worker.signals.status.connect(self.database_detail_status)
        worker.signals.batch_ready.connect(self.database_detail_batch_ready)
        worker.signals.finished.connect(self.database_detail_finished)
        thread_pool = getattr(
            self,
            "databaseDetailThreadPool",
            self.databaseLoadThreadPool,
            )
        thread_pool.start(worker)

        expensive_worker = DatabaseExpensiveDetailWorker(
            expensive_generation,
            abspath,
            run_ids,
            batch_size=10,
            )
        self._database_expensive_detail_worker = expensive_worker
        expensive_worker.prioritize_run_ids(priority_run_ids)
        expensive_worker.signals.status.connect(self.database_expensive_detail_status)
        expensive_worker.signals.batch_ready.connect(
            self.database_expensive_detail_batch_ready
            )
        expensive_worker.signals.finished.connect(
            self.database_expensive_detail_finished
            )
        expensive_thread_pool = getattr(
            self,
            "databaseExpensiveDetailThreadPool",
            thread_pool,
            )
        expensive_thread_pool.start(expensive_worker)
        QtCore.QTimer.singleShot(0, self._prioritize_database_detail_runs)


    def _database_detail_run_order(self, runs):
        def sort_key(run_id):
            try:
                return int(run_id)
            except (TypeError, ValueError):
                return 0

        return sorted((runs or {}).keys(), key=sort_key, reverse=True)


    def _prioritize_database_detail_runs(self, run_ids=None):
        if not (
                getattr(self, "_database_detail_active", False)
                or getattr(self, "_database_expensive_detail_active", False)
                ):
            return

        priority_run_ids = self._database_detail_priority_run_ids(run_ids=run_ids)
        for active_attr, worker_attr in (
                ("_database_detail_active", "_database_detail_worker"),
                (
                    "_database_expensive_detail_active",
                    "_database_expensive_detail_worker",
                    ),
                ):
            if not getattr(self, active_attr, False):
                continue

            worker = getattr(self, worker_attr, None)
            if worker is None or not hasattr(worker, "prioritize_run_ids"):
                continue

            worker.prioritize_run_ids(priority_run_ids)


    def _database_detail_priority_run_ids(self, run_ids=None):
        priority_ids = []
        seen = set()

        def add(candidate):
            if candidate is None:
                return
            try:
                key = int(candidate)
            except (TypeError, ValueError):
                key = candidate
            if key in seen:
                return
            priority_ids.append(candidate)
            seen.add(key)

        if isinstance(run_ids, (list, tuple, set)):
            for run_id in run_ids:
                add(run_id)
        else:
            add(run_ids)

        run_list = getattr(self, "RunList", None)
        selected_run_ids = getattr(run_list, "selected_run_ids", None)
        if callable(selected_run_ids):
            for run_id in selected_run_ids():
                add(run_id)

        visible_run_ids = getattr(run_list, "visible_run_ids", None)
        if callable(visible_run_ids):
            for run_id in visible_run_ids():
                add(run_id)

        return priority_ids


    @QtCore.pyqtSlot(int, str)
    def database_detail_status(self, generation, message):
        if generation != getattr(self, "_database_detail_generation", 0):
            return
        if not getattr(self, "_database_detail_active", False):
            return

        self.show_status(message, 0)


    @QtCore.pyqtSlot(int, str, object)
    def database_detail_batch_ready(self, generation, abspath, runs):
        if generation != getattr(self, "_database_detail_generation", 0):
            return
        if not getattr(self, "_database_detail_active", False):
            return
        if abspath != self.fileTextbox.text():
            return

        self._apply_database_detail_batch(runs)


    @QtCore.pyqtSlot(int, str)
    def database_expensive_detail_status(self, generation, message):
        if generation != getattr(self, "_database_expensive_detail_generation", 0):
            return
        if not getattr(self, "_database_expensive_detail_active", False):
            return

        self.show_status(message, 0)


    @QtCore.pyqtSlot(int, str, object)
    def database_expensive_detail_batch_ready(self, generation, abspath, runs):
        if generation != getattr(self, "_database_expensive_detail_generation", 0):
            return
        if not getattr(self, "_database_expensive_detail_active", False):
            return
        if abspath != self.fileTextbox.text():
            return

        self._apply_database_detail_batch(runs)


    def _apply_database_detail_batch(self, runs):
        updated_runs = self.RunList.updateRuns(runs)
        if not updated_runs:
            return

        self.infoBox.preview.add_runs(updated_runs, queue_previews=False)
        prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
        if callable(prioritize_previews):
            prioritize_previews()
        self._refresh_selected_run_details(updated_runs)


    @QtCore.pyqtSlot(int, str, object)
    def database_detail_finished(self, generation, abspath, error):
        if generation != getattr(self, "_database_detail_generation", 0):
            return

        self._database_detail_active = False
        self._database_detail_worker = None

        if abspath != self.fileTextbox.text():
            return

        if error is not None:
            log_exception("Database detail load failed", error, __name__)
            self.show_status(f"Run detail loading failed: {error}", 5000)
            return

        if not getattr(self, "_database_expensive_detail_active", False):
            self.show_status("Run details loaded.", 5000)


    @QtCore.pyqtSlot(int, str, object)
    def database_expensive_detail_finished(self, generation, abspath, error):
        if generation != getattr(self, "_database_expensive_detail_generation", 0):
            return

        self._database_expensive_detail_active = False
        self._database_expensive_detail_worker = None

        if abspath != self.fileTextbox.text():
            return

        if error is not None:
            log_exception("Expensive database detail load failed", error, __name__)
            self.show_status(f"Setpoint and size loading failed: {error}", 5000)
            return

        if not getattr(self, "_database_detail_active", False):
            self.show_status("Run details loaded.", 5000)


    def _refresh_selected_run_details(self, runs):
        guid = getattr(getattr(self, "ds", None), "guid", None)
        if not guid:
            return

        for metadata in runs.values():
            if metadata.get("guid") == guid:
                self.updateSelected(guid)
                return


    def select_default_run(self):
        """
        Select the first visible run so the details pane is not left empty.

        """
        if self.RunList.topLevelItemCount() == 0:
            return

        first_item = self.RunList.topLevelItem(0)
        if first_item is None:
            return

        self.RunList.setCurrentItem(first_item)
        self.RunList.scrollToItem(first_item, qtw.QAbstractItemView.ScrollHint.PositionAtTop)


    def _loaded_empty_database_status(self, abspath, elapsed):
        basename = os.path.basename(abspath)
        if self._main_refresh_interval() > 0:
            return (
                f"Loaded empty database {basename} in {elapsed:.2f} s; "
                "waiting for measurements."
            )

        return (
            f"Loaded empty database {basename} in {elapsed:.2f} s; "
            "refresh manually to check for measurements."
        )


    def _empty_database_refresh_status(self):
        if self._main_refresh_interval() > 0:
            return "No measurements found yet; still waiting for new runs."
        return "No measurements found yet."


    def _main_refresh_interval(self):
        current_refresh_interval = getattr(self, "_current_refresh_interval", None)
        if callable(current_refresh_interval):
            return current_refresh_interval()
        return 0.0
