import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QDesktopServices
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling.database import (
    DatabaseDetailWorker,
    DatabaseExpensiveDetailWorker,
    DatabaseLoadWorker,
    DatabaseRefreshWorker,
    DatabaseSelectedRunWorker,
    database_info_rows,
)
from qplot.datahandling.file_identity import (
    DatabaseInstance,
    database_instance,
    database_instances_differ,
    database_publication_guard_path,
    logical_database_path,
)
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    UnverifiableDatabaseWalError,
    quarantine_wal_for_replaced_database,
    replacement_wal_is_quarantined,
    set_qcodes_database_location,
)
from qplot.datahandling.trusted_live_service import (
    SNAPSHOT_FALLBACK_MODE,
    TRUSTED_LIVE_MODE,
)
from qplot.datahandling.trusted_presentation import bounded_presentation_error
from qplot.diagnostics import log_event, log_exception
from qplot.testdata import (
    GenerationCancelled,
    generate_database,
    instruction_collection_contents,
    read_specifications,
    write_example_csv,
)

from ._config_persistence import persist_config_value
from ._dataset_handle import (
    canonical_database_path,
    database_file_identity,
)
from ._export_paths import (
    choose_export_path,
    prepare_export_destination,
    write_export_atomically,
)
from ._widgets.details_tables import (
    CopyableTableWidget,
    copy_to_clipboard,
    format_value,
)

_database_file_identity = database_file_identity


class _DatabaseLoadPublicationAborted(RuntimeError):
    """A nested Qt event invalidated an in-progress UI publication."""


class _DatabaseRefreshPublicationAborted(RuntimeError):
    """A nested Qt event invalidated an in-progress refresh publication."""


def _database_instances_differ(first, second):
    """Return whether two available identities name different file instances."""
    return first is not None and second is not None and first != second


def _database_paths_equal(first, second):
    """Compare database paths using the platform's logical path spelling."""
    if not first or not second:
        return False
    try:
        return logical_database_path(first) == logical_database_path(second)
    except (TypeError, ValueError, OSError):
        return False


def _database_observations_differ(first, second):
    """Compare saved observations, retaining compatibility with test harnesses."""

    if isinstance(first, DatabaseInstance) and isinstance(second, DatabaseInstance):
        return database_instances_differ(first, second)
    first_identity = getattr(first, "identity", first)
    second_identity = getattr(second, "identity", second)
    return _database_instances_differ(first_identity, second_identity)


def _database_instances_or_sidecars_differ(first, second):
    """Reject main/sidecar replacement while allowing later sidecar creation.

    Stage 3 permits a writer or SQLite itself to create the exact WAL/SHM
    sidecars between finite reader operations.  Comparisons are directional:
    ``first`` is the accepted observation and ``second`` is a later one.  A
    newly observed identity is therefore compatible, while removal or
    replacement of any identity already bound to the accepted source is not.
    """
    if not isinstance(first, DatabaseInstance) or not isinstance(
            second,
            DatabaseInstance,
            ):
        return _database_observations_differ(first, second)
    if database_instances_differ(first, second):
        return True
    return not first.sidecar_identities.issubset(second.sidecar_identities)


def _selected_run_detail_cache_key(instance, run_id, guid):
    """Return a sidecar-aware immutable key for one plain detail view."""
    if not isinstance(instance, DatabaseInstance):
        return None
    try:
        run_id = int(run_id)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        instance.logical_path,
        instance.resolved_path,
        instance.identity,
        frozenset(instance.sidecar_identities),
        run_id,
        str(guid),
    )


def _database_publication_is_guarded(database_path):
    """Fail closed if publication is in flight or its guard cannot be inspected."""
    try:
        database_publication_guard_path(database_path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


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


def reveal_file_in_file_manager(file_path):
    """Reveal a generated file in Finder, or open its folder elsewhere."""
    file_path = os.path.abspath(file_path)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["open", "-R", file_path],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    folder = os.path.dirname(file_path)
    return QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))


def _write_export_bytes(filename, content):
    """Write one private staging file for the shared export transaction."""
    with open(filename, "wb") as output_file:
        output_file.write(content)
    return True


class TestDatabaseGenerationSignals(QtCore.QObject):
    """Signals emitted by background test-database generation."""

    finished = QtCore.pyqtSignal(str, object, object, bool)


@dataclass(slots=True)
class _TestDatabaseReplacementState:
    """Controller state retained while a loaded database may be replaced."""

    database_path: str
    original_instance: DatabaseInstance
    selected_guid: str | None
    selected_run_id: int | str | None
    run_id_text: str
    measurement_text: str
    details_tab_index: int | None
    monitor_was_active: bool
    monitor_interval_ms: int
    outcome: str | None = None


class TestDatabaseGenerationWorker(QtCore.QRunnable):
    """Generate a test database without blocking the GUI thread."""

    def __init__(self, specifications, database_path, overwrite=False):
        super().__init__()
        self.signals = TestDatabaseGenerationSignals()
        self.specifications = list(specifications)
        self.database_path = str(database_path)
        self.overwrite = overwrite
        self._cancelled = threading.Event()
        self._publication_completed = threading.Event()

    def cancel(self):
        """Request cooperative cancellation and temporary-file cleanup."""
        self._cancelled.set()

    def run(self):
        error = None
        try:
            generate_database(
                self.specifications,
                self.database_path,
                overwrite=self.overwrite,
                cancelled_callback=self._cancelled.is_set,
                publication_callback=self._publication_completed.set,
            )
        except GenerationCancelled as err:
            error = err
        except Exception as err:
            error = err
            log_exception("Test database generation failed", err, __name__)

        try:
            self.signals.finished.emit(
                self.database_path,
                self.specifications,
                error,
                self._publication_completed.is_set(),
            )
        except RuntimeError as err:
            if "wrapped C/C++ object" not in str(err):
                raise


class DatabaseActionsMixin:
    """
    Database loading, refresh, recent-file, and database-status actions.

    The mixin expects the owning window to provide the widgets and state created
    by MainWindow, plus show_status(), show_error(), and openPlot().
    """

    _database_refresh_worker: DatabaseRefreshWorker | None
    _database_refresh_instance: DatabaseInstance | None
    _test_database_generation_worker: TestDatabaseGenerationWorker | None

    def _reap_retired_trusted_read_services(self):
        """Zero-wait reap retired brokers and drive polling only while needed."""
        retired = getattr(self, "_retired_trusted_read_services", None)
        diagnostics = getattr(self, "_retired_service_reap_diagnostics", None)
        if diagnostics is None:
            diagnostics = {}
            self._retired_service_reap_diagnostics = diagnostics
        if retired is not None:
            for service in tuple(retired):
                wait_closed = getattr(service, "wait_closed", None)
                if not callable(wait_closed):
                    continue
                try:
                    closed = wait_closed(0)
                except BaseException as error:
                    diagnostics[id(service)] = (
                        "retired service zero-wait reap raised "
                        f"{type(error).__name__}: {error}"
                    )
                    continue
                diagnostics.pop(id(service), None)
                if not closed:
                    continue
                retired.discard(service)
        reaper_timer = getattr(self, "_retired_service_reaper_timer", None)
        if reaper_timer is None:
            return
        if retired:
            is_active = getattr(reaper_timer, "isActive", None)
            if not callable(is_active) or not is_active():
                reaper_timer.start()
        else:
            reaper_timer.stop()


    def _retire_trusted_read_service(self, service, *, force=False):
        """Begin prompt off-GUI shutdown while retaining the service object."""
        if service is None:
            return
        if (
                not force
                and service is getattr(self, "_trusted_read_service", None)
                ):
            return
        retired = getattr(self, "_retired_trusted_read_services", None)
        if retired is None:
            retired = set()
            self._retired_trusted_read_services = retired
        already_retiring = service in retired
        retired.add(service)
        if not already_retiring:
            service.close_async()
        DatabaseActionsMixin._reap_retired_trusted_read_services(self)


    def _retire_all_trusted_read_services(self):
        """Invalidate active and pending sessions without blocking Qt."""
        active = getattr(self, "_trusted_read_service", None)
        self._trusted_read_service = None
        pending = getattr(self, "_pending_trusted_read_services", {})
        self._pending_trusted_read_services = {}
        for service in {active, *pending.values()}:
            if service is not None:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    service,
                    force=True,
                )
        self._database_access_mode = None
        self._database_fallback_reason = None


    def _active_trusted_service_for_instance(self, instance=None):
        if getattr(self, "_database_access_mode", None) != TRUSTED_LIVE_MODE:
            return None
        service = getattr(self, "_trusted_read_service", None)
        if (
                service is None
                or getattr(service, "closing", False)
                or getattr(service, "closed", False)
                ):
            return None
        if instance is None:
            instance = getattr(self, "_loaded_database_instance", None)
        if not isinstance(instance, DatabaseInstance):
            return None
        try:
            service_instance = service.database_instance
        except Exception:
            return None
        # Stage 3 reports its accepted source before SQLite opens it. The exact
        # permitted ``-shm`` may therefore appear after this observation, so a
        # service-to-UI comparison can bind only the main database instance.
        if database_instances_differ(service_instance, instance):
            return None
        return service


    def _advance_loaded_database_sidecar_baseline(self, current_instance):
        """Retain compatible sidecars so a later swap cannot look like creation.

        The first WAL/SHM observation after acceptance is allowed, but it must
        immediately become part of the controller's accepted baseline.  Every
        later guard can then distinguish another legitimate addition from the
        removal or replacement of a sidecar already observed for this view.
        """
        loaded_instance = getattr(self, "_loaded_database_instance", None)
        if not isinstance(loaded_instance, DatabaseInstance) or not isinstance(
                current_instance,
                DatabaseInstance,
                ):
            return False
        if _database_instances_or_sidecars_differ(
                loaded_instance,
                current_instance,
                ):
            return False
        if loaded_instance.sidecar_identities != current_instance.sidecar_identities:
            self._loaded_database_instance = current_instance
            self._loaded_database_identity = current_instance.identity
        return True


    def _cancel_selected_run_detail(self):
        worker = getattr(self, "_database_selected_run_worker", None)
        if worker is not None:
            worker.cancel()
        self._database_selected_run_generation = (
            getattr(self, "_database_selected_run_generation", 0) + 1
        )
        self._database_selected_run_worker = None
        self._database_selected_run_instance = None
        self._database_selected_run_mode = None

    def _database_generation_transaction_blocks_path(self, database_path=None):
        """Return whether a publication transaction blocks database actions.

        A database-load publication is path-independent while its nested Qt
        event yields stage a new run model.  The test-database transaction
        remains owned while the released view is being recovered, and while an
        ambiguous publication guard prevents safe recovery.  Comparing both
        the selected logical path and the accepted resolved source keeps aliases
        of the owned database behind the same gate without touching SQLite.
        """
        if (
                getattr(self, "_database_load_publication_active", False)
                or getattr(self, "_database_refresh_publication_active", False)
                ):
            return True
        state = getattr(self, "_test_database_replacement_state", None)
        if state is None:
            return False

        if database_path is None:
            file_textbox = getattr(self, "fileTextbox", None)
            database_path = file_textbox.text() if file_textbox is not None else ""
        if not database_path:
            return False

        owned_path = getattr(state, "database_path", "")
        if not owned_path:
            return False
        if logical_database_path(database_path) == logical_database_path(owned_path):
            return True

        candidate_resolved = canonical_database_path(database_path)
        original_instance = getattr(state, "original_instance", None)
        original_resolved = getattr(original_instance, "resolved_path", None)
        return bool(
            candidate_resolved == canonical_database_path(owned_path)
            or (
                original_resolved
                and candidate_resolved == canonical_database_path(original_resolved)
            )
        )


    def _database_generation_read_allowed(
            self,
            database_path=None,
            *,
            operation="accessing the database",
            notify=True,
            ):
        """Central gate for actions that could acquire a database consumer."""
        blocked = DatabaseActionsMixin._database_generation_transaction_blocks_path(
            self,
            database_path,
        )
        if blocked and notify:
            if (
                    getattr(self, "_database_load_publication_active", False)
                    or getattr(self, "_database_refresh_publication_active", False)
                    ):
                message = (
                    "Wait for the current database view to finish loading before "
                    f"{operation}."
                )
            else:
                message = (
                    "Wait for test-database generation to finish before "
                    f"{operation}."
                )
            self.show_status(message, 5000)
        return not blocked


    def _sync_database_generation_controls(self):
        """Keep read-producing controls aligned with the path transaction."""
        blocked = DatabaseActionsMixin._database_generation_transaction_blocks_path(self)
        load_active = bool(getattr(self, "_database_load_active", False))
        enabled = not blocked and not load_active
        for attr in (
            "refreshDatabaseButton",
            "databaseInfoButton",
            "emptyStateRefreshButton",
            "plotRunButton",
            "exportCsvButton",
            "run_idBox",
            "measurementBox",
            "RunList",
            "infoBox",
            "autoPlotBox",
        ):
            widget = getattr(self, attr, None)
            set_enabled = getattr(widget, "setEnabled", None)
            if callable(set_enabled):
                set_enabled(enabled)

        generation_action = getattr(self, "generateTestDatabaseAction", None)
        set_generation_enabled = getattr(generation_action, "setEnabled", None)
        if callable(set_generation_enabled):
            set_generation_enabled(
                not blocked
                and not getattr(self, "_test_database_generation_active", False)
                and getattr(self, "_test_database_replacement_state", None) is None
                and not load_active
            )


    def _clear_test_database_generation_transaction(self):
        """Release the path gate after recovery or a newer view takes over."""
        self._test_database_replacement_state = None
        self._database_view_released_for_generation = False
        DatabaseActionsMixin._sync_database_generation_controls(self)


    def _pending_database_load_targets_another_path(self, database_path):
        if not getattr(self, "_database_load_active", False):
            return False
        state = getattr(self, "_database_load_state", None) or {}
        pending_path = state.get("abspath")
        return bool(
            pending_path
            and logical_database_path(pending_path)
            != logical_database_path(database_path)
        )


    def _resume_test_database_generation_recovery(self):
        """Resume a deferred terminal recovery after an unrelated load ends."""
        state = getattr(self, "_test_database_replacement_state", None)
        if state is None or getattr(self, "_test_database_generation_active", False):
            return False
        database_path = getattr(state, "database_path", "")
        if not database_path or not (
                DatabaseActionsMixin._database_generation_transaction_blocks_path(
                    self,
                    database_path,
                )
                ):
            return False

        file_textbox = getattr(self, "fileTextbox", None)
        current_path = file_textbox.text() if file_textbox is not None else ""
        if (
                current_path
                and not (
                    DatabaseActionsMixin._database_generation_transaction_blocks_path(
                        self,
                        current_path,
                    )
                )
                ):
            DatabaseActionsMixin._clear_test_database_generation_transaction(self)
            return False
        if DatabaseActionsMixin._pending_database_load_targets_another_path(
                self,
                database_path,
                ):
            return False

        outcome = getattr(state, "outcome", None)
        if outcome == "replacement":
            return self._reload_replaced_database(
                database_path,
                generation_recovery=True,
            )
        if outcome == "recovering-original":
            return self.load_file(
                database_path,
                force=True,
                generation_recovery=True,
            )
        return False

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

        recent_files = self.config.get("file.recent_file_paths")
        if recent_files:
            recent_file = os.path.abspath(recent_files[0])
            if os.path.isfile(recent_file):
                return self.load_database_path(recent_file)

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
    def create_test_database_csv(self):
        """Create an example test-data CSV and open its folder."""
        suggested_path = os.path.join(
            self.database_open_directory(),
            "qplot-test-runs.csv",
        )
        try:
            destination = choose_export_path(
                self,
                caption="Create Test Database CSV",
                suggested_path=suggested_path,
                name_filter="CSV Files (*.csv)",
                required_suffix=".csv",
                replace_title="Replace Example CSV?",
                file_description="example CSV file",
            )
            if destination is None:
                self.show_status("Example CSV creation cancelled.", 3000)
                return False
            write_export_atomically(
                destination,
                lambda temporary: write_example_csv(temporary, overwrite=True),
            )
        except Exception as err:
            log_exception("Example test-data CSV creation failed", err, __name__)
            self.show_error(
                "Example CSV Creation Failed",
                "Could not create the example test-database CSV.",
                str(err),
            )
            return False

        csv_path = destination.filename
        opened = reveal_file_in_file_manager(csv_path)
        if not opened:
            self.show_error(
                "Open CSV Folder Failed",
                "The example CSV was created, but its folder could not be opened.",
                csv_path,
            )
            return True

        self.show_status(f"Created example CSV and opened its folder: {csv_path}", 5000)
        return True


    @QtCore.pyqtSlot()
    def export_test_database_csv_collection(self):
        """Export the installed cumulative CSV collection and open its folder."""
        directory = qtw.QFileDialog.getExistingDirectory(
            self,
            "Export Test Database CSV Collection",
            self.database_open_directory(),
        )
        if not directory:
            self.show_status("CSV collection export cancelled.", 3000)
            return False

        directory = os.path.abspath(directory)
        try:
            collection = instruction_collection_contents()
            destinations = tuple(
                prepare_export_destination(
                    self,
                    os.path.join(directory, filename),
                )
                for filename, _content in collection
            )
            for destination, (_filename, content) in zip(
                    destinations,
                    collection,
                    strict=True,
                    ):
                write_export_atomically(
                    destination,
                    lambda temporary, data=content: (
                        _write_export_bytes(temporary, data)
                    ),
                )
            output_paths = tuple(
                Path(destination.filename) for destination in destinations
            )
        except Exception as err:
            log_exception("Test-data CSV collection export failed", err, __name__)
            self.show_error(
                "CSV Collection Export Failed",
                "Could not export the test-database CSV collection.",
                str(err),
            )
            return False

        opened = reveal_file_in_file_manager(output_paths[0])
        if not opened:
            self.show_error(
                "Open CSV Folder Failed",
                "The CSV collection was exported, but its folder could not be opened.",
                directory,
            )
            return True

        self.show_status(
            f"Exported {len(output_paths)} instruction CSV files and opened their "
            f"folder: {directory}",
            5000,
        )
        return True


    @QtCore.pyqtSlot()
    def generate_test_database_from_csv(self):
        """Choose a CSV and generate its QCoDeS database in the background."""
        if getattr(self, "_database_load_publication_active", False):
            self.show_status(
                "Wait for the current database view to finish loading before "
                "generating a test database.",
                5000,
            )
            return False
        if getattr(self, "_test_database_generation_active", False):
            self.show_status("A test database is already being generated.", 5000)
            return False
        if (
                getattr(self, "_database_load_active", False)
                or getattr(self, "_database_view_released_for_generation", False)
                ):
            self.show_status(
                "Wait for the current database recovery to finish.",
                5000,
            )
            return False

        csv_path = qtw.QFileDialog.getOpenFileName(
            self,
            "Select Test Database CSV",
            self.database_open_directory(),
            "CSV Files (*.csv)",
        )[0]
        if not csv_path:
            self.show_status("Test database generation cancelled.", 3000)
            return False

        try:
            specifications = read_specifications(csv_path)
        except Exception as err:
            log_exception("Test-data CSV validation failed", err, __name__)
            self.show_error(
                "Invalid Test Database CSV",
                "The selected CSV could not be used to generate a database.",
                str(err),
            )
            return False

        suggested_path = str(os.path.splitext(os.path.abspath(csv_path))[0] + ".db")
        database_path = qtw.QFileDialog.getSaveFileName(
            self,
            "Save Test Database",
            suggested_path,
            "QCoDeS Database (*.db)",
            options=qtw.QFileDialog.Option.DontConfirmOverwrite,
        )[0]
        if not database_path:
            self.show_status("Test database generation cancelled.", 3000)
            return False
        if not database_path.lower().endswith(".db"):
            database_path += ".db"
        database_path = os.path.abspath(database_path)
        overwrite = os.path.exists(database_path)
        if overwrite:
            reply = qtw.QMessageBox.question(
                self,
                "Replace Test Database?",
                f"{database_path} already exists.\n\nReplace it with the "
                "generated test database?",
                qtw.QMessageBox.StandardButton.Yes
                | qtw.QMessageBox.StandardButton.No,
                qtw.QMessageBox.StandardButton.No,
            )
            if reply != qtw.QMessageBox.StandardButton.Yes:
                self.show_status("Test database generation cancelled.", 3000)
                return False

        file_textbox = getattr(self, "fileTextbox", None)
        current_database = file_textbox.text() if file_textbox is not None else ""
        if (
                current_database
                and canonical_database_path(current_database)
                == canonical_database_path(database_path)
                ):
            # Capture the accepted source before releasing its handles. The
            # worker stages elsewhere, so this is only a reversible release:
            # the terminal callback alone may confirm replacement/quarantine.
            prepare_replacement = getattr(
                self,
                "_prepare_test_database_replacement",
                None,
            )
            if callable(prepare_replacement):
                prepare_replacement(database_path)

        self._test_database_generation_active = True
        DatabaseActionsMixin._sync_database_generation_controls(self)
        worker = TestDatabaseGenerationWorker(
            specifications,
            database_path,
            overwrite=overwrite,
        )
        self._test_database_generation_worker = worker
        worker.signals.finished.connect(self.test_database_generation_finished)
        self.show_status(
            f"Generating test database {os.path.basename(database_path)}...",
            0,
        )
        self.testDatabaseGenerationThreadPool.start(worker)
        return True


    @QtCore.pyqtSlot(str, object, object, bool)
    def test_database_generation_finished(
            self,
            database_path,
            specifications,
            error,
            publication_completed=False,
            ):
        """Apply the result of background test-database generation."""
        self._test_database_generation_active = False
        self._test_database_generation_worker = None

        shutting_down = bool(
            getattr(self, "_shutdown_started", False)
            or getattr(self, "_shutdown_ready", False)
        )
        if shutting_down:
            DatabaseActionsMixin._clear_test_database_generation_transaction(self)
            return

        cancelled = isinstance(error, GenerationCancelled)
        if cancelled:
            self.show_status("Test database generation cancelled.", 3000)
        elif error is not None:
            self.show_error(
                "Test Database Generation Failed",
                "Could not generate the test database.",
                str(error),
            )

        if error is None:
            run_count = len(specifications)
            point_count = sum(
                specification.point_count for specification in specifications
            )
            run_word = "run" if run_count == 1 else "runs"
            self.show_status(
                f"Generated {os.path.basename(database_path)} with {run_count} "
                f"{run_word} and {point_count} points.",
                7000,
            )

        replacement_state = getattr(
            self,
            "_test_database_replacement_state",
            None,
        )
        state_matches = bool(
            replacement_state is not None
            and logical_database_path(replacement_state.database_path)
            == logical_database_path(database_path)
        )
        file_textbox = getattr(self, "fileTextbox", None)
        current_database = file_textbox.text() if file_textbox is not None else ""
        current_matches = bool(
            current_database
            and (
                DatabaseActionsMixin._database_generation_transaction_blocks_path(
                    self,
                    current_database,
                )
                if state_matches
                else canonical_database_path(current_database)
                == canonical_database_path(database_path)
            )
        )
        pending_unrelated_load = (
            DatabaseActionsMixin._pending_database_load_targets_another_path(
                self,
                database_path,
            )
        )
        if not state_matches and not current_matches:
            DatabaseActionsMixin._sync_database_generation_controls(self)
            return

        instance_changed = False
        if state_matches:
            current_instance = database_instance(database_path)
            instance_changed = _database_observations_differ(
                replacement_state.original_instance,
                current_instance,
            )

        replacement_confirmed = bool(
            error is None or publication_completed or instance_changed
        )
        guarded = _database_publication_is_guarded(database_path)
        if replacement_confirmed:
            # This transition is intentionally the first quarantine point.
            # It covers success as well as an exception raised after commit.
            quarantine_wal_for_replaced_database(database_path)
            if state_matches:
                replacement_state.outcome = "replacement"
            if guarded:
                if state_matches:
                    replacement_state.outcome = "ambiguous"
                DatabaseActionsMixin._sync_database_generation_controls(self)
                return
            if state_matches and (not current_matches or pending_unrelated_load):
                if not current_matches and not pending_unrelated_load:
                    DatabaseActionsMixin._clear_test_database_generation_transaction(
                        self
                    )
                else:
                    DatabaseActionsMixin._sync_database_generation_controls(self)
                return
            if current_matches:
                if state_matches:
                    self._reload_replaced_database(
                        database_path,
                        generation_recovery=True,
                    )
                else:
                    self._reload_replaced_database(database_path)
            else:
                DatabaseActionsMixin._sync_database_generation_controls(self)
            return

        if guarded:
            # The publisher retained a guard because it could not prove a safe
            # rollback. Keep the released view invalid and do not guess which
            # main any sidecar belongs to.
            if state_matches:
                replacement_state.outcome = "ambiguous"
            DatabaseActionsMixin._sync_database_generation_controls(self)
            return

        if state_matches:
            replacement_state.outcome = "recovering-original"
            if not current_matches and not pending_unrelated_load:
                DatabaseActionsMixin._clear_test_database_generation_transaction(self)
                return
            if pending_unrelated_load:
                DatabaseActionsMixin._sync_database_generation_controls(self)
                return
        # A rejected/cancelled publication left the accepted instance in
        # place. Force bypasses the ordinary same-file shortcut while the
        # released controller session is rebuilt from its read-only view.
        if state_matches:
            self.load_file(
                database_path,
                force=True,
                generation_recovery=True,
            )
        else:
            self.load_file(database_path, force=True)


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


    def _clear_snapshot_setpoint_summary_cache(self):
        """Discard summaries captured from the previously accepted source."""

        cache = getattr(self, "_snapshot_setpoint_summary_cache", None)
        if isinstance(cache, dict):
            cache.clear()


    def close_database(self, status=True):
        """
        Clears the current database from the main window state.

        """
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.clear_database()
        cancel_detail_load = getattr(self, "_cancel_database_detail_load", None)
        if callable(cancel_detail_load):
            cancel_detail_load()
        DatabaseActionsMixin._cancel_database_refresh(self)
        DatabaseActionsMixin._cancel_selected_run_detail(self)
        DatabaseActionsMixin._retire_all_trusted_read_services(self)

        self._database_load_generation = getattr(self, "_database_load_generation", 0) + 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._loaded_database_identity = None
        self._loaded_database_instance = None
        if getattr(self, "_test_database_replacement_state", None) is None:
            self._database_view_released_for_generation = False
        if hasattr(self, "_set_database_load_controls_enabled"):
            self._set_database_load_controls_enabled(True)
        if hasattr(self, "_hide_database_load_panel"):
            self._hide_database_load_panel()

        apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
        if callable(apply_refresh_interval):
            apply_refresh_interval(self._main_refresh_interval())
        else:
            self.monitor.stop()
        self.fileTextbox.setText("")
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self._selected_run_guid = None
        self._selected_run_detail_cache = {}
        self._selected_run_partial_detail_keys = set()
        release_selected = getattr(self, "_release_selected_dataset", None)
        if callable(release_selected):
            release_selected()
        else:
            self.ds = None
            self._selected_dataset_key = None
        self.localLastFile = None

        close_handles = getattr(self, "_close_all_dataset_handles", None)
        if callable(close_handles):
            close_handles()
        else:
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
        clear_summary_cache = getattr(
            self,
            "_clear_snapshot_setpoint_summary_cache",
            None,
        )
        if callable(clear_summary_cache):
            clear_summary_cache()
        clear_database_cache = getattr(self.infoBox, "clear_database_cache", None)
        if callable(clear_database_cache):
            clear_database_cache()
        self.infoBox.preview.set_database_runs("", {})
        self.infoBox.scrollToTop()
        DatabaseActionsMixin._sync_database_generation_controls(self)
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
        if not DatabaseActionsMixin._database_generation_read_allowed(
                self,
                database_path,
                operation="showing database information",
                ):
            return
        if DatabaseActionsMixin._reload_if_database_instance_changed(
                self,
                database_path,
                ):
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

        # A read-only query can legitimately take long enough for an external
        # process to atomically replace the main file.  Do not show metadata
        # from that later instance while the UI still represents the earlier
        # one.
        if DatabaseActionsMixin._reload_if_database_instance_changed(
                self,
                database_path,
                ):
            return

        dialog = DatabaseInfoDialog(rows, parent=self)
        dialog.copyButton.clicked.connect(
            lambda: self.show_status("Copied database information.", 3000)
            )
        dialog.exec()


    @QtCore.pyqtSlot()
    def refreshMain(self, automatic=False):
        """
        On self.monitor timer or force refresh, check for new runs in Database

        """
        if automatic and getattr(self, "_loaded_database_instance", None) is None:
            # The automatic path is silent: its lifecycle guard has already
            # invalidated this timer, so do not turn a stale queued timeout
            # into repeated "Load a database" status messages.
            apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
            if callable(apply_refresh_interval):
                apply_refresh_interval(self._main_refresh_interval())
            return

        if not self.fileTextbox.text():
            self.show_status("Load a database before refreshing.", 5000)
            return

        database_path = self.fileTextbox.text()
        if not DatabaseActionsMixin._database_generation_read_allowed(
                self,
                database_path,
                operation="refreshing it",
                ):
            return

        if (
                getattr(self, "_database_load_active", False)
                or getattr(self, "_database_load_publication_active", False)
                ):
            self.show_status("Database reload already in progress.", 3000)
            return

        if DatabaseActionsMixin._reload_if_database_instance_changed(
                self,
                database_path,
                ):
            return

        current_instance = database_instance(database_path)
        current_identity = current_instance.identity

        if getattr(self, "_database_refresh_active", False):
            self._database_refresh_pending = True
            self.show_status("Database refresh queued.", 3000)
            return

        self.show_status("Checking for new runs...", 0)
        self._database_refresh_generation = (
            getattr(self, "_database_refresh_generation", 0) + 1
            )
        generation = self._database_refresh_generation
        self._database_refresh_active = True
        self._database_refresh_pending = False
        self._database_refresh_identity = current_identity
        self._database_refresh_instance = current_instance
        self._database_refresh_staged_new_runs = {}
        watched_pairs = []
        watched_guids = []
        item_run_id = getattr(self.RunList, "_item_run_id", None)
        for run in list(self.RunList.watching):
            guid = getattr(run, "guid", None)
            if not guid:
                continue
            run_id = item_run_id(run) if callable(item_run_id) else None
            if run_id is None:
                continue
            try:
                run_id = int(run_id)
            except (TypeError, ValueError):
                continue
            watched_pairs.append((run_id, str(guid)))
            watched_guids.append(str(guid))

        watched_by_run_id = dict(watched_pairs)
        watched_runs = []
        scheduled_run_ids = set()

        def schedule_watched(run_id, guid, category):
            try:
                run_id = int(run_id)
            except (TypeError, ValueError):
                return
            if run_id in scheduled_run_ids or not guid:
                return
            scheduled_run_ids.add(run_id)
            watched_runs.append((run_id, str(guid), category))

        selected_guid = getattr(self, "_selected_run_guid", None)
        if selected_guid:
            run_id_for_guid = getattr(self.RunList, "run_id_for_guid", None)
            selected_run_id = (
                run_id_for_guid(selected_guid)
                if callable(run_id_for_guid)
                else None
            )
            if selected_run_id is not None:
                schedule_watched(selected_run_id, selected_guid, "selected")

        visible_run_ids = getattr(self.RunList, "visible_run_ids", None)
        if callable(visible_run_ids):
            for run_id in visible_run_ids():
                try:
                    numeric_run_id = int(run_id)
                except (TypeError, ValueError):
                    continue
                guid = watched_by_run_id.get(numeric_run_id)
                if guid is not None:
                    schedule_watched(numeric_run_id, guid, "visible")

        for run_id, guid in watched_pairs:
            schedule_watched(run_id, guid, "remaining")

        refresh_kwargs = {
            "expected_database_instance": current_instance,
        }
        trusted_service = DatabaseActionsMixin._active_trusted_service_for_instance(
            self,
            current_instance,
        )
        if (
                getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE
                and trusted_service is None
                ):
            self._database_refresh_active = False
            self._database_refresh_worker = None
            self._database_refresh_identity = None
            self._database_refresh_instance = None
            self.show_error(
                "Trusted Refresh Unavailable",
                "The accepted trusted live-reader session is no longer usable.",
                "Reload the database to start a new trusted session. qPlot did "
                "not fall back to a snapshot after the accepted-session failure.",
            )
            return
        if trusted_service is not None:
            refresh_kwargs["trusted_service"] = trusted_service
            refresh_kwargs["derived_coordinator_owned"] = bool(
                getattr(self, "_trusted_derived_bridge", None)
            )
        worker = DatabaseRefreshWorker(
            generation,
            database_path,
            self.RunList.maxRunId,
            watched_runs if trusted_service is not None else watched_guids,
            require_publication_ack=trusted_service is not None,
            **refresh_kwargs,
        )
        self._database_refresh_worker = worker
        new_runs_ready = getattr(worker.signals, "new_runs_ready", None)
        if new_runs_ready is not None:
            new_runs_ready.connect(
                lambda *args, refresh_worker=worker:
                DatabaseActionsMixin.database_refresh_new_runs_ready(
                    self,
                    *args,
                    refresh_worker,
                )
            )
        worker.signals.finished.connect(
            lambda *args: self.database_refresh_finished(*args)
            )
        self.databaseRefreshThreadPool.start(worker)


    @QtCore.pyqtSlot(int, str, object)
    def database_refresh_new_runs_ready(
            self,
            generation,
            database_path,
            new_runs,
        source_worker=None,
            ):
        """Publish trusted basic rows before any follow-up status/detail work."""
        if getattr(self, "_database_refresh_publication_active", False):
            error = _DatabaseRefreshPublicationAborted(
                "A second trusted refresh page arrived during UI publication."
            )
            reject = getattr(source_worker, "reject_new_runs_publication", None)
            if callable(reject):
                reject(error)
            return

        publication_error = None
        publication_source_changed = False
        staged = None
        staged_before_publication = None
        refresh_instance = getattr(self, "_database_refresh_instance", None)
        loaded_identity = getattr(self, "_loaded_database_identity", None)
        publication_baseline_instance = getattr(
            self,
            "_loaded_database_instance",
            None,
        )
        accepted_selected_guid = str(
            getattr(self, "_selected_run_guid", "") or ""
        )
        self._database_refresh_publication_active = True
        DatabaseActionsMixin._sync_database_generation_controls(self)

        def publication_is_current():
            return bool(
                getattr(self, "_database_refresh_publication_active", False)
                and generation == getattr(self, "_database_refresh_generation", 0)
                and getattr(self, "_database_refresh_active", False)
                and _database_paths_equal(database_path, self.fileTextbox.text())
                and not getattr(self, "_shutdown_started", False)
                and not getattr(self, "_shutdown_ready", False)
            )

        def publication_source_is_current():
            """Perform the final source check inside RunList's rollback scope."""

            nonlocal publication_source_changed
            if not publication_is_current():
                return False
            current_instance = database_instance(database_path)
            if (
                    _database_instances_or_sidecars_differ(
                        refresh_instance,
                        current_instance,
                    )
                    or _database_instances_or_sidecars_differ(
                        publication_baseline_instance or loaded_identity,
                        current_instance,
                    )
                    ):
                publication_source_changed = True
                return False
            if not DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
                    self,
                    current_instance,
                    ):
                publication_source_changed = True
                return False
            return True

        try:
            if generation != getattr(self, "_database_refresh_generation", 0):
                raise _DatabaseRefreshPublicationAborted(
                    "The trusted refresh generation changed before publication."
                )
            if not getattr(self, "_database_refresh_active", False):
                raise _DatabaseRefreshPublicationAborted(
                    "The trusted refresh ended before publication."
                )
            if not _database_paths_equal(database_path, self.fileTextbox.text()):
                raise _DatabaseRefreshPublicationAborted(
                    "The selected database changed before refresh publication."
                )
            if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                    self,
                    database_path,
                    refresh_instance,
                    ):
                raise _DatabaseRefreshPublicationAborted(
                    "The database instance changed before refresh publication."
                )
            # The preflight may legitimately bind a newly appeared WAL/SHM.
            # Freeze that promoted observation for the nested-event commit
            # guard so removal/replacement cannot look like another addition.
            publication_baseline_instance = getattr(
                self,
                "_loaded_database_instance",
                publication_baseline_instance,
            )
            staged = getattr(self, "_database_refresh_staged_new_runs", None)
            if staged is None:
                staged = {}
                self._database_refresh_staged_new_runs = staged
            staged_before_publication = dict(staged)
            staged.update(new_runs or {})
            published = DatabaseActionsMixin._apply_basic_new_runs(
                self,
                new_runs or {},
                continue_loading=publication_is_current,
                commit_check=publication_source_is_current,
            )
            if published is False:
                if publication_source_changed:
                    raise DatabaseInstanceChangedError(
                        "The database changed during trusted refresh publication."
                    )
                raise _DatabaseRefreshPublicationAborted(
                    "Trusted refresh basic-row publication was invalidated."
                )
            sync_empty_state = getattr(self, "_sync_empty_state", None)
            if callable(sync_empty_state):
                sync_empty_state()
        except Exception as error:
            publication_error = error
            if (
                    staged is not None
                    and staged_before_publication is not None
                    and getattr(self, "_database_refresh_staged_new_runs", None)
                    is staged
                    ):
                staged.clear()
                staged.update(staged_before_publication)
            log_exception(
                "Trusted refresh basic-row publication failed",
                error,
                __name__,
            )
        finally:
            self._database_refresh_publication_active = False
            DatabaseActionsMixin._sync_database_generation_controls(self)
            try:
                selected_items = getattr(self.RunList, "selectedItems", None)
                if callable(selected_items):
                    items = selected_items()
                    selected_guid = ""
                    if len(items) == 1:
                        selected_guid = str(
                            getattr(items[0], "guid", "") or ""
                        )
                    if selected_guid != accepted_selected_guid:
                        if selected_guid:
                            update_selected = getattr(self, "updateSelected", None)
                            if callable(update_selected):
                                update_selected(selected_guid)
                        else:
                            clear_selection = getattr(
                                self,
                                "clear_non_single_run_selection",
                                None,
                            )
                            if callable(clear_selection):
                                clear_selection()
            except Exception as selection_error:
                # Selection replay is controller repair after the basic-row
                # transaction.  It must never suppress the worker's page
                # acknowledgement or rejection.
                log_exception(
                    "Refresh selection reconciliation failed",
                    selection_error,
                    __name__,
                )
            if publication_error is None:
                acknowledge = getattr(
                    source_worker,
                    "acknowledge_new_runs_published",
                    None,
                )
                if callable(acknowledge):
                    acknowledge()
            else:
                reject = getattr(
                    source_worker,
                    "reject_new_runs_publication",
                    None,
                )
                if callable(reject):
                    reject(publication_error)
            if publication_source_changed:
                self._reload_replaced_database(database_path)


    @QtCore.pyqtSlot(int, str, object, object, object)
    def database_refresh_finished(
            self,
            generation,
            database_path,
            new_runs,
            statuses,
            error,
            ):
        if generation != getattr(self, "_database_refresh_generation", 0):
            return
        if not getattr(self, "_database_refresh_active", False):
            return
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                database_path,
                ):
            DatabaseActionsMixin._cancel_database_refresh(self)
            return

        try:
            if not _database_paths_equal(database_path, self.fileTextbox.text()):
                return
            if isinstance(error, DatabaseInstanceChangedError):
                DatabaseActionsMixin._cancel_database_refresh(self)
                self._reload_replaced_database(database_path)
                return
            refresh_identity = getattr(self, "_database_refresh_identity", None)
            refresh_instance = getattr(self, "_database_refresh_instance", None)
            current_instance = database_instance(database_path)
            loaded_identity = getattr(self, "_loaded_database_identity", None)
            loaded_instance = getattr(self, "_loaded_database_instance", None)
            if (
                    _database_instances_or_sidecars_differ(
                        refresh_instance or refresh_identity,
                        current_instance,
                    )
                    or _database_instances_or_sidecars_differ(
                        loaded_instance or loaded_identity,
                        current_instance,
                    )
                    ):
                self._reload_replaced_database(database_path)
                return
            DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
                self,
                current_instance,
            )
            if error is not None:
                logged_error = (
                    error
                    if isinstance(error, BaseException)
                    else RuntimeError(str(error))
                )
                log_exception("Main-window refresh failed", logged_error, __name__)
                if isinstance(error, UnverifiableDatabaseWalError):
                    self.show_error(
                        "Unverifiable Database WAL",
                        "qPlot cannot verify this database's WAL. Close every "
                        "owning QCoDeS/SQLite connection cleanly, or checkpoint "
                        "with the owning writer, then refresh.",
                        str(error),
                        )
                    return
                self.show_error(
                    "Refresh Failed",
                    "Could not refresh the run list.",
                    str(error),
                    )
                return

            effective_new_runs = dict(
                getattr(self, "_database_refresh_staged_new_runs", {})
            )
            effective_new_runs.update(new_runs or {})
            self._apply_database_refresh_result(effective_new_runs, statuses or {})
            if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                    self,
                    database_path,
                    refresh_instance,
                    discard_stale_publication=True,
                    ):
                return
        finally:
            self._database_refresh_active = False
            self._database_refresh_worker = None
            self._database_refresh_identity = None
            self._database_refresh_instance = None
            self._database_refresh_staged_new_runs = {}
            pending = bool(getattr(self, "_database_refresh_pending", False))
            self._database_refresh_pending = False
            if pending and _database_paths_equal(
                    database_path,
                    self.fileTextbox.text(),
                    ):
                QtCore.QTimer.singleShot(0, self.refreshMain)


    def _apply_database_refresh_result(self, new_runs, statuses):
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(self):
            return
        updated_runs = self.RunList.checkWatching(statuses) or {}
        update_runs = getattr(self.RunList, "updateRuns", None)
        run_id_for_guid = getattr(self.RunList, "run_id_for_guid", None)
        if callable(update_runs) and callable(run_id_for_guid):
            updated_guids = {
                str(metadata.get("guid"))
                for metadata in updated_runs.values()
                if metadata.get("guid")
            }
            remaining_statuses = {}
            for guid, status in (statuses or {}).items():
                if not status or str(guid) in updated_guids:
                    continue
                run_id = run_id_for_guid(guid)
                if run_id is None:
                    continue
                metadata = dict(status)
                metadata.setdefault("guid", str(guid))
                remaining_statuses[run_id] = metadata
            updated_runs.update(update_runs(remaining_statuses))
        if updated_runs:
            if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
                bridge = getattr(self, "_trusted_derived_bridge", None)
                if bridge is not None:
                    bridge.source_changed(tuple(updated_runs))
            else:
                self.infoBox.preview.add_runs(updated_runs)
                prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
                if callable(prioritize_previews):
                    prioritize_previews()
            self._refresh_selected_run_details(updated_runs, live_only=True)

        if new_runs:
            DatabaseActionsMixin._apply_basic_new_runs(self, new_runs)

        self._sync_empty_state()
        if not new_runs:
            if self.RunList.topLevelItemCount() == 0:
                self.show_status(self._empty_database_refresh_status(), 3000)
            else:
                self.show_status("No new runs found.", 3000)
            return

        count = len(new_runs)
        noun = "run" if count == 1 else "runs"
        self.show_status(f"Found {count} new {noun}.", 5000)

        if (
                getattr(self, "_database_access_mode", None) != TRUSTED_LIVE_MODE
                and self.autoPlotBox.isChecked()
                ):
            for run in new_runs.values():
                self.openPlot(run["guid"])


    def _apply_basic_new_runs(
            self,
            new_runs,
            *,
            continue_loading=None,
            commit_check=None,
            ):
        """Add bounded basic rows immediately, then queue their metadata work."""
        if not new_runs:
            return True
        all_run_metadata = getattr(self.RunList, "all_run_metadata", None)
        existing = all_run_metadata() if callable(all_run_metadata) else {}

        def run_key(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return str(value)

        existing_ids = {run_key(run_id) for run_id in existing}
        existing_guids = {
            str(metadata.get("guid"))
            for metadata in existing.values()
            if metadata.get("guid")
        }
        additions = {
            run_id: metadata
            for run_id, metadata in new_runs.items()
            if (
                run_key(run_id) not in existing_ids
                and str(metadata.get("guid") or "") not in existing_guids
            )
        }
        if not additions:
            return True
        numeric_run_ids = []
        for run_id in additions:
            try:
                numeric_run_ids.append(int(run_id))
            except (TypeError, ValueError):
                continue
        previous_max_run_id = self.RunList.maxRunId
        publication_complete = False
        try:
            if continue_loading is None and commit_check is None:
                published = self.RunList.addRuns(additions)
            else:
                published = self.RunList.addRuns(
                    additions,
                    continue_loading=continue_loading,
                    commit_check=commit_check,
                )
            if published is False:
                return False
            if numeric_run_ids:
                self.RunList.maxRunId = max(
                    previous_max_run_id,
                    max(numeric_run_ids),
                )
            publication_complete = True
        finally:
            if not publication_complete:
                self.RunList.maxRunId = previous_max_run_id
        if getattr(self, "_database_access_mode", None) != TRUSTED_LIVE_MODE:
            self.infoBox.preview.add_runs(additions)
            prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
            if callable(prioritize_previews):
                prioritize_previews()
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            bridge = getattr(self, "_trusted_derived_bridge", None)
            if bridge is not None:
                bridge.reconcile_runs(self.RunList.all_run_metadata())
            else:
                DatabaseActionsMixin._queue_incremental_trusted_detail_runs(
                    self,
                    tuple(additions),
                )
        else:
            restart_details = getattr(
                self,
                "_restart_database_detail_load_for_current_runs",
                None,
            )
            if callable(restart_details):
                restart_details()
        return True


    def _restart_database_detail_load_for_current_runs(self):
        database_path = self.fileTextbox.text()
        if not database_path:
            return
        runs = self.RunList.all_run_metadata()
        self._cancel_database_detail_load()
        self._start_database_detail_load(database_path, runs)


    def _queue_incremental_trusted_detail_runs(self, run_ids):
        """Compatibility hook routing trusted additions only to Stage 5C."""

        bridge = getattr(self, "_trusted_derived_bridge", None)
        if bridge is not None:
            bridge.reconcile_runs(self.RunList.all_run_metadata())
            return
        normalised = []
        for run_id in run_ids or ():
            try:
                run_id = int(run_id)
            except (TypeError, ValueError):
                continue
            if run_id > 0:
                normalised.append(run_id)
        if not normalised:
            return

        database_path = self.fileTextbox.text()
        detail_instance = getattr(self, "_loaded_database_instance", None)
        if not database_path or not isinstance(detail_instance, DatabaseInstance):
            return
        trusted_service = DatabaseActionsMixin._active_trusted_service_for_instance(
            self,
            detail_instance,
        )
        if trusted_service is None:
            return
        worker_kwargs = {
            "expected_database_instance": detail_instance,
            "trusted_service": trusted_service,
        }
        cheap_worker = getattr(self, "_database_detail_worker", None)
        add_cheap = getattr(cheap_worker, "add_run_ids", None)
        if not (
                getattr(self, "_database_detail_active", False)
                and callable(add_cheap)
                and add_cheap(normalised)
                ):
            DatabaseActionsMixin._start_database_cheap_detail_worker(
                self,
                database_path,
                normalised,
                detail_instance,
                worker_kwargs,
            )
        expensive_worker = getattr(self, "_database_expensive_detail_worker", None)
        add_expensive = getattr(expensive_worker, "add_run_ids", None)
        if not (
                getattr(self, "_database_expensive_detail_active", False)
                and callable(add_expensive)
                and add_expensive(normalised)
                ):
            DatabaseActionsMixin._start_database_expensive_detail_worker(
                self,
                database_path,
                normalised,
                detail_instance,
                worker_kwargs,
            )
        self._prioritize_database_detail_runs()


    def _cancel_database_refresh(self):
        worker = getattr(self, "_database_refresh_worker", None)
        if worker is not None:
            worker.cancel()
        self._database_refresh_generation = (
            getattr(self, "_database_refresh_generation", 0) + 1
            )
        self._database_refresh_active = False
        self._database_refresh_pending = False
        self._database_refresh_worker = None
        self._database_refresh_identity = None
        self._database_refresh_instance = None
        self._database_refresh_staged_new_runs = {}


    def _reload_replaced_database(
            self,
            database_path,
            *,
            generation_recovery=False,
            load_started_at=None,
            ):
        """Invalidate one replaced database instance and force a safe reload."""
        if not generation_recovery:
            return self.load_file(
                database_path,
                load_started_at,
                force=True,
                replacement=True,
            )
        return self.load_file(
            database_path,
            load_started_at,
            force=True,
            replacement=True,
            generation_recovery=True,
        )


    def _retry_replaced_database_if_current(
            self,
            retry_generation,
            database_path,
            *,
            generation_recovery=False,
            load_started_at=None,
            ):
        """Run a queued replacement retry only for its still-current generation."""
        if (
                retry_generation != getattr(self, "_database_load_generation", 0)
                or getattr(self, "_shutdown_started", False)
                or getattr(self, "_shutdown_ready", False)
                ):
            return False
        reload_replaced_database = getattr(
            self,
            "_reload_replaced_database",
            None,
        )
        if not callable(reload_replaced_database):
            reload_replaced_database = (
                lambda path, **kwargs:
                DatabaseActionsMixin._reload_replaced_database(
                    self,
                    path,
                    **kwargs,
                )
            )
        return reload_replaced_database(
            database_path,
            generation_recovery=generation_recovery,
            load_started_at=load_started_at,
        )


    def _reload_if_database_instance_changed(
            self,
            database_path,
            *,
            discard_stale_publication=False,
            ):
        """Start replacement reload before any action reads a changed source."""

        if DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                database_path,
                ):
            if discard_stale_publication:
                DatabaseActionsMixin._discard_stale_worker_publication(self)
            return True

        loaded_identity = getattr(self, "_loaded_database_identity", None)
        loaded_instance = getattr(self, "_loaded_database_instance", None)
        if loaded_instance is None:
            changed = _database_instances_differ(
                loaded_identity,
                _database_file_identity(database_path),
            )
        else:
            current_instance = database_instance(database_path)
            changed = _database_instances_or_sidecars_differ(
                loaded_instance,
                current_instance,
            )
        if not changed:
            if loaded_instance is not None:
                DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
                    self,
                    current_instance,
                )
            return False
        if discard_stale_publication:
            DatabaseActionsMixin._discard_stale_worker_publication(self)
        self._reload_replaced_database(database_path)
        return True


    def _reload_if_worker_database_instance_changed(
            self,
            database_path,
            expected_instance,
            *,
            discard_stale_publication=False,
            ):
        """Reject a metadata callback not bound to the accepted DB instance."""
        if not isinstance(expected_instance, DatabaseInstance):
            return DatabaseActionsMixin._reload_if_database_instance_changed(
                self,
                database_path,
                discard_stale_publication=discard_stale_publication,
            )

        current_instance = database_instance(expected_instance.logical_path)
        loaded_instance = getattr(self, "_loaded_database_instance", None)
        if (
                _database_instances_or_sidecars_differ(
                    expected_instance,
                    current_instance,
                )
                or (
                    isinstance(loaded_instance, DatabaseInstance)
                    and _database_instances_or_sidecars_differ(
                        loaded_instance,
                        current_instance,
                    )
                )
                ):
            if discard_stale_publication:
                DatabaseActionsMixin._discard_stale_worker_publication(self)
            self._reload_replaced_database(database_path)
            return True
        DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
            self,
            current_instance,
        )
        return False


    def _discard_stale_worker_publication(self):
        """Fail closed after a widget mutation races source replacement.

        Replacement reload owns worker cancellation, session retirement, and
        generation changes.  This local step only removes the callback's
        visible model before that reload is requested, so it cannot leave a
        mixed-source view when reload startup is asynchronous or overridden.
        """
        run_id_box = getattr(self, "run_idBox", None)
        run_list = getattr(self, "RunList", None)
        run_id_signals_blocked = None
        run_list_signals_blocked = None
        if run_id_box is not None:
            block_signals = getattr(run_id_box, "blockSignals", None)
            if callable(block_signals):
                run_id_signals_blocked = block_signals(True)
        if run_list is not None:
            block_signals = getattr(run_list, "blockSignals", None)
            if callable(block_signals):
                run_list_signals_blocked = block_signals(True)

        try:
            if run_id_box is not None:
                set_text = getattr(run_id_box, "setText", None)
                if callable(set_text):
                    set_text("")
            measurement_box = getattr(self, "measurementBox", None)
            set_measurement = getattr(measurement_box, "setText", None)
            if callable(set_measurement):
                set_measurement("*")

            self.selected_run_id = None
            self._selected_run_guid = None
            self._selected_run_detail_cache = {}
            self._selected_run_partial_detail_keys = set()
            release_selected = getattr(self, "_release_selected_dataset", None)
            if callable(release_selected):
                release_selected()
            else:
                self.ds = None
                self._selected_dataset_key = None

            if run_list is not None:
                clear_selection = getattr(run_list, "clearSelection", None)
                if callable(clear_selection):
                    clear_selection()
                clear = getattr(run_list, "clear", None)
                if callable(clear):
                    clear()
                if hasattr(run_list, "watching"):
                    run_list.watching = []
                if hasattr(run_list, "maxRunId"):
                    run_list.maxRunId = 0
                scroll_to_top = getattr(run_list, "scrollToTop", None)
                if callable(scroll_to_top):
                    scroll_to_top()

            info_box = getattr(self, "infoBox", None)
            clear_info = getattr(info_box, "clear", None)
            if callable(clear_info):
                clear_info()
            clear_summary_cache = getattr(
                self,
                "_clear_snapshot_setpoint_summary_cache",
                None,
            )
            if callable(clear_summary_cache):
                clear_summary_cache()
            else:
                summary_cache = getattr(
                    self,
                    "_snapshot_setpoint_summary_cache",
                    None,
                )
                if isinstance(summary_cache, dict):
                    summary_cache.clear()
            clear_database_cache = getattr(info_box, "clear_database_cache", None)
            if callable(clear_database_cache):
                clear_database_cache()
            preview = getattr(info_box, "preview", None)
            clear_preview_source = getattr(preview, "set_database_runs", None)
            if callable(clear_preview_source):
                clear_preview_source("", {})
            scroll_info_to_top = getattr(info_box, "scrollToTop", None)
            if callable(scroll_info_to_top):
                scroll_info_to_top()
        finally:
            if run_list is not None and run_list_signals_blocked is not None:
                run_list.blockSignals(run_list_signals_blocked)
            if run_id_box is not None and run_id_signals_blocked is not None:
                run_id_box.blockSignals(run_id_signals_blocked)

        sync_empty_state = getattr(self, "_sync_empty_state", None)
        if callable(sync_empty_state):
            sync_empty_state()


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

        default_load_path = self.config.get("file.default_load_path")

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
            if persist_config_value(
                    self,
                    self.config,
                    "file.default_load_path",
                    foldername,
                    "the default database folder",
                    ):
                self.show_status(f"Default load folder set to {foldername}", 5000)
                return True
            return False
        else:
            self.show_status("Default load folder unchanged.", 3000)
            return False


    @QtCore.pyqtSlot(str)
    def load_database_path(self, filename):
        """
        Load a database path chosen from the file dialog or dropped by the user.

        """
        load_started_at = perf_counter()
        log_event("Database load requested: %s", filename, logger_name=__name__)

        if not DatabaseActionsMixin._database_generation_read_allowed(
                self,
                filename,
                operation="reloading that path",
                ):
            return False

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
        paths = list(self.config.get("file.recent_file_paths"))

        deduped = []
        seen = set()
        for path in paths:
            abspath = os.path.abspath(path)
            if abspath in seen:
                continue
            seen.add(abspath)
            deduped.append(abspath)

        return deduped[:10]


    def remember_loaded_database(self, filename):
        """
        Persists the successfully loaded database path.

        """
        abspath = os.path.abspath(filename)
        current_paths = list(self.config.get("file.recent_file_paths"))

        paths = [path for path in self.recent_database_paths() if path != abspath]
        paths.insert(0, abspath)
        paths = paths[:10]

        if current_paths == paths:
            return True

        if not persist_config_value(
                self,
                self.config,
                "file.recent_file_paths",
                paths,
                "the recent database list",
                ):
            return False

        self.refresh_recent_database_menu()
        return True


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


    def load_file(
            self,
            abspath,
            load_started_at=None,
            *,
            force=False,
            replacement=False,
            generation_recovery=False,
            ):
        """
        Updates the database for RunList display and loading datasets.

        """
        if load_started_at is None:
            load_started_at = perf_counter()
        log_event("Loading database file: %s", abspath, logger_name=__name__)

        transaction_blocks = (
            DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                abspath,
            )
        )
        if transaction_blocks and not generation_recovery:
            DatabaseActionsMixin._database_generation_read_allowed(
                self,
                abspath,
                operation="reloading that path",
            )
            return False
        if generation_recovery and not transaction_blocks:
            return False

        if (
                self._database_load_active
                or getattr(self, "_database_load_publication_active", False)
                ):
            self.show_status("Wait for the current database load to finish.", 5000)
            return False

        qcodes_database = get_DB_location()
        displayed_database = self.fileTextbox.text()
        same_loaded_database = bool(
            qcodes_database
            and displayed_database
            and logical_database_path(abspath)
            == logical_database_path(qcodes_database)
            == logical_database_path(displayed_database)
        )
        current_instance = database_instance(abspath)
        current_identity = current_instance.identity
        loaded_identity = getattr(self, "_loaded_database_identity", None)
        loaded_instance = getattr(self, "_loaded_database_instance", None)
        if current_identity is None and os.path.isfile(abspath):
            if same_loaded_database and loaded_instance is not None:
                self._prepare_replaced_database_reload(abspath)
                self._loaded_database_identity = None
                self._loaded_database_instance = None
            self.show_error(
                "Database Identity Unavailable",
                "qPlot cannot safely monitor this database for replacement.",
                (
                    "The filesystem did not provide a stable file identity. "
                    "The database was not loaded because qPlot cannot safely "
                    "distinguish normal live writes from a replaced file."
                ),
            )
            return False
        replacement = bool(
            replacement
            or (
                same_loaded_database
                and _database_instances_or_sidecars_differ(
                    loaded_instance or loaded_identity,
                    current_instance,
                )
                )
            )
        if same_loaded_database and not replacement:
            DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
                self,
                current_instance,
            )

        if (
                same_loaded_database
                and not replacement
                and not force
                and not getattr(
                    self,
                    "_database_view_released_for_generation",
                    False,
                )
                and loaded_identity is not None
                and current_identity == loaded_identity
                and (
                    getattr(self, "_database_access_mode", None)
                    != TRUSTED_LIVE_MODE
                    or DatabaseActionsMixin._active_trusted_service_for_instance(
                        self,
                        current_instance,
                    )
                    is not None
                )
                ):
            if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
                service = DatabaseActionsMixin._active_trusted_service_for_instance(
                    self,
                    current_instance,
                )
                bridge = getattr(self, "_trusted_derived_bridge", None)
                refresh = getattr(bridge, "refresh_active_database", None)
                refreshed = bool(
                    service is not None
                    and callable(refresh)
                    and refresh(
                        getattr(self, "_loaded_database_instance", current_instance),
                        self.RunList.all_run_metadata(),
                        service,
                    )
                )
                if not refreshed and bridge is not None:
                    generation = getattr(self, "_database_load_generation", 0)
                    QtCore.QTimer.singleShot(
                        0,
                        lambda: self._start_trusted_derived_bridge(
                            generation,
                            abspath,
                        ),
                    )
            elif not self.infoBox.preview.has_database(abspath):
                self.infoBox.preview.set_database_runs(
                    abspath,
                    self.RunList.all_run_metadata(),
                )
            elapsed = perf_counter() - load_started_at
            self.show_status(f"Database is already loaded ({elapsed:.2f} s).", 3000)
            self.remember_loaded_database(abspath)
            return True

        DatabaseActionsMixin._cancel_database_refresh(self)
        if replacement:
            self._prepare_replaced_database_reload(abspath)

        if replacement:
            load_message = (
                f"Database was replaced; reloading {os.path.basename(abspath)}..."
            )
        else:
            load_message = f"Loading database {os.path.basename(abspath)}..."

        self._database_load_generation += 1
        generation = self._database_load_generation
        self._database_load_active = True
        self._database_load_state = {
            "abspath": abspath,
            "load_started_at": load_started_at,
            "reload_same_path": force or same_loaded_database,
            "load_identity": current_identity,
            "load_instance": current_instance,
            "replacement_reload": replacement,
            "generation_recovery": generation_recovery,
        }
        apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
        if callable(apply_refresh_interval):
            apply_refresh_interval(self._main_refresh_interval())

        self._set_database_load_controls_enabled(False)
        self._show_database_load_panel(load_message)

        cloud_sync_timeout = self.config.get(
            "runtime_settings.cloud_sync_timeout"
            )

        # Bootstrap resets the adapter's schema cache and pagination cursors.
        # A reload therefore needs an isolated pending service: borrowing the
        # committed service would mutate A before the replacement view has
        # successfully published.  The unchanged-instance fast path above is
        # the only load path that may continue using the active service.
        worker_kwargs = {
            "expected_database_instance": current_instance,
        }
        worker = DatabaseLoadWorker(
            generation,
            abspath,
            cloud_sync_timeout,
            **worker_kwargs,
        )
        self._database_load_worker = worker
        pending_service = getattr(worker, "trusted_service", None)
        owns_pending_service = bool(
            pending_service is not None
            and pending_service is not getattr(self, "_trusted_read_service", None)
        )
        if owns_pending_service:
            pending_services = getattr(
                self,
                "_pending_trusted_read_services",
                None,
            )
            if pending_services is None:
                pending_services = {}
                self._pending_trusted_read_services = pending_services
            pending_services[generation] = pending_service
        self._database_load_state["worker"] = worker
        self._database_load_state["trusted_service"] = pending_service
        self._database_load_state["owns_trusted_service"] = owns_pending_service
        worker.signals.status.connect(self.database_load_status)
        worker.signals.finished.connect(
            lambda callback_generation, callback_path, runs, error,
            load_worker=worker: self.database_load_finished(
                callback_generation,
                callback_path,
                runs,
                error,
                load_worker,
            )
        )
        self.databaseLoadThreadPool.start(worker)
        return True


    def _prepare_test_database_replacement(self, abspath):
        """Capture and release a loaded source before tentative publication."""
        monitor = getattr(self, "monitor", None)
        monitor_is_active = getattr(monitor, "isActive", None)
        if callable(monitor_is_active):
            monitor_was_active = bool(monitor_is_active())
        else:
            monitor_was_active = self._main_refresh_interval() > 0
        monitor_interval = getattr(monitor, "interval", None)
        if callable(monitor_interval):
            monitor_interval_ms = int(monitor_interval())
        else:
            monitor_interval_ms = max(
                1,
                round(self._main_refresh_interval() * 1000),
            )

        selected_dataset = getattr(self, "ds", None)
        selected_guid = getattr(self, "_selected_run_guid", None)
        if selected_guid is None:
            selected_guid = getattr(selected_dataset, "guid", None)
        if selected_guid is None:
            selected_items = getattr(self.RunList, "selectedItems", None)
            if callable(selected_items):
                items = selected_items()
                if len(items) == 1:
                    selected_guid = getattr(items[0], "guid", None)

        run_id_box = getattr(self, "run_idBox", None)
        measurement_box = getattr(self, "measurementBox", None)
        details_tab = getattr(self, "infoBox", None)
        current_tab_index = getattr(details_tab, "currentIndex", None)
        loaded_instance = getattr(self, "_loaded_database_instance", None)
        if (
                not isinstance(loaded_instance, DatabaseInstance)
                or loaded_instance.logical_path != logical_database_path(abspath)
                ):
            loaded_instance = database_instance(abspath)

        state = _TestDatabaseReplacementState(
            database_path=str(abspath),
            original_instance=loaded_instance,
            selected_guid=selected_guid,
            selected_run_id=getattr(self, "selected_run_id", None),
            run_id_text=run_id_box.text() if run_id_box is not None else "",
            measurement_text=(
                measurement_box.text() if measurement_box is not None else "*"
            ),
            details_tab_index=(
                int(current_tab_index()) if callable(current_tab_index) else None
            ),
            monitor_was_active=monitor_was_active,
            monitor_interval_ms=monitor_interval_ms,
        )
        self._test_database_replacement_state = state
        self._database_view_released_for_generation = True
        self._release_database_runtime_state(abspath)
        DatabaseActionsMixin._sync_database_generation_controls(self)
        return state


    def _prepare_replaced_database_reload(self, abspath):
        """Quarantine and discard objects after replacement is confirmed."""
        quarantine_wal_for_replaced_database(abspath)
        self._release_database_runtime_state(abspath)


    def _release_database_runtime_state(self, abspath):
        """Release database consumers without asserting that publication won."""
        old_instance = getattr(self, "_loaded_database_instance", None)
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.clear_database()
        stop_refresh_timer = getattr(self, "_stop_automatic_refresh_timer", None)
        if callable(stop_refresh_timer):
            stop_refresh_timer()
        else:
            self.monitor.stop()
        # Once consumers have been released, this is no longer a committed
        # source even though the path remains displayed while replacement
        # loading is in progress.
        self._loaded_database_identity = None
        self._loaded_database_instance = None
        DatabaseActionsMixin._cancel_database_refresh(self)
        self._cancel_database_detail_load()
        DatabaseActionsMixin._cancel_selected_run_detail(self)
        active_service = getattr(self, "_trusted_read_service", None)
        self._trusted_read_service = None
        DatabaseActionsMixin._retire_trusted_read_service(
            self,
            active_service,
            force=True,
        )
        self._database_access_mode = None
        self._database_fallback_reason = None
        self._selected_run_detail_cache = {}
        self._selected_run_partial_detail_keys = set()

        cancel_plot_work = getattr(self, "_cancel_plot_work", None)
        if callable(cancel_plot_work):
            cancel_plot_work()

        invalidate_runtime_state = getattr(
            self,
            "_invalidate_database_runtime_state",
            None,
        )
        if callable(invalidate_runtime_state):
            invalidate_runtime_state(old_instance or abspath)

        run_id_signals_blocked = self.run_idBox.blockSignals(True)
        run_list_signals_blocked = self.RunList.blockSignals(True)
        try:
            self._prepare_database_load_ui(abspath)
            self.infoBox.preview.set_database_runs(abspath, {})
        finally:
            self.RunList.blockSignals(run_list_signals_blocked)
            self.run_idBox.blockSignals(run_id_signals_blocked)
        self._sync_empty_state()


    def _prepare_database_load_ui(
            self,
            abspath,
            *,
            release_selected=True,
            clear_details=True,
            update_path=True,
            ):
        """
        Replaces the main-window state with a successfully loaded database.

        """
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self._selected_run_guid = None
        if release_selected:
            release_selected_dataset = getattr(
                self,
                "_release_selected_dataset",
                None,
            )
            if callable(release_selected_dataset):
                release_selected_dataset()
            else:
                self.ds = None
                self._selected_dataset_key = None

        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxRunId = 0
        self.RunList.scrollToTop()

        if clear_details:
            self.infoBox.clear()
            clear_summary_cache = getattr(
                self,
                "_clear_snapshot_setpoint_summary_cache",
                None,
            )
            if callable(clear_summary_cache):
                clear_summary_cache()
            clear_database_cache = getattr(self.infoBox, "clear_database_cache", None)
            if callable(clear_database_cache):
                clear_database_cache()
            self.infoBox.scrollToTop()

        if update_path:
            if self.fileTextbox.text() and self.fileTextbox.text() != self.localLastFile:
                self.localLastFile = self.fileTextbox.text()

            self.fileTextbox.setText(abspath)


    def _capture_database_load_publication(self):
        """Capture the committed view while a pending database is staged."""
        preview = self.infoBox.preview
        preview_path = getattr(preview, "database_path", None)
        if preview_path is None:
            database_runs = getattr(preview, "database_runs", ("", {}))
            preview_path = database_runs[0] if database_runs else ""
        return {
            "database_path": self.fileTextbox.text(),
            "runs": dict(self.RunList.all_run_metadata()),
            "run_id_text": self.run_idBox.text(),
            "measurement_text": self.measurementBox.text(),
            "selected_run_id": getattr(self, "selected_run_id", None),
            "selected_run_guid": getattr(self, "_selected_run_guid", None),
            "selected_dataset": getattr(self, "ds", None),
            "selected_dataset_key": getattr(self, "_selected_dataset_key", None),
            "local_last_file": getattr(self, "localLastFile", ""),
            "preview_path": preview_path or "",
            "trusted_service": getattr(self, "_trusted_read_service", None),
            "access_mode": getattr(self, "_database_access_mode", None),
            "fallback_reason": getattr(self, "_database_fallback_reason", None),
            "database_identity": getattr(self, "_loaded_database_identity", None),
            "database_instance": getattr(self, "_loaded_database_instance", None),
            "qcodes_database": get_DB_location(),
        }


    def _restore_database_load_publication(
            self,
            snapshot,
            *,
            continue_loading=None,
            ):
        """Restore A after B fails before its publication transaction commits."""
        self._trusted_read_service = snapshot["trusted_service"]
        self._database_access_mode = snapshot["access_mode"]
        self._database_fallback_reason = snapshot["fallback_reason"]
        self._loaded_database_identity = snapshot["database_identity"]
        self._loaded_database_instance = snapshot["database_instance"]
        self.selected_run_id = snapshot["selected_run_id"]
        self._selected_run_guid = snapshot["selected_run_guid"]
        self.ds = snapshot["selected_dataset"]
        self._selected_dataset_key = snapshot["selected_dataset_key"]
        self.localLastFile = snapshot["local_last_file"]
        self.run_idBox.setText(snapshot["run_id_text"])
        self.measurementBox.setText(snapshot["measurement_text"])
        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        restored = self.RunList.addRuns(
            snapshot["runs"],
            continue_loading=continue_loading,
        )
        if restored is False:
            return False
        self.fileTextbox.setText(snapshot["database_path"])
        self.infoBox.preview.set_database_runs(
            snapshot["preview_path"],
            snapshot["runs"],
        )
        set_qcodes_database_location(snapshot["qcodes_database"])
        return True


    def _set_database_load_controls_enabled(self, enabled):
        """
        Enables or disables controls that start overlapping database actions.

        """
        for attr in (
            "loadDatabaseButton",
            "openDatabaseFolderButton",
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setEnabled(enabled)
        DatabaseActionsMixin._sync_database_generation_controls(self)


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

        cancelled_generation = getattr(self, "_database_load_generation", 0)
        pending_service = getattr(
            self,
            "_pending_trusted_read_services",
            {},
        ).pop(cancelled_generation, None)
        DatabaseActionsMixin._retire_trusted_read_service(
            self,
            pending_service,
        )

        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)

        self._hide_database_load_panel()
        self.show_status("Database load cancelled.", 3000)
        DatabaseActionsMixin._resume_test_database_generation_recovery(self)
        apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
        if callable(apply_refresh_interval):
            apply_refresh_interval(self._main_refresh_interval())


    @QtCore.pyqtSlot(int, str)
    def database_load_status(self, generation, message):
        """
        Shows progress from the active database load.

        """
        if generation != self._database_load_generation or not self._database_load_active:
            return

        self._show_database_load_panel(message)


    @QtCore.pyqtSlot(int, str, object, object)
    def database_load_finished(
            self,
            generation,
            abspath,
            runs,
            error,
            load_worker=None,
            ):
        """
        Applies the background database load result on the GUI thread.

        """
        pending_service = getattr(
            self,
            "_pending_trusted_read_services",
            {},
        ).pop(generation, None)
        if generation != self._database_load_generation:
            DatabaseActionsMixin._retire_trusted_read_service(
                self,
                pending_service,
            )
            return
        if not getattr(self, "_database_load_active", False):
            DatabaseActionsMixin._retire_trusted_read_service(
                self,
                pending_service,
            )
            return

        state = self._database_load_state or {}
        if state.get("abspath") != abspath:
            DatabaseActionsMixin._retire_trusted_read_service(
                self,
                pending_service,
            )
            return
        if load_worker is None:
            load_worker = state.get("worker")
        if pending_service is None:
            pending_service = state.get("trusted_service")
        owns_pending_service = bool(
            state.get(
                "owns_trusted_service",
                pending_service is not None
                and pending_service
                is not getattr(self, "_trusted_read_service", None),
            )
        )
        access_mode = getattr(
            load_worker,
            "access_mode",
            SNAPSHOT_FALLBACK_MODE,
        ) or SNAPSHOT_FALLBACK_MODE
        fallback_reason = getattr(load_worker, "fallback_reason", None)

        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)
        self._hide_database_load_panel()
        load_started_at = state.get("load_started_at") or perf_counter()
        load_identity = state.get("load_identity")
        load_instance = state.get("load_instance")
        current_instance = database_instance(abspath)

        if (
                isinstance(error, DatabaseInstanceChangedError)
                or _database_instances_or_sidecars_differ(
                    load_instance or load_identity,
                    current_instance,
                )
                ):
            if owns_pending_service:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    pending_service,
                )
            self._database_load_generation += 1
            retry_generation = self._database_load_generation
            self.show_status("Database changed while loading; retrying...", 0)
            generation_recovery = bool(state.get("generation_recovery"))
            QtCore.QTimer.singleShot(
                0,
                lambda: DatabaseActionsMixin._retry_replaced_database_if_current(
                    self,
                    retry_generation,
                    abspath,
                    generation_recovery=generation_recovery,
                    load_started_at=load_started_at,
                ),
            )
            return

        if error is not None:
            if owns_pending_service:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    pending_service,
                )
            log_exception("Database load failed", error, __name__)
            if isinstance(error, UnverifiableDatabaseWalError):
                self.show_error(
                    "Unverifiable Database WAL",
                    "qPlot refused to load this database because its WAL "
                    "cannot be verified. Close every owning QCoDeS/SQLite "
                    "connection cleanly, or checkpoint with the owning writer, "
                    "then retry loading the database.",
                    str(error),
                )
                DatabaseActionsMixin._resume_test_database_generation_recovery(self)
                apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
                if callable(apply_refresh_interval):
                    apply_refresh_interval(self._main_refresh_interval())
                return
            self.show_error(
                "Database Load Failed",
                f"Could not load database {abspath}.",
                str(error),
            )
            DatabaseActionsMixin._resume_test_database_generation_recovery(self)
            apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
            if callable(apply_refresh_interval):
                apply_refresh_interval(self._main_refresh_interval())
            return

        # This observation is the transaction's publication point.  A failed
        # or stale B load returns above while A and its service are still
        # committed.  Only a B instance revalidated here may replace A; any
        # later source change is a new replacement event caught by the final
        # post-publication guard below.
        accepted_instance = current_instance
        accepted_identity = accepted_instance.identity
        if access_mode == TRUSTED_LIVE_MODE:
            if (
                    pending_service is None
                    or not getattr(pending_service, "accepted", False)
                    # The helper accepts its source before SQLite may create the
                    # permitted exact SHM.  Directional comparison accepts that
                    # first appearance, but never normalises removal or
                    # replacement of a sidecar the helper already accepted.
                    or _database_instances_or_sidecars_differ(
                        pending_service.database_instance,
                        current_instance,
                    )
                    ):
                if owns_pending_service:
                    DatabaseActionsMixin._retire_trusted_read_service(
                        self,
                        pending_service,
                    )
                self.show_error(
                    "Trusted Database Load Failed",
                    "The trusted reader did not remain bound to the accepted "
                    "database instance.",
                    "The pending session was retired without changing the "
                    "currently displayed database.",
                )
                DatabaseActionsMixin._resume_test_database_generation_recovery(self)
                apply_refresh_interval = getattr(
                    self,
                    "_apply_refresh_interval",
                    None,
                )
                if callable(apply_refresh_interval):
                    apply_refresh_interval(self._main_refresh_interval())
                return
        elif access_mode != SNAPSHOT_FALLBACK_MODE:
            if owns_pending_service:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    pending_service,
                )
            self.show_error(
                "Database Load Failed",
                "The database worker returned an unknown access mode.",
                str(access_mode),
            )
            DatabaseActionsMixin._resume_test_database_generation_recovery(self)
            apply_refresh_interval = getattr(
                self,
                "_apply_refresh_interval",
                None,
            )
            if callable(apply_refresh_interval):
                apply_refresh_interval(self._main_refresh_interval())
            return

        publication_snapshot = (
            DatabaseActionsMixin._capture_database_load_publication(self)
        )
        old_service = publication_snapshot["trusted_service"]
        new_service = (
            pending_service if access_mode == TRUSTED_LIVE_MODE else None
        )
        runs = runs or {}
        publication_error = None
        publication_source_changed = False
        publication_aborted = False
        publication_restored = False
        publication_committed = False
        publication_rollback_error = None
        publication_rollback_source_changed = False

        # A nested event yield must not let A's already-running metadata jobs
        # merge into the staged B model.  Their public cancellation is prompt;
        # rollback below explicitly restarts A's progressive work.
        self._cancel_database_detail_load()
        DatabaseActionsMixin._cancel_selected_run_detail(self)
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.suspend_publications()

        # Keep every owned pending session reachable by close_database()/quit
        # while addRuns deliberately pumps Qt events.  Snapshot fallback has
        # already closed its rejected broker, but qPlot must still retain that
        # object until shutdown completion is observable.
        if pending_service is not None and owns_pending_service:
            self._pending_trusted_read_services[generation] = pending_service

        def publication_is_current():
            return bool(
                getattr(self, "_database_load_publication_active", False)
                and generation == getattr(self, "_database_load_generation", 0)
                and not getattr(self, "_shutdown_started", False)
                and not getattr(self, "_shutdown_ready", False)
            )

        self._database_load_publication_active = True
        DatabaseActionsMixin._sync_database_generation_controls(self)
        run_id_signals_blocked = self.run_idBox.blockSignals(True)
        run_list_signals_blocked = self.RunList.blockSignals(True)
        set_preview_publication_suspended = getattr(
            self.RunList,
            "set_preview_publication_suspended",
            None,
        )
        preview_publication_was_suspended = False
        preview_publication_gate_set = False
        try:
            if callable(set_preview_publication_suspended):
                preview_publication_was_suspended = (
                    set_preview_publication_suspended(True)
                )
                preview_publication_gate_set = True
            try:
                # Stage B's run model while every action remains gated and A is
                # still the committed service/instance.  Only after staging and
                # one final source observation do the plain controller fields
                # advance together to B.
                self._prepare_database_load_ui(
                    abspath,
                    release_selected=False,
                    clear_details=False,
                    update_path=False,
                )
                staged = self.RunList.addRuns(
                    runs,
                    continue_loading=publication_is_current,
                )
                if staged is False or not publication_is_current():
                    raise _DatabaseLoadPublicationAborted(
                        "The database view was closed while B was staged."
                    )
                staged_instance = database_instance(abspath)
                if _database_instances_or_sidecars_differ(
                        accepted_instance,
                        staged_instance,
                        ):
                    raise DatabaseInstanceChangedError(
                        "The database changed while its loaded view was staged."
                    )

                set_qcodes_database_location(abspath)
                self._trusted_read_service = new_service
                self._database_access_mode = access_mode
                self._database_fallback_reason = fallback_reason
                self._loaded_database_identity = accepted_identity
                # A WAL/SHM that appeared while B was staged is compatible,
                # but it is now an accepted identity that later guards must
                # retain in order to detect replacement or ABA.
                self._loaded_database_instance = staged_instance
                prior_path = publication_snapshot["database_path"]
                if prior_path and prior_path != self.localLastFile:
                    self.localLastFile = prior_path
                self.fileTextbox.setText(abspath)
                if access_mode == TRUSTED_LIVE_MODE:
                    self.infoBox.preview.set_database_runs("", {})
                    show_run_preview_placeholders = getattr(
                        self.RunList,
                        "show_run_preview_placeholders",
                        None,
                    )
                    if callable(show_run_preview_placeholders):
                        show_run_preview_placeholders()
                else:
                    self.infoBox.preview.set_database_runs(abspath, runs)
                self.infoBox.clear()
                clear_summary_cache = getattr(
                    self,
                    "_clear_snapshot_setpoint_summary_cache",
                    None,
                )
                if callable(clear_summary_cache):
                    clear_summary_cache()
                clear_database_cache = getattr(
                    self.infoBox,
                    "clear_database_cache",
                    None,
                )
                if callable(clear_database_cache):
                    clear_database_cache()
                self.infoBox.scrollToTop()
                release_selected = getattr(
                    self,
                    "_release_selected_dataset",
                    None,
                )
                if callable(release_selected):
                    release_selected()
                else:
                    self.ds = None
                    self._selected_dataset_key = None
                publication_committed = True
            except _DatabaseLoadPublicationAborted:
                publication_aborted = True
            except DatabaseInstanceChangedError as publication_exception:
                publication_error = publication_exception
                publication_source_changed = True
            except Exception as publication_exception:
                publication_error = publication_exception

            if publication_committed:
                # The controller fields now name B.  Transfer every remaining
                # service immediately, before signal restoration or control
                # synchronisation in the enclosing finally can raise.
                if old_service is not new_service:
                    # The old coordinator may still own a bounded read against
                    # A.  Retire it at the same commit boundary that transfers
                    # the trusted service, before the queued Stage 5C bind
                    # creates B's coordinator.  This keeps stale work harmless
                    # and promptly releases Windows reader locks.
                    clear_derived = getattr(
                        derived_bridge,
                        "clear_database",
                        None,
                    )
                    if callable(clear_derived):
                        clear_derived()
                    DatabaseActionsMixin._retire_trusted_read_service(
                        self,
                        old_service,
                        force=True,
                    )
                if access_mode != TRUSTED_LIVE_MODE and owns_pending_service:
                    DatabaseActionsMixin._retire_trusted_read_service(
                        self,
                        pending_service,
                        force=True,
                    )

            if publication_error is not None:
                try:
                    rollback_path = publication_snapshot["database_path"]
                    rollback_instance = publication_snapshot["database_instance"]
                    rollback_current_instance = None
                    if (
                            rollback_path
                            and isinstance(rollback_instance, DatabaseInstance)
                            ):
                        rollback_current_instance = database_instance(rollback_path)
                        if _database_instances_or_sidecars_differ(
                                rollback_instance,
                                rollback_current_instance,
                                ):
                            publication_rollback_source_changed = True
                            raise DatabaseInstanceChangedError(
                                "The previously displayed database changed while "
                                "the replacement view was staged."
                            )
                    restored = (
                        DatabaseActionsMixin._restore_database_load_publication(
                            self,
                            publication_snapshot,
                            continue_loading=publication_is_current,
                        )
                    )
                    if restored is False or not publication_is_current():
                        publication_error = None
                        publication_source_changed = False
                        publication_aborted = True
                    else:
                        if rollback_current_instance is not None:
                            rollback_final_instance = database_instance(rollback_path)
                            if _database_instances_or_sidecars_differ(
                                    rollback_current_instance,
                                    rollback_final_instance,
                                    ):
                                publication_rollback_source_changed = True
                                raise DatabaseInstanceChangedError(
                                    "The previously displayed database changed "
                                    "while its view was restored."
                                )
                            DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
                                self,
                                rollback_final_instance,
                            )
                        publication_restored = True
                except Exception as rollback_exception:
                    publication_rollback_error = rollback_exception
                    publication_error = None
                    publication_source_changed = False
                    publication_aborted = True
                    # A partially restored controller must not retain either
                    # reader or publish a mixture of A and B.  This path uses
                    # only normal controller teardown and filesystem identity
                    # observations; it performs no database read.
                    DatabaseActionsMixin._retire_trusted_read_service(
                        self,
                        old_service,
                        force=True,
                    )
                    self.close_database(status=False)

            if publication_aborted:
                # close_database() may have cleared the tree during a nested
                # event and addRuns may then have appended one final row before
                # observing cancellation.  Leave the closed view truly empty.
                self.RunList.clearSelection()
                self.RunList.clear()
                self.RunList.watching = []
                self.RunList.maxRunId = 0
        finally:
            owned_pending = getattr(
                self,
                "_pending_trusted_read_services",
                {},
            ).pop(generation, None)
            if not publication_committed and owned_pending is not None:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    owned_pending,
                    force=True,
                )
            try:
                self.RunList.blockSignals(run_list_signals_blocked)
                self.run_idBox.blockSignals(run_id_signals_blocked)
            finally:
                try:
                    if (
                            preview_publication_gate_set
                            and callable(set_preview_publication_suspended)
                            ):
                        set_preview_publication_suspended(
                            preview_publication_was_suspended,
                        )
                finally:
                    self._database_load_publication_active = False
                    DatabaseActionsMixin._sync_database_generation_controls(self)

        if publication_aborted:
            if publication_rollback_error is not None:
                log_exception(
                    "Database view rollback failed",
                    publication_rollback_error,
                    __name__,
                )
                self.show_error(
                    "Database Recovery Failed",
                    "qPlot closed the database view because its previous "
                    "state could not be restored safely.",
                    str(publication_rollback_error),
                )
                if publication_rollback_source_changed:
                    retry_generation = getattr(
                        self,
                        "_database_load_generation",
                        0,
                    )
                    rollback_path = publication_snapshot["database_path"]
                    self.show_status(
                        "Previously displayed database changed; retrying...",
                        0,
                    )
                    QtCore.QTimer.singleShot(
                        0,
                        lambda: (
                            DatabaseActionsMixin
                            ._retry_replaced_database_if_current(
                                self,
                                retry_generation,
                                rollback_path,
                                load_started_at=load_started_at,
                            )
                        ),
                    )
            return

        if publication_restored:
            if derived_bridge is not None:
                derived_bridge.resume_publications()
            rollback_path = publication_snapshot["database_path"]
            rollback_runs = publication_snapshot["runs"]
            if rollback_path:
                self._start_database_detail_load(rollback_path, rollback_runs)
                if (
                        publication_snapshot["access_mode"] == TRUSTED_LIVE_MODE
                        and publication_snapshot["selected_run_guid"]
                        and callable(
                            getattr(self.RunList, "_item_for_guid", None)
                        )
                        ):
                    self._update_trusted_selected_run(
                        publication_snapshot["selected_run_guid"]
                    )

        if publication_error is not None:
            if owns_pending_service and pending_service is not old_service:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    pending_service,
                )
            if publication_source_changed:
                self._database_load_generation += 1
                retry_generation = self._database_load_generation
                self.show_status("Database changed while loading; retrying...", 0)
                QtCore.QTimer.singleShot(
                    0,
                    lambda: DatabaseActionsMixin._retry_replaced_database_if_current(
                        self,
                        retry_generation,
                        abspath,
                        generation_recovery=bool(state.get("generation_recovery")),
                        load_started_at=load_started_at,
                    ),
                )
                return
            log_exception(
                "Database view publication failed",
                publication_error,
                __name__,
            )
            self.show_error(
                "Database Load Failed",
                f"Could not publish database {abspath}.",
                str(publication_error),
            )
            DatabaseActionsMixin._resume_test_database_generation_recovery(self)
            apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
            if callable(apply_refresh_interval):
                apply_refresh_interval(self._current_refresh_interval())
            return

        final_instance = database_instance(abspath)
        if _database_instances_or_sidecars_differ(staged_instance, final_instance):
            self._reload_replaced_database(
                abspath,
                generation_recovery=bool(state.get("generation_recovery")),
            )
            return
        DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
            self,
            final_instance,
        )

        self._selected_run_detail_cache = {}
        self._selected_run_partial_detail_keys = set()
        if state.get("reload_same_path") and not state.get("replacement_reload"):
            invalidate_runtime_state = getattr(
                self,
                "_invalidate_database_runtime_state",
                None,
            )
            if callable(invalidate_runtime_state):
                invalidate_runtime_state(
                    publication_snapshot["database_instance"] or abspath
                )
        replacement_state = getattr(
            self,
            "_test_database_replacement_state",
            None,
        )
        transaction_matches = bool(
            replacement_state is not None
            and logical_database_path(replacement_state.database_path)
            == logical_database_path(abspath)
        )
        recovering_original = bool(
            transaction_matches
            and replacement_state.outcome == "recovering-original"
            and not _database_observations_differ(
                replacement_state.original_instance,
                accepted_instance,
            )
        )
        detached_terminal_transaction = bool(
            replacement_state is not None
            and not transaction_matches
            and not getattr(self, "_test_database_generation_active", False)
            and getattr(replacement_state, "outcome", None) is not None
        )
        if transaction_matches or detached_terminal_transaction:
            DatabaseActionsMixin._clear_test_database_generation_transaction(self)
        else:
            DatabaseActionsMixin._sync_database_generation_controls(self)
        if recovering_original:
            self._restore_test_database_replacement_selection(replacement_state)
        else:
            self.select_default_run()
        prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
        if (
                access_mode != TRUSTED_LIVE_MODE
                and callable(prioritize_previews)
                ):
            prioritize_previews()
        self._sync_empty_state()
        apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
        if callable(apply_refresh_interval):
            apply_refresh_interval(self._current_refresh_interval())

        elapsed = perf_counter() - load_started_at
        self.remember_loaded_database(abspath)
        run_count = self.RunList.topLevelItemCount()
        if state.get("replacement_reload"):
            run_word = "run" if run_count == 1 else "runs"
            status = (
                f"Database was replaced and reloaded: "
                f"{os.path.basename(abspath)} ({run_count} {run_word})."
            )
            if replacement_wal_is_quarantined(abspath):
                status += " WAL sidecars are ignored for this replacement view."
        elif run_count == 0:
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
        if access_mode == TRUSTED_LIVE_MODE:
            if derived_bridge is not None:
                QtCore.QTimer.singleShot(
                    0,
                    lambda: DatabaseActionsMixin._start_trusted_derived_bridge(
                        self,
                        generation,
                        abspath,
                    ),
                )
            else:
                self._start_database_detail_load(abspath, runs)
        else:
            if derived_bridge is not None:
                derived_bridge.clear_database()
            self._start_database_detail_load(abspath, runs)


    def _start_trusted_derived_bridge(self, generation, database_path):
        """Start Stage 5C only after the cheap run-list commit returned to Qt."""

        if generation != getattr(self, "_database_load_generation", 0):
            return
        if getattr(self, "_database_access_mode", None) != TRUSTED_LIVE_MODE:
            return
        if not _database_paths_equal(database_path, self.fileTextbox.text()):
            return
        if (
                getattr(self, "_shutdown_started", False)
                or getattr(self, "_shutdown_ready", False)
                ):
            return
        instance = getattr(self, "_loaded_database_instance", None)
        service = DatabaseActionsMixin._active_trusted_service_for_instance(
            self,
            instance,
        )
        bridge = getattr(self, "_trusted_derived_bridge", None)
        if (
                not isinstance(instance, DatabaseInstance)
                or service is None
                or bridge is None
                ):
            return
        current = database_instance(database_path)
        if _database_instances_or_sidecars_differ(instance, current):
            self._reload_replaced_database(database_path)
            return
        DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
            self,
            current,
        )
        bridge.bind_database(
            getattr(self, "_loaded_database_instance", current),
            self.RunList.all_run_metadata(),
            service,
        )


    def _restore_test_database_replacement_selection(self, state):
        """Restore the unchanged source's selected run and controller fields."""
        measurement_box = getattr(self, "measurementBox", None)
        if measurement_box is not None:
            measurement_box.setText(state.measurement_text)

        item = None
        item_for_guid = getattr(self.RunList, "_item_for_guid", None)
        if state.selected_guid and callable(item_for_guid):
            item = item_for_guid(state.selected_guid)
        if item is None and state.selected_run_id is not None:
            find_items = getattr(self.RunList, "findItems", None)
            if callable(find_items):
                matches = find_items(
                    str(state.selected_run_id),
                    QtCore.Qt.MatchFlag.MatchExactly,
                    0,
                )
                if matches:
                    item = matches[0]

        if item is not None:
            self.RunList.setCurrentItem(item)
            self.RunList.scrollToItem(
                item,
                qtw.QAbstractItemView.ScrollHint.PositionAtCenter,
            )
        elif state.run_id_text:
            self.run_idBox.setText(state.run_id_text)
        else:
            self.select_default_run()

        if state.details_tab_index is not None:
            set_current_index = getattr(self.infoBox, "setCurrentIndex", None)
            if callable(set_current_index):
                set_current_index(state.details_tab_index)


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
        self._database_detail_instance = None
        self._database_expensive_detail_generation = (
            getattr(self, "_database_expensive_detail_generation", 0) + 1
            )
        self._database_expensive_detail_active = False
        self._database_expensive_detail_worker = None
        self._database_expensive_detail_instance = None


    def _start_database_detail_load(self, abspath, runs):
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                abspath,
                ):
            return
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            bridge = getattr(self, "_trusted_derived_bridge", None)
            if bridge is not None:
                if bridge.coordinator is not None:
                    bridge.reconcile_runs(self.RunList.all_run_metadata())
                    bridge.request_priority_update()
                return
        run_ids = self._database_detail_run_order(runs)
        if not run_ids:
            return

        detail_instance = getattr(self, "_loaded_database_instance", None)
        if not isinstance(detail_instance, DatabaseInstance):
            detail_instance = database_instance(abspath)

        trusted_service = DatabaseActionsMixin._active_trusted_service_for_instance(
            self,
            detail_instance,
        )
        if (
                getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE
                and trusted_service is None
                ):
            DatabaseActionsMixin._cancel_database_detail_load(self)
            self.show_error(
                "Trusted Details Unavailable",
                "The accepted trusted live-reader session is no longer usable.",
                "Reload the database to start a new trusted session. qPlot did "
                "not fall back to snapshot metadata after the accepted-session "
                "failure.",
            )
            return
        worker_kwargs = {
            "expected_database_instance": detail_instance,
        }
        if trusted_service is not None:
            worker_kwargs["trusted_service"] = trusted_service

        DatabaseActionsMixin._start_database_cheap_detail_worker(
            self,
            abspath,
            run_ids,
            detail_instance,
            worker_kwargs,
        )
        DatabaseActionsMixin._start_database_expensive_detail_worker(
            self,
            abspath,
            run_ids,
            detail_instance,
            worker_kwargs,
        )
        QtCore.QTimer.singleShot(0, self._prioritize_database_detail_runs)


    def _start_database_cheap_detail_worker(
            self,
            abspath,
            run_ids,
            detail_instance,
            worker_kwargs,
            ):
        if (
                getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE
                and (
                    getattr(self, "_trusted_derived_bridge", None) is not None
                    or worker_kwargs.get("trusted_service") is None
                )
                ):
            return
        prior_worker = getattr(self, "_database_detail_worker", None)
        if prior_worker is not None:
            prior_worker.cancel()
        self._database_detail_generation = (
            getattr(self, "_database_detail_generation", 0) + 1
        )
        generation = self._database_detail_generation
        self._database_detail_active = True
        self._database_detail_instance = detail_instance

        worker = DatabaseDetailWorker(
            generation,
            abspath,
            run_ids,
            batch_size=100,
            **worker_kwargs,
        )
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


    def _start_database_expensive_detail_worker(
            self,
            abspath,
            run_ids,
            detail_instance,
            worker_kwargs,
            ):
        if (
                getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE
                and (
                    getattr(self, "_trusted_derived_bridge", None) is not None
                    or worker_kwargs.get("trusted_service") is None
                )
                ):
            return
        prior_worker = getattr(self, "_database_expensive_detail_worker", None)
        if prior_worker is not None:
            prior_worker.cancel()
        self._database_expensive_detail_generation = (
            getattr(self, "_database_expensive_detail_generation", 0) + 1
        )
        expensive_generation = self._database_expensive_detail_generation
        self._database_expensive_detail_active = True
        self._database_expensive_detail_instance = detail_instance
        priority_run_ids = self._database_detail_priority_run_ids()
        expensive_worker = DatabaseExpensiveDetailWorker(
            expensive_generation,
            abspath,
            run_ids,
            batch_size=100,
            **worker_kwargs,
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
            getattr(self, "databaseDetailThreadPool", self.databaseLoadThreadPool),
            )
        expensive_thread_pool.start(expensive_worker)


    def _database_detail_run_order(self, runs):
        def sort_key(run_id):
            try:
                return int(run_id)
            except (TypeError, ValueError):
                return 0

        return sorted((runs or {}).keys(), key=sort_key, reverse=True)


    def _prioritize_database_detail_runs(self, run_ids=None):
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            bridge = getattr(self, "_trusted_derived_bridge", None)
            if bridge is not None:
                bridge.request_priority_update()
                return
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
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                abspath,
                ):
            return
        if not _database_paths_equal(abspath, self.fileTextbox.text()):
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                abspath,
                getattr(self, "_database_detail_instance", None),
                ):
            return

        detail_instance = getattr(self, "_database_detail_instance", None)
        self._apply_database_detail_batch(runs)
        DatabaseActionsMixin._reload_if_worker_database_instance_changed(
            self,
            abspath,
            detail_instance,
            discard_stale_publication=True,
        )


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
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                abspath,
                ):
            return
        if not _database_paths_equal(abspath, self.fileTextbox.text()):
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                abspath,
                getattr(self, "_database_expensive_detail_instance", None),
                ):
            return

        detail_instance = getattr(
            self,
            "_database_expensive_detail_instance",
            None,
        )
        self._apply_database_detail_batch(runs)
        DatabaseActionsMixin._reload_if_worker_database_instance_changed(
            self,
            abspath,
            detail_instance,
            discard_stale_publication=True,
        )


    def _apply_database_detail_batch(self, runs):
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(self):
            return
        updated_runs = self.RunList.updateRuns(runs)
        if not updated_runs:
            return

        if getattr(self, "_database_access_mode", None) != TRUSTED_LIVE_MODE:
            self.infoBox.preview.add_runs(updated_runs, queue_previews=False)
            prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
            if callable(prioritize_previews):
                prioritize_previews()
        self._refresh_selected_run_details(updated_runs)

    @QtCore.pyqtSlot(int, str, object)
    def database_detail_finished(self, generation, abspath, error):
        if generation != getattr(self, "_database_detail_generation", 0):
            return

        detail_instance = getattr(self, "_database_detail_instance", None)
        self._database_detail_active = False
        self._database_detail_worker = None
        self._database_detail_instance = None

        if not _database_paths_equal(abspath, self.fileTextbox.text()):
            return

        if isinstance(error, DatabaseInstanceChangedError):
            DatabaseActionsMixin._cancel_database_detail_load(self)
            self._reload_replaced_database(abspath)
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
            self,
            abspath,
            detail_instance,
        ):
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

        detail_instance = getattr(
            self,
            "_database_expensive_detail_instance",
            None,
        )
        self._database_expensive_detail_active = False
        self._database_expensive_detail_worker = None
        self._database_expensive_detail_instance = None

        if not _database_paths_equal(abspath, self.fileTextbox.text()):
            return

        if isinstance(error, DatabaseInstanceChangedError):
            DatabaseActionsMixin._cancel_database_detail_load(self)
            self._reload_replaced_database(abspath)
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
            self,
            abspath,
            detail_instance,
        ):
            return

        if error is not None:
            log_exception("Expensive database detail load failed", error, __name__)
            self.show_status(f"Setpoint and size loading failed: {error}", 5000)
            return

        if not getattr(self, "_database_detail_active", False):
            self.show_status("Run details loaded.", 5000)


    def _refresh_selected_run_details(self, runs, *, live_only=False):
        guid = getattr(self, "_selected_run_guid", None)
        selected_key = getattr(self, "_selected_dataset_key", None)
        if not guid:
            guid = getattr(selected_key, "guid", None)
        if not guid:
            guid = getattr(getattr(self, "ds", None), "guid", None)
        if not guid:
            return

        for metadata in runs.values():
            if metadata.get("guid") == guid:
                if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
                    if not live_only:
                        return
                    cache = getattr(self, "_selected_run_detail_cache", {})
                    partial_keys = getattr(
                        self,
                        "_selected_run_partial_detail_keys",
                        set(),
                    )
                    for key in tuple(cache):
                        if key and key[-1] == guid:
                            cache.pop(key, None)
                            partial_keys.discard(key)
                    self.updateSelected(guid)
                elif live_only:
                    update_live = getattr(
                        self.infoBox,
                        "update_live_run_details",
                        None,
                        )
                    if callable(update_live):
                        update_live(metadata)
                else:
                    self.updateSelected(guid)
                return


    def _selected_run_publication_instance(
            self,
            instance,
            guid,
            run_id,
            generation,
            ):
        """Return the currently bound source for one still-current selection.

        Widget setters and run-list updates can enter a nested Qt event loop.
        The generation/GUID/run checks therefore belong on every side of each
        publication boundary, not only at worker callback dispatch.
        """
        if generation != getattr(self, "_database_selected_run_generation", 0):
            return None
        if str(guid) != getattr(self, "_selected_run_guid", None):
            return None
        try:
            selected_run_id = int(getattr(self, "selected_run_id", -1))
            expected_run_id = int(run_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if selected_run_id != expected_run_id:
            return None
        if not isinstance(instance, DatabaseInstance):
            return None
        if not _database_paths_equal(
                self.fileTextbox.text(),
                instance.logical_path,
                ):
            return None
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                instance.logical_path,
                instance,
                discard_stale_publication=True,
                ):
            return None
        current_instance = getattr(self, "_loaded_database_instance", None)
        if not isinstance(current_instance, DatabaseInstance):
            return None
        return current_instance


    def _restore_current_selected_run_view(self):
        """Re-render the newest selection after a nested stale publication.

        A stale setter may have resumed after selection changed underneath it.
        Reapplying the current cached detail (or its immediate loading model)
        prevents the older detail from remaining visible.  This method never
        opens SQLite and never starts another worker.
        """
        if getattr(self, "_restoring_selected_run_publication", False):
            self._selected_run_restore_pending = True
            return False
        self._restoring_selected_run_publication = True
        try:
            for _attempt in range(4):
                self._selected_run_restore_pending = False
                generation = getattr(
                    self,
                    "_database_selected_run_generation",
                    0,
                )
                guid = getattr(self, "_selected_run_guid", None)
                run_id = getattr(self, "selected_run_id", None)
                instance = getattr(self, "_loaded_database_instance", None)
                if not guid or not isinstance(instance, DatabaseInstance):
                    break

                item_for_guid = getattr(self.RunList, "_item_for_guid", None)
                item = item_for_guid(guid) if callable(item_for_guid) else None
                metadata = dict(getattr(item, "run_metadata", {}) or {})
                metadata.setdefault("guid", str(guid))
                metadata.setdefault("run_id", run_id)
                cache_key = _selected_run_detail_cache_key(instance, run_id, guid)
                detail = getattr(self, "_selected_run_detail_cache", {}).get(
                    cache_key
                )
                access_mode = getattr(self, "_database_access_mode", None)
                if access_mode == SNAPSHOT_FALLBACK_MODE:
                    setter_name = "set_snapshot_run_unavailable"
                    setter_value = metadata
                elif detail is not None:
                    setter_name = "set_trusted_run_detail"
                    setter_value = detail
                else:
                    setter_name = "set_trusted_run_loading"
                    setter_value = metadata
                setter = getattr(self.infoBox, setter_name, None)
                if not callable(setter):
                    break
                setter(setter_value)
                if (
                        not getattr(self, "_selected_run_restore_pending", False)
                        and DatabaseActionsMixin._selected_run_publication_instance(
                            self,
                            instance,
                            guid,
                            run_id,
                            generation,
                        ) is not None
                        ):
                    return True

            clear = getattr(self.infoBox, "clear", None)
            if callable(clear):
                clear()
            return False
        finally:
            self._selected_run_restore_pending = False
            self._restoring_selected_run_publication = False


    def _update_snapshot_selected_run(self, guid):
        """Publish cached basics only for one snapshot-fallback selection.

        Opening the retained fallback reader prepares a complete private
        snapshot. Ordinary selection must never pay that potentially enormous
        copy cost, even on a worker thread. Rich fallback detail therefore
        remains unavailable until an explicit action (or a later stage).
        """
        instance = getattr(self, "_loaded_database_instance", None)
        database_path = (
            instance.logical_path
            if isinstance(instance, DatabaseInstance)
            else self.fileTextbox.text()
        )
        if (
                database_path
                and DatabaseActionsMixin._reload_if_database_instance_changed(
                    self,
                    database_path,
                )
                ):
            return True
        instance = getattr(self, "_loaded_database_instance", instance)

        item = self.RunList._item_for_guid(guid)
        if item is None:
            return False
        run_id = self.RunList.run_id_for_guid(guid)
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            return False

        DatabaseActionsMixin._cancel_selected_run_detail(self)
        generation = self._database_selected_run_generation
        metadata = dict(getattr(item, "run_metadata", {}) or {})
        metadata.setdefault("run_id", run_id)
        metadata.setdefault("guid", str(guid))

        self._selected_run_guid = str(guid)
        self.selected_run_id = run_id
        release_selected = getattr(self, "_release_selected_dataset", None)
        if callable(release_selected):
            release_selected()
        else:
            self.ds = None
            self._selected_dataset_key = None
        blocked = self.run_idBox.blockSignals(True)
        try:
            self.run_idBox.setText(str(run_id))
        finally:
            self.run_idBox.blockSignals(blocked)

        prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
        if callable(prioritize_previews):
            prioritize_previews([run_id])

        if isinstance(instance, DatabaseInstance):
            instance = DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                run_id,
                generation,
            )
            if instance is None:
                DatabaseActionsMixin._restore_current_selected_run_view(self)
                return True

        if not isinstance(instance, DatabaseInstance):
            set_error = getattr(self.infoBox, "set_snapshot_run_error", None)
            if callable(set_error):
                set_error(
                    "The accepted snapshot source is no longer available. "
                    "Reload the database to continue.",
                    metadata,
                )
            return True

        set_unavailable = getattr(
            self.infoBox,
            "set_snapshot_run_unavailable",
            None,
        )
        if callable(set_unavailable):
            set_unavailable(metadata)
        else:
            set_error = getattr(self.infoBox, "set_snapshot_run_error", None)
            if callable(set_error):
                set_error(
                    "Detailed run metadata is unavailable during ordinary "
                    "snapshot-fallback selection. Use an explicit plot or "
                    "CSV action to materialise this run.",
                    metadata,
                )
        instance = DatabaseActionsMixin._selected_run_publication_instance(
            self,
            instance,
            guid,
            run_id,
            generation,
        )
        if instance is None:
            DatabaseActionsMixin._restore_current_selected_run_view(self)
            return True
        self._show_selected_run_status(run_id, self._run_point_count(metadata))
        if DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                run_id,
                generation,
                ) is None:
            DatabaseActionsMixin._restore_current_selected_run_view(self)
        return True


    def _update_trusted_selected_run(self, guid):
        """Select and progressively render one run without opening a DataSet."""
        instance = getattr(self, "_loaded_database_instance", None)
        database_path = (
            instance.logical_path
            if isinstance(instance, DatabaseInstance)
            else self.fileTextbox.text()
        )
        if (
                database_path
                and DatabaseActionsMixin._reload_if_database_instance_changed(
                    self,
                    database_path,
                )
                ):
            return True
        instance = getattr(self, "_loaded_database_instance", instance)

        item = self.RunList._item_for_guid(guid)
        if item is None:
            return False
        run_id = self.RunList.run_id_for_guid(guid)
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            return False

        DatabaseActionsMixin._cancel_selected_run_detail(self)
        generation = self._database_selected_run_generation
        metadata = dict(getattr(item, "run_metadata", {}) or {})
        metadata.setdefault("run_id", run_id)
        metadata.setdefault("guid", str(guid))

        self._selected_run_guid = str(guid)
        self.selected_run_id = run_id
        release_selected = getattr(self, "_release_selected_dataset", None)
        if callable(release_selected):
            release_selected()
        else:
            self.ds = None
            self._selected_dataset_key = None
        blocked = self.run_idBox.blockSignals(True)
        try:
            self.run_idBox.setText(str(run_id))
        finally:
            self.run_idBox.blockSignals(blocked)

        set_loading = getattr(self.infoBox, "set_trusted_run_loading", None)
        if callable(set_loading):
            set_loading(metadata)
        bridge = getattr(self, "_trusted_derived_bridge", None)
        if bridge is None:
            self._prioritize_database_detail_runs([run_id])

        if isinstance(instance, DatabaseInstance):
            instance = DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                run_id,
                generation,
            )
            if instance is None:
                DatabaseActionsMixin._restore_current_selected_run_view(self)
                return True

        service = DatabaseActionsMixin._active_trusted_service_for_instance(
            self,
            instance,
        )
        if not isinstance(instance, DatabaseInstance) or service is None:
            set_error = getattr(self.infoBox, "set_trusted_run_error", None)
            if callable(set_error):
                set_error(
                    "The accepted trusted session is no longer available. "
                    "Reload the database to continue.",
                    metadata,
                )
            return True
        if bridge is not None:
            bridge.select_run(str(guid))
            return True

        # Non-MainWindow compatibility harnesses retain the Stage 4 path.
        # Every production MainWindow owns the Stage 5C bridge, so this branch
        # cannot compete with coordinator-owned trusted work in the app.
        cache_key = _selected_run_detail_cache_key(instance, run_id, guid)
        cached = getattr(self, "_selected_run_detail_cache", {}).get(cache_key)
        if cached is not None:
            if not DatabaseActionsMixin._publish_trusted_selected_run_detail(
                    self,
                    instance,
                    guid,
                    cached,
                    detail_complete=(
                        cache_key not in getattr(
                            self,
                            "_selected_run_partial_detail_keys",
                            set(),
                        )
                    ),
                    access_mode=TRUSTED_LIVE_MODE,
                    generation=generation,
                    ):
                DatabaseActionsMixin._restore_current_selected_run_view(self)
                return True
            partial_keys = getattr(
                self,
                "_selected_run_partial_detail_keys",
                set(),
            )
            if cache_key not in partial_keys:
                return True

        worker = DatabaseSelectedRunWorker(
            generation,
            instance.logical_path,
            run_id,
            str(guid),
            service,
            expected_database_instance=instance,
        )
        self._database_selected_run_worker = worker
        self._database_selected_run_instance = instance
        self._database_selected_run_mode = TRUSTED_LIVE_MODE
        worker.signals.progress.connect(self.database_selected_run_progress)
        worker.signals.finished.connect(self.database_selected_run_finished)
        pool = getattr(
            self,
            "databaseSelectedRunThreadPool",
            self.databaseDetailThreadPool,
        )
        pool.start(worker)
        return True


    @QtCore.pyqtSlot(int, str, str, object)
    def database_selected_run_progress(
            self,
            generation,
            database_path,
            guid,
            detail,
            ):
        """Publish cheap selected-run fields while expensive work continues."""
        if generation != getattr(self, "_database_selected_run_generation", 0):
            return
        instance = getattr(self, "_database_selected_run_instance", None)
        if getattr(self, "_database_selected_run_mode", None) != TRUSTED_LIVE_MODE:
            return
        if guid != getattr(self, "_selected_run_guid", None):
            return
        if not _database_paths_equal(database_path, self.fileTextbox.text()):
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                database_path,
                instance,
                ):
            return
        instance = getattr(self, "_loaded_database_instance", instance)
        DatabaseActionsMixin._publish_trusted_selected_run_detail(
            self,
            instance,
            guid,
            detail,
            detail_complete=False,
            access_mode=TRUSTED_LIVE_MODE,
            generation=generation,
        )


    @QtCore.pyqtSlot(int, str, str, object, object)
    def database_selected_run_finished(
            self,
            generation,
            database_path,
            guid,
            detail,
            error,
            ):
        if generation != getattr(self, "_database_selected_run_generation", 0):
            return
        instance = getattr(self, "_database_selected_run_instance", None)
        access_mode = getattr(self, "_database_selected_run_mode", None)
        self._database_selected_run_worker = None
        self._database_selected_run_instance = None
        self._database_selected_run_mode = None
        if guid != getattr(self, "_selected_run_guid", None):
            return
        if not _database_paths_equal(database_path, self.fileTextbox.text()):
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                database_path,
                instance,
                ):
            return
        instance = getattr(self, "_loaded_database_instance", instance)

        run_metadata = self._run_metadata_for_guid(guid)
        if error is not None:
            if isinstance(error, DatabaseInstanceChangedError):
                log_exception("Selected-run detail failed", error, __name__)
                self._reload_replaced_database(database_path)
                return
            error = bounded_presentation_error(error)
            log_exception("Selected-run detail failed", error, __name__)
            cache_key = _selected_run_detail_cache_key(
                instance,
                getattr(self, "selected_run_id", None),
                guid,
            )
            cached = getattr(self, "_selected_run_detail_cache", {}).get(cache_key)
            partial_keys = getattr(
                self,
                "_selected_run_partial_detail_keys",
                set(),
            )
            service = (
                DatabaseActionsMixin._active_trusted_service_for_instance(
                    self,
                    instance,
                )
                if access_mode == TRUSTED_LIVE_MODE
                else None
            )
            if cached is not None and cache_key in partial_keys and service is not None:
                if not DatabaseActionsMixin._publish_trusted_selected_run_detail(
                        self,
                        instance,
                        guid,
                        cached,
                        detail_complete=False,
                        access_mode=TRUSTED_LIVE_MODE,
                        generation=generation,
                        ):
                    DatabaseActionsMixin._restore_current_selected_run_view(self)
                    return
                self.show_status(
                    f"Selected-run summaries failed; basic details remain: {error}",
                    5000,
                )
                if DatabaseActionsMixin._selected_run_publication_instance(
                        self,
                        instance,
                        guid,
                        getattr(self, "selected_run_id", None),
                        generation,
                        ) is None:
                    DatabaseActionsMixin._restore_current_selected_run_view(self)
                return
            error_method = (
                "set_snapshot_run_error"
                if access_mode == SNAPSHOT_FALLBACK_MODE
                else "set_trusted_run_error"
            )
            set_error = getattr(self.infoBox, error_method, None)
            if callable(set_error):
                set_error(error, run_metadata)
            instance = DatabaseActionsMixin._selected_run_publication_instance(
                    self,
                    instance,
                    guid,
                    getattr(self, "selected_run_id", None),
                    generation,
                    )
            if instance is None:
                DatabaseActionsMixin._restore_current_selected_run_view(self)
                return
            self.show_status(f"Selected-run details failed: {error}", 5000)
            if DatabaseActionsMixin._selected_run_publication_instance(
                    self,
                    instance,
                    guid,
                    getattr(self, "selected_run_id", None),
                    generation,
                    ) is None:
                DatabaseActionsMixin._restore_current_selected_run_view(self)
            return
        if detail is None or not isinstance(instance, DatabaseInstance):
            return

        DatabaseActionsMixin._publish_trusted_selected_run_detail(
            self,
            instance,
            guid,
            detail,
            detail_complete=True,
            access_mode=access_mode,
            generation=generation,
        )


    def _publish_trusted_selected_run_detail(
            self,
            instance,
            guid,
            detail,
            *,
            detail_complete,
            access_mode=None,
            generation=None,
            ):
        """Apply one current selected-run view model without database I/O."""
        if detail is None or not isinstance(instance, DatabaseInstance):
            return False
        if generation is None:
            generation = getattr(self, "_database_selected_run_generation", 0)
        detail_metadata = detail.run.as_dict()
        detail_guid_value = detail_metadata.get("guid")
        if detail_guid_value is None:
            return False
        detail_guid = str(detail_guid_value)
        if (
                detail_guid != guid
                or guid != getattr(self, "_selected_run_guid", None)
                or detail.run.run_id != self.selected_run_id
                ):
            return False
        instance = DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                detail.run.run_id,
                generation,
                )
        if instance is None:
            return False

        def discard_stale_cache(candidate_instance):
            cache = getattr(self, "_selected_run_detail_cache", {})
            partial_keys = getattr(
                self,
                "_selected_run_partial_detail_keys",
                set(),
            )
            stale_key = _selected_run_detail_cache_key(
                candidate_instance,
                detail.run.run_id,
                guid,
            )
            cache.pop(stale_key, None)
            partial_keys.discard(stale_key)

        self.RunList.updateRuns({detail.run.run_id: detail_metadata})
        guarded_instance = (
            DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                detail.run.run_id,
                generation,
            )
        )
        if guarded_instance is None:
            discard_stale_cache(instance)
            DatabaseActionsMixin._restore_current_selected_run_view(self)
            return False
        instance = guarded_instance

        if access_mode == SNAPSHOT_FALLBACK_MODE:
            self.infoBox.set_snapshot_run_detail(detail)
        else:
            self.infoBox.set_trusted_run_detail(detail)
        guarded_instance = DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                detail.run.run_id,
                generation,
        )
        if guarded_instance is None:
            discard_stale_cache(instance)
            DatabaseActionsMixin._restore_current_selected_run_view(self)
            return False
        instance = guarded_instance

        cache = getattr(self, "_selected_run_detail_cache", None)
        if cache is None:
            cache = {}
            self._selected_run_detail_cache = cache
        cache_key = _selected_run_detail_cache_key(
            instance,
            detail.run.run_id,
            guid,
        )
        cache[cache_key] = detail
        partial_keys = getattr(self, "_selected_run_partial_detail_keys", None)
        if partial_keys is None:
            partial_keys = set()
            self._selected_run_partial_detail_keys = partial_keys
        if detail_complete:
            partial_keys.discard(cache_key)
        else:
            partial_keys.add(cache_key)

        self._show_selected_run_status(
            detail.run.run_id,
            self._run_point_count(detail_metadata),
        )
        if DatabaseActionsMixin._selected_run_publication_instance(
                self,
                instance,
                guid,
                detail.run.run_id,
                generation,
                ) is None:
            cache.pop(cache_key, None)
            partial_keys.discard(cache_key)
            DatabaseActionsMixin._restore_current_selected_run_view(self)
            return False
        return True


    def _show_selected_run_status(self, run_id, point_count):
        if point_count is None:
            self.show_status(f"Selected run {run_id}.", 5000)
        else:
            self.show_status(
                f"Selected run {run_id} with {int(point_count):,} points.",
                5000,
            )


    def select_default_run(self):
        """
        Select the first visible run so the details pane is not left empty.

        """
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(self):
            return
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
