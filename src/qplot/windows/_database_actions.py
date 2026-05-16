import os
from time import perf_counter

import numpy as np
from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw
from PyQt5.QtGui import QDesktopServices
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling import find_new_runs
from qplot.datahandling.database import (
    DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS,
    DatabaseLoadWorker,
    database_info_report,
)
from qplot.diagnostics import log_event, log_exception


class DatabaseActionsMixin:
    """
    Database loading, refresh, recent-file, and database-status actions.

    The mixin expects the owning window to provide the widgets and state created
    by MainWindow, plus show_status(), show_error(), and openPlot().
    """

    def load_startup_database(self):
        """
        Load a requested startup database, or the last database when available.

        Missing or unset paths are ignored so first-run and moved-file startup
        behaviour stays the same as an empty launch.

        """
        startup_database_path = getattr(self, "startup_database_path", None)
        if startup_database_path:
            return self.load_database_path(startup_database_path)

        try:
            last_file = self.config.get("file.last_file_path")
        except KeyError:
            return False

        if not last_file:
            return False

        last_file = os.path.abspath(last_file)
        if not os.path.isfile(last_file):
            return False

        return self.load_database_path(last_file)


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
        self.localLastFile = None

        for holder in self.dataset_holder.values():
            del_timer = holder.get("del_timer")
            if del_timer is not None:
                del_timer.stop()
        self.dataset_holder.clear()

        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
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
            report = database_info_report(database_path)
        except Exception as err:
            log_exception("Database information failed", err, __name__)
            self.show_error(
                "Database Information Failed",
                "Could not read database information.",
                str(err),
            )
            return

        box = qtw.QMessageBox(
            qtw.QMessageBox.Information,
            "Database Information",
            report,
            parent=self,
        )
        copy_button = box.addButton("Copy", qtw.QMessageBox.ActionRole)
        box.addButton(qtw.QMessageBox.Close)
        box.exec_()

        if box.clickedButton() == copy_button:
            qtw.QApplication.clipboard().setText(report)
            self.show_status("Copied database information.", 3000)


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
            newRuns = find_new_runs(self.RunList.maxTime)
            updatedRuns = self.RunList.checkWatching()
            if updatedRuns:
                self.infoBox.preview.add_runs(updatedRuns)
        except Exception as err:
            log_exception("Main-window refresh failed", err, __name__)
            self.show_error("Refresh Failed", "Could not refresh the run list.", str(err))
            return

        if not newRuns:
            self._sync_empty_state()
            if self.RunList.topLevelItemCount() == 0:
                self.show_status(self._empty_database_refresh_status(), 3000)
            else:
                self.show_status("No new runs found.", 3000)
            return

        self.RunList.maxTime = max(
            np.array(
                [subDict["run_timestamp"] for subDict in newRuns.values()],
                dtype=float,
            ),
            default=0,
        )
        self.RunList.addRuns(newRuns)
        self.infoBox.preview.add_runs(newRuns)
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
        Event handle for Open Location action in the options menu.

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
            empty_action = qtw.QAction("No Recent Databases", self)
            empty_action.setEnabled(False)
            self.recentDatabaseMenu.addAction(empty_action)
            return

        for index, path in enumerate(paths, start=1):
            label = f"{index}. {os.path.basename(path) or path}"
            action = qtw.QAction(label, self)
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

        if self._database_load_active:
            self.show_status("Wait for the current database load to finish.", 5000)
            return False

        previous_file = self.fileTextbox.text()
        previous_runs = self._current_run_metadata()
        monitorTimer = self.spinBox.value()
        load_message = f"Loading database {os.path.basename(abspath)}..."

        self.monitor.stop()

        self._database_load_generation += 1
        generation = self._database_load_generation
        self._database_load_active = True
        self._database_load_state = {
            "abspath": abspath,
            "load_started_at": load_started_at,
            "monitorTimer": monitorTimer,
            "previous_file": previous_file,
            "previous_runs": previous_runs,
        }

        self._prepare_database_load_ui(abspath)
        self._set_database_load_controls_enabled(False)
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
        Clears the main-window state for a new database load.

        """
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None

        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.scrollToTop()

        if self.fileTextbox.text() and self.fileTextbox.text() != self.localLastFile:
            self.localLastFile = self.fileTextbox.text()

        self.fileTextbox.setText(abspath)
        self._sync_empty_state()


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


    def _current_run_metadata(self):
        """
        Returns the currently displayed run metadata, if available.

        """
        all_run_metadata = getattr(self.RunList, "all_run_metadata", None)
        if not callable(all_run_metadata):
            return {}

        try:
            return all_run_metadata()
        except Exception as err:
            log_exception("Could not capture current run metadata", err, __name__)
            return {}


    def _restore_database_load_previous_state(self, state):
        """
        Restores the visible database state after a cancelled or failed load.

        """
        previous_file = state.get("previous_file", "")
        previous_runs = state.get("previous_runs") or {}

        self.fileTextbox.setText(previous_file)
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None

        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        if previous_runs:
            self.RunList.addRuns(previous_runs)
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.scrollToTop()
        self.infoBox.preview.set_database_runs(previous_file, previous_runs)
        self._sync_empty_state()


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
        Cancels the active database load and restores the previous view.

        """
        if not getattr(self, "_database_load_active", False):
            self._hide_database_load_panel()
            return

        state = self._database_load_state or {}
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()

        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)
        self._restore_database_load_previous_state(state)

        monitorTimer = state.get("monitorTimer", 0)
        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))

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

        state = self._database_load_state or {}
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)
        self._hide_database_load_panel()
        self._sync_empty_state()

        monitorTimer = state.get("monitorTimer", 0)
        load_started_at = state.get("load_started_at") or perf_counter()

        if error is not None:
            self._restore_database_load_previous_state(state)
            log_exception("Database load failed", error, __name__)
            self.show_error(
                "Database Load Failed",
                f"Could not load database {abspath}.",
                str(error),
            )
            if monitorTimer > 0:
                self.monitor.start(int(monitorTimer * 1000))
            return

        runs = runs or {}
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        self.RunList.addRuns(runs)
        self.infoBox.preview.set_database_runs(abspath, runs)
        self.select_default_run()
        self._sync_empty_state()

        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))

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
        self.RunList.scrollToItem(first_item, qtw.QAbstractItemView.PositionAtTop)


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
