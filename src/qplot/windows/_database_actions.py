from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, perf_counter
from typing import TYPE_CHECKING

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QDesktopServices
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling.database import (
    DatabaseDetailWorker,
    DatabaseExpensiveDetailWorker,
    DatabaseInfoWorker,
    DatabaseRefreshWorker,
)
from qplot.datahandling.file_identity import (
    DatabaseInstance,
    database_instance,
    database_instances_differ,
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
from qplot.diagnostics import log_event, log_exception
from qplot.windows._database_detail_state import (
    BASIC_METADATA_COLUMNS,
    EXPENSIVE_METADATA_COLUMNS,
    detail_channel,
    set_run_metadata_loading,
)

from ._auto_plot import cancel_auto_plot, enqueue_auto_plot, update_auto_plot
from ._config_persistence import persist_config_value
from ._database_load_controller import DatabaseLoadController
from ._database_load_publication import DatabaseLoadPublication
from ._database_load_state import DatabaseLoadSession, DatabaseViewSnapshot
from ._database_load_ui import DatabaseLoadUi
from ._database_observation import (
    database_instances_or_sidecars_differ as _database_instances_or_sidecars_differ,
)
from ._database_refresh_controller import (
    DatabaseRefreshController,
    RefreshPublicationAborted,
)
from ._database_refresh_ui import DatabaseRefreshUi
from ._dataset_handle import canonical_database_path
from ._small_dialogs import (
    close_default,
    dialog_layout,
    equal_button_widths,
    resize_dialog,
)
from ._widgets.details_tables import (
    CopyableTableWidget,
    copy_to_clipboard,
    format_value,
)

if TYPE_CHECKING:
    from qplot.datahandling.database import DatabaseLoadWorker
    from qplot.datahandling.trusted_live_service import TrustedLiveReadService


@dataclass(frozen=True)
class _SelectedRunState:
    """Generation and cached metadata captured before selection callbacks."""

    generation: int
    metadata: dict[str, object]


def _database_paths_equal(first, second):
    """Compare database paths using the platform's logical path spelling."""
    if not first or not second:
        return False
    try:
        return logical_database_path(first) == logical_database_path(second)
    except (TypeError, ValueError, OSError):
        return False


class DatabaseInfoDialog(qtw.QDialog):
    """
    Copyable table dialog for current database diagnostics.

    """

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self._rows = [(format_value(label), format_value(value)) for label, value in rows]
        self.setObjectName("databaseInfoDialog")
        self.setWindowTitle("Database information")
        layout = dialog_layout(self)

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
        self.table.setAccessibleName("Database information")
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
        close = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
        assert close is not None
        equal_button_widths(self.copyButton, close)
        close_default(buttons)
        resize_dialog(self, 720, 360)


    def _setup_table(self):
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Field", "Value"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setSectionResizeMode(
            qtw.QHeaderView.ResizeMode.ResizeToContents
        )
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


    def _table_item(self, value):
        item = qtw.QTableWidgetItem(value)
        item.setToolTip(value)
        return item


    def copyAll(self):
        copy_to_clipboard(_database_info_rows_clipboard_text(self._rows))


def _database_info_rows_clipboard_text(rows):
    return "\n".join("\t".join(row) for row in rows)


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


class DatabaseActionsMixin:
    """
    Database loading, refresh, recent-file, and database-status actions.

    The mixin expects the owning window to provide the widgets and state created
    by MainWindow, plus show_status(), show_error(), and openPlot().
    """

    _database_loader: DatabaseLoadController

    def _init_database_loader(self) -> None:
        self._database_loader = DatabaseLoadController(
            DatabaseLoadUi(self),
            observe_instance=lambda path: database_instance(path),
            schedule=lambda callback: QtCore.QTimer.singleShot(0, callback),
        )

    # Read-only observations for the neighbouring refresh, export and selection
    # components. Pending ownership and all mutations belong to the controller.
    @property
    def _database_load_generation(self) -> int:
        return self._database_loader.generation

    @property
    def _database_load_active(self) -> bool:
        return self._database_loader.active

    @property
    def _database_load_state(self) -> DatabaseLoadSession | None:
        return self._database_loader.session

    @property
    def _database_load_worker(self) -> DatabaseLoadWorker | None:
        return self._database_loader.worker

    @property
    def _database_load_publication_active(self) -> bool:
        return self._database_loader.publication_active

    @property
    def _pending_trusted_read_services(self) -> Mapping[int, TrustedLiveReadService]:
        return self._database_loader.pending_services

    _database_refresher: DatabaseRefreshController

    def _init_database_refresher(self) -> None:
        ui = DatabaseRefreshUi(self)
        self._database_refresher = DatabaseRefreshController(
            ui, schedule=ui.schedule,
        )

    @property
    def _database_refresh_generation(self) -> int:
        return self._database_refresher.generation

    @property
    def _database_refresh_active(self) -> bool:
        return self._database_refresher.active

    @property
    def _database_refresh_worker(self) -> DatabaseRefreshWorker | None:
        return self._database_refresher.worker

    @property
    def _database_refresh_pending(self) -> bool:
        return self._database_refresher.pending

    @property
    def _database_refresh_instance(self) -> DatabaseInstance | None:
        return self._database_refresher.instance

    @property
    def _database_refresh_staged_new_runs(self):
        return self._database_refresher.staged_runs

    @property
    def _database_refresh_publication_active(self) -> bool:
        return self._database_refresher.publication_active

    _database_info_worker: DatabaseInfoWorker | None
    _database_info_instance: DatabaseInstance | None

    def _reap_retired_trusted_read_services(self):
        """Zero-wait reap retired brokers and drive polling only while needed."""
        retired = getattr(self, "_retired_trusted_read_services", None)
        diagnostics = getattr(self, "_retired_service_reap_diagnostics", None)
        if diagnostics is None:
            diagnostics = {}
            self._retired_service_reap_diagnostics = diagnostics
        if retired is not None:
            for service in tuple(retired):
                if getattr(service, "_qplot_retained_by_plots", False):
                    if any(getattr(handle.dataset, "service", None) is service
                           for handle in getattr(self, "dataset_holder", {}).values()):
                        continue
                    service._qplot_retained_by_plots = False
                    service.close_async()
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
        keep_plots = not force and any(
            getattr(handle.dataset, "service", None) is service
            for handle in getattr(self, "dataset_holder", {}).values()
        )
        if keep_plots:
            service._qplot_retained_by_plots = True
        elif (not already_retiring
              or getattr(service, "_qplot_retained_by_plots", False)):
            service._qplot_retained_by_plots = False
            service.close_async()
        DatabaseActionsMixin._reap_retired_trusted_read_services(self)


    def _retire_all_trusted_read_services(self):
        """Invalidate active and pending sessions without blocking Qt."""
        active = getattr(self, "_trusted_read_service", None)
        self._trusted_read_service = None
        pending = self._database_loader.take_pending_services()
        for service in {active, *pending}:
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
        """Invalidate synchronous selection publication; the bridge owns work."""
        self._database_selected_run_generation = (
            getattr(self, "_database_selected_run_generation", 0) + 1
        )

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
        enabled = not blocked and not load_active and not getattr(self, "_shutdown_started", False)
        sync_available = getattr(self, "_sync_available_actions", None)
        if callable(sync_available):
            sync_available(enabled)
        else:
            for attr in (
                "refreshDatabaseButton", "databaseInfoButton", "emptyStateRefreshButton",
                "plotRunButton", "exportCsvButton", "run_idBox", "measurementBox",
                "RunList", "infoBox", "autoPlotBox",
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
        state = getattr(self, "_database_load_state", None)
        pending_path = state.abspath if state is not None else None
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
        from ._export_tasks import cancel_owner_exports

        cancel_owner_exports(self)
        cancel_auto_plot(self)
        self._database_loader.invalidate()
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.clear_database()
        cancel_detail_load = getattr(self, "_cancel_database_detail_load", None)
        if callable(cancel_detail_load):
            cancel_detail_load()
        DatabaseActionsMixin._cancel_database_refresh(self)
        DatabaseActionsMixin._cancel_selected_run_detail(self)
        DatabaseActionsMixin._cancel_database_info(self)
        DatabaseActionsMixin._retire_all_trusted_read_services(self)

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
        if getattr(self, "_database_load_active", False):
            self.show_status("Wait for database loading to finish before reading information.", 5000)
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

        if getattr(self, "_database_info_active", False):
            self.show_status("Database information is already loading.", 3000)
            return

        instance = getattr(self, "_loaded_database_instance", None)
        trusted_service = DatabaseActionsMixin._active_trusted_service_for_instance(
            self, instance,
        )
        if (
                getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE
                and trusted_service is None
                ):
            self.show_error(
                "Database Information Failed",
                "Could not read database information.",
                "The trusted reader session is unavailable. Reload the database.",
            )
            return

        self._database_info_generation = getattr(self, "_database_info_generation", 0) + 1
        worker = DatabaseInfoWorker(
            self._database_info_generation,
            database_path,
            expected_database_instance=instance,
            deadline=monotonic() + 10.0,
            trusted_service=trusted_service,
        )
        self._database_info_worker = worker
        self._database_info_instance = worker.database_instance
        self._database_info_active = True
        worker.signals.finished.connect(self.database_info_finished)
        self.show_status("Reading database information...", 0)
        self.databaseDetailThreadPool.start(worker)


    def _cancel_database_info(self):
        worker = getattr(self, "_database_info_worker", None)
        if worker is not None:
            worker.cancel()
        self._database_info_generation = getattr(self, "_database_info_generation", 0) + 1
        self._database_info_worker = None
        self._database_info_instance = None
        self._database_info_active = False


    @QtCore.pyqtSlot(int, str, object, object)
    def database_info_finished(self, generation, database_path, rows, error):
        if generation != getattr(self, "_database_info_generation", 0):
            return
        instance = getattr(self, "_database_info_instance", None)
        self._database_info_worker = None
        self._database_info_instance = None
        self._database_info_active = False
        if (
                not _database_paths_equal(database_path, self.fileTextbox.text())
                or getattr(self, "_database_load_active", False)
                or getattr(self, "_shutdown_started", False)
                or getattr(self, "_shutdown_ready", False)
                or DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                    self, database_path, instance,
                )
                ):
            return
        if error is not None:
            log_exception("Database information failed", error, __name__)
            self.show_error(
                "Database Information Failed",
                "Could not read database information.",
                str(error),
            )
            return

        self.show_status("Database information loaded.", 3000)
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
        self._database_refresher.start(database_path, current_instance)


    @QtCore.pyqtSlot(int, str, object)
    def database_refresh_new_runs_ready(
        self, generation, database_path, new_runs, source_worker=None,
    ):
        self._database_refresher.publish_page(generation, database_path, new_runs, source_worker)

    def _publish_database_refresh_page(self, session, new_runs, publication_is_current):
        """Apply a basic-row transaction inside the controller's publication gate."""
        publication_source_changed = False
        refresh_instance = session.instance
        database_path = session.path
        publication_baseline_instance = getattr(
            self,
            "_loaded_database_instance",
            None,
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
                        publication_baseline_instance,
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

        if not publication_is_current():
            raise RefreshPublicationAborted("The trusted refresh ended before publication.")
        if not _database_paths_equal(database_path, self.fileTextbox.text()):
            raise RefreshPublicationAborted(
                "The selected database changed before refresh publication."
            )
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                database_path,
                refresh_instance,
                ):
            raise RefreshPublicationAborted(
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
            raise RefreshPublicationAborted(
                "Trusted refresh basic-row publication was invalidated."
            )
        sync_empty_state = getattr(self, "_sync_empty_state", None)
        if callable(sync_empty_state):
            sync_empty_state()

    def _reconcile_refresh_selection(self, accepted_selected_guid):
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


    @QtCore.pyqtSlot(int, str, object, object, object)
    def database_refresh_finished(
        self, generation, database_path, new_runs, statuses, error, source_worker=None,
    ):
        self._database_refresher.finish(
            generation, database_path, new_runs, statuses, error, source_worker,
        )

    def _complete_database_refresh(self, session, new_runs, statuses, error, is_current):
        database_path = session.path
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(
                self,
                database_path,
                ):
            DatabaseActionsMixin._cancel_database_refresh(self)
            return

        if not _database_paths_equal(database_path, self.fileTextbox.text()):
            return
        if isinstance(error, DatabaseInstanceChangedError):
            DatabaseActionsMixin._cancel_database_refresh(self)
            self._reload_replaced_database(database_path)
            return
        refresh_instance = session.instance
        current_instance = database_instance(database_path)
        loaded_instance = getattr(self, "_loaded_database_instance", None)
        if (
                _database_instances_or_sidecars_differ(
                    refresh_instance,
                    current_instance,
                )
                or _database_instances_or_sidecars_differ(
                    loaded_instance,
                    current_instance,
                )
                ):
            self._reload_replaced_database(database_path)
            return
        DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
            self,
            current_instance,
        )
        if not is_current():
            return
        if error is not None:
            log_exception("Main-window refresh failed", error, __name__)
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

        if not is_current():
            return
        self._apply_database_refresh_result(new_runs, statuses)
        if not is_current():
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self,
                database_path,
                refresh_instance,
                discard_stale_publication=True,
                ):
            return


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

        update_auto_plot(self, updated_runs)
        self._sync_empty_state()
        checked_at = datetime.now().astimezone().strftime("%H:%M:%S")
        status_prefix = f"Last checked {checked_at} · "
        if not new_runs:
            if self.RunList.topLevelItemCount() == 0:
                self.show_status(status_prefix + self._empty_database_refresh_status(), 3000)
            else:
                self.show_status(status_prefix + "No new runs", 3000)
            return

        count = len(new_runs)
        noun = "run" if count == 1 else "runs"
        self.show_status(status_prefix + f"Found {count} new {noun}", 5000)

        if self.autoPlotBox.isChecked():
            enqueue_auto_plot(self, [run["guid"] for run in new_runs.values()])


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
        """Route trusted additions through the current derived-work bridge."""
        bridge = getattr(self, "_trusted_derived_bridge", None)
        if bridge is not None:
            bridge.reconcile_runs(self.RunList.all_run_metadata())


    def _cancel_database_refresh(self):
        self._database_refresher.invalidate()


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

        loaded_instance = getattr(self, "_loaded_database_instance", None)
        current_instance = database_instance(database_path)
        changed = _database_instances_or_sidecars_differ(
            loaded_instance, current_instance,
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
                    loaded_instance,
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
                and loaded_instance is not None
                and loaded_instance.identity is not None
                and current_identity == loaded_instance.identity
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

        from ._export_tasks import cancel_owner_exports

        cancel_owner_exports(self)
        DatabaseActionsMixin._cancel_database_refresh(self)
        DatabaseActionsMixin._cancel_database_info(self)
        if replacement:
            self._prepare_replaced_database_reload(abspath)

        if replacement:
            load_message = (
                f"Database was replaced; reloading {os.path.basename(abspath)}..."
            )
        else:
            load_message = f"Loading database {os.path.basename(abspath)}..."

        session = DatabaseLoadSession(
            abspath=abspath,
            load_started_at=load_started_at,
            reload_same_path=force or same_loaded_database,
            load_instance=current_instance,
            replacement_reload=replacement,
            generation_recovery=generation_recovery,
        )
        return self._database_loader.start(session, load_message)


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
        DatabaseActionsMixin._cancel_database_info(self)
        active_service = getattr(self, "_trusted_read_service", None)
        self._trusted_read_service = None
        DatabaseActionsMixin._retire_trusted_read_service(
            self,
            active_service,
            force=True,
        )
        self._database_access_mode = None
        self._database_fallback_reason = None

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


    def _capture_database_load_publication(self) -> DatabaseViewSnapshot:
        """Capture the committed view while a pending database is staged."""
        return DatabaseViewSnapshot(
            database_path=self.fileTextbox.text(),
            runs=dict(self.RunList.all_run_metadata()),
            run_id_text=self.run_idBox.text(),
            measurement_text=self.measurementBox.text(),
            selected_run_id=getattr(self, "selected_run_id", None),
            selected_run_guid=getattr(self, "_selected_run_guid", None),
            selected_dataset=getattr(self, "ds", None),
            selected_dataset_key=getattr(self, "_selected_dataset_key", None),
            local_last_file=getattr(self, "localLastFile", ""),
            preview_path=self.infoBox.preview.database_path,
            trusted_service=getattr(self, "_trusted_read_service", None),
            access_mode=getattr(self, "_database_access_mode", None),
            fallback_reason=getattr(self, "_database_fallback_reason", None),
            database_identity=getattr(self, "_loaded_database_identity", None),
            database_instance=getattr(self, "_loaded_database_instance", None),
            qcodes_database=get_DB_location(),
        )


    def _commit_database_load_view(
            self, snapshot: DatabaseViewSnapshot, abspath, runs, new_service, access_mode,
            fallback_reason, staged_instance,
            ):
        """Publish the staged view without yielding to Qt events."""
        set_qcodes_database_location(abspath)
        self._trusted_read_service = new_service
        self._database_access_mode = access_mode
        self._database_fallback_reason = fallback_reason
        self._loaded_database_identity = staged_instance.identity
        # A WAL/SHM that appeared while B was staged is compatible,
        # but it is now an accepted identity that later guards must
        # retain in order to detect replacement or ABA.
        self._loaded_database_instance = staged_instance
        prior_path = snapshot.database_path
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


    def _restore_database_load_publication(
            self,
            snapshot: DatabaseViewSnapshot,
            *,
            continue_loading=None,
            ):
        """Restore A after B fails before its publication transaction commits."""
        self._trusted_read_service = snapshot.trusted_service
        self._database_access_mode = snapshot.access_mode
        self._database_fallback_reason = snapshot.fallback_reason
        self._loaded_database_identity = snapshot.database_identity
        self._loaded_database_instance = snapshot.database_instance
        self.selected_run_id = snapshot.selected_run_id
        self._selected_run_guid = snapshot.selected_run_guid
        self.ds = snapshot.selected_dataset
        self._selected_dataset_key = snapshot.selected_dataset_key
        self.localLastFile = snapshot.local_last_file
        self.run_idBox.setText(snapshot.run_id_text)
        self.measurementBox.setText(snapshot.measurement_text)
        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        restored = self.RunList.addRuns(
            snapshot.runs,
            continue_loading=continue_loading,
        )
        if restored is False:
            return False
        self.fileTextbox.setText(snapshot.database_path)
        self.infoBox.preview.set_database_runs(
            snapshot.preview_path,
            snapshot.runs,
        )
        set_qcodes_database_location(snapshot.qcodes_database)
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
        """Cancel pending work while preserving the committed database view."""
        self._database_loader.cancel()


    @QtCore.pyqtSlot(int, str)
    def database_load_status(self, generation, message):
        """Deliver worker progress to the controller on the GUI thread."""
        self._database_loader.progress(generation, message)


    @QtCore.pyqtSlot(int, str, object, object)
    def database_load_finished(self, generation, abspath, runs, error, load_worker=None):
        """Deliver worker completion to the controller on the GUI thread."""
        self._database_loader.finish(generation, abspath, runs, error, load_worker)


    def _recover_database_load_publication(self, publication: DatabaseLoadPublication) -> bool:
        """Report or retry a failed publication after its gates have been released."""
        state = publication.session
        abspath = state.abspath
        load_started_at = state.load_started_at
        pending_service = publication.pending_service
        owns_pending_service = state.owns_trusted_service
        publication_snapshot = publication.snapshot
        old_service = publication_snapshot.trusted_service
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)

        if publication.aborted:
            if publication.rollback_error is not None:
                log_exception(
                    "Database view rollback failed",
                    publication.rollback_error,
                    __name__,
                )
                self.show_error(
                    "Database Recovery Failed",
                    "qPlot closed the database view because its previous "
                    "state could not be restored safely.",
                    str(publication.rollback_error),
                )
                if publication.rollback_source_changed:
                    rollback_path = publication_snapshot.database_path
                    self._database_loader.queue_retry(
                        DatabaseLoadSession(
                            abspath=rollback_path,
                            load_instance=(publication_snapshot.database_instance
                                           or database_instance(rollback_path)),
                            load_started_at=load_started_at,
                        ),
                        message="Previously displayed database changed; retrying...",
                    )
            return False

        if publication.restored:
            if derived_bridge is not None:
                derived_bridge.resume_publications()
            rollback_path = publication_snapshot.database_path
            rollback_runs = publication_snapshot.runs
            if rollback_path:
                self._start_database_detail_load(rollback_path, rollback_runs)
                if (
                        publication_snapshot.access_mode == TRUSTED_LIVE_MODE
                        and publication_snapshot.selected_run_guid
                        and callable(
                            getattr(self.RunList, "_item_for_guid", None)
                        )
                        ):
                    self._update_trusted_selected_run(
                        publication_snapshot.selected_run_guid
                    )

        if publication.error is not None:
            if owns_pending_service and pending_service is not old_service:
                DatabaseActionsMixin._retire_trusted_read_service(
                    self,
                    pending_service,
                )
            if publication.source_changed:
                self._database_loader.queue_retry(state)
                return False
            log_exception(
                "Database view publication failed",
                publication.error,
                __name__,
            )
            self.show_error(
                "Database Load Failed",
                f"Could not publish database {abspath}.",
                str(publication.error),
            )
            DatabaseActionsMixin._resume_test_database_generation_recovery(self)
            apply_refresh_interval = getattr(self, "_apply_refresh_interval", None)
            if callable(apply_refresh_interval):
                apply_refresh_interval(self._current_refresh_interval())
            return False

        return True


    def _complete_database_load_publication(self, publication: DatabaseLoadPublication):
        """Revalidate the committed source, then resume selection and progressive work."""
        state = publication.session
        abspath = state.abspath
        load_started_at = state.load_started_at
        generation = publication.generation
        runs = publication.runs
        access_mode = publication.access_mode
        accepted_instance = publication.accepted_instance
        publication_snapshot = publication.snapshot
        derived_bridge = publication.derived_bridge
        final_instance = database_instance(abspath)
        if _database_instances_or_sidecars_differ(publication.staged_instance, final_instance):
            self._reload_replaced_database(
                abspath,
                generation_recovery=bool(state.generation_recovery),
            )
            return
        DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
            self,
            final_instance,
        )

        if state.reload_same_path and not state.replacement_reload:
            invalidate_runtime_state = getattr(
                self,
                "_invalidate_database_runtime_state",
                None,
            )
            if callable(invalidate_runtime_state):
                invalidate_runtime_state(
                    publication_snapshot.database_instance or abspath
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
            and not database_instances_differ(
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
        if state.replacement_reload:
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
        channels = (detail_channel(self), detail_channel(self, True))
        for channel in channels:
            channel.cancel_worker()
        for channel in channels:
            channel.invalidate()
        set_run_metadata_loading(self, loading=False)


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
            self, abspath, run_ids, detail_instance, worker_kwargs):
        DatabaseActionsMixin._start_database_detail_worker(
            self, abspath, run_ids, detail_instance, worker_kwargs, expensive=False,
        )


    def _start_database_expensive_detail_worker(
            self, abspath, run_ids, detail_instance, worker_kwargs):
        DatabaseActionsMixin._start_database_detail_worker(
            self, abspath, run_ids, detail_instance, worker_kwargs, expensive=True,
        )


    def _start_database_detail_worker(
            self, abspath, run_ids, detail_instance, worker_kwargs, *, expensive):
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            return
        channel = detail_channel(self, expensive)
        channel.cancel_worker()
        channel.generation += 1
        channel.active = True
        channel.instance = detail_instance
        worker_type = DatabaseExpensiveDetailWorker if expensive else DatabaseDetailWorker
        priority = self._database_detail_priority_run_ids() if expensive else None
        worker = worker_type(
            channel.generation, abspath, run_ids, batch_size=100, **worker_kwargs,
        )
        channel.worker = worker
        if priority is None:
            priority = self._database_detail_priority_run_ids()
        worker.prioritize_run_ids(priority)
        if expensive:
            worker.signals.status.connect(self.database_expensive_detail_status)
            worker.signals.batch_ready.connect(self.database_expensive_detail_batch_ready)
            worker.signals.finished.connect(self.database_expensive_detail_finished)
        else:
            worker.signals.status.connect(self.database_detail_status)
            worker.signals.batch_ready.connect(self.database_detail_batch_ready)
            worker.signals.finished.connect(self.database_detail_finished)
        pool = getattr(self, "databaseDetailThreadPool", self.databaseLoadThreadPool)
        if expensive:
            pool = getattr(self, "databaseExpensiveDetailThreadPool", pool)
        columns = EXPENSIVE_METADATA_COLUMNS if expensive else BASIC_METADATA_COLUMNS
        set_run_metadata_loading(self, columns=columns, loading=False)
        set_run_metadata_loading(self, run_ids=run_ids, columns=columns)
        pool.start(worker)


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
                detail_channel(self).active
                or detail_channel(self, True).active
                ):
            return

        priority_run_ids = self._database_detail_priority_run_ids(run_ids=run_ids)
        for channel in (detail_channel(self), detail_channel(self, True)):
            if channel.active and channel.worker is not None:
                channel.worker.prioritize_run_ids(priority_run_ids)


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
        DatabaseActionsMixin._detail_status(self, generation, message, expensive=False)


    @QtCore.pyqtSlot(int, str)
    def database_expensive_detail_status(self, generation, message):
        DatabaseActionsMixin._detail_status(self, generation, message, expensive=True)


    def _detail_status(self, generation, message, *, expensive):
        if detail_channel(self, expensive).accepts(generation):
            self.show_status(message, 0)


    @QtCore.pyqtSlot(int, str, object)
    def database_detail_batch_ready(self, generation, abspath, runs):
        DatabaseActionsMixin._detail_batch_ready(
            self, generation, abspath, runs, expensive=False,
        )


    @QtCore.pyqtSlot(int, str, object)
    def database_expensive_detail_batch_ready(self, generation, abspath, runs):
        DatabaseActionsMixin._detail_batch_ready(
            self, generation, abspath, runs, expensive=True,
        )


    def _detail_batch_ready(self, generation, abspath, runs, *, expensive):
        channel = detail_channel(self, expensive)
        if not channel.accepts(generation):
            return
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(self, abspath):
            return
        if not _database_paths_equal(abspath, self.fileTextbox.text()):
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self, abspath, channel.instance):
            return
        detail_instance = channel.instance
        self._apply_database_detail_batch(runs)
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
            self, abspath, detail_instance, discard_stale_publication=True,
        ) or not channel.accepts(generation):
            return
        if expensive:
            # Shape and size arrive in separate batches from this worker.
            for name, fields in (
                ("Setpoints", {"setpoint_shape", "point_shape", "setpoint_count", "expected_results"}),
                ("Size", {"storage_bytes"}),
            ):
                guids = [run.get("guid") for run in runs.values() if fields.intersection(run)]
                set_run_metadata_loading(self, guids, columns=(name,), loading=False)
        else:
            set_run_metadata_loading(
                self, [run.get("guid") for run in runs.values()],
                columns=BASIC_METADATA_COLUMNS, loading=False,
            )


    def _apply_database_detail_batch(self, runs):
        if DatabaseActionsMixin._database_generation_transaction_blocks_path(self):
            return
        updated_runs = self.RunList.updateRuns(runs)
        update_auto_plot(self, updated_runs)
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
        DatabaseActionsMixin._detail_finished(
            self, generation, abspath, error, expensive=False,
        )


    @QtCore.pyqtSlot(int, str, object)
    def database_expensive_detail_finished(self, generation, abspath, error):
        DatabaseActionsMixin._detail_finished(
            self, generation, abspath, error, expensive=True,
        )


    def _detail_finished(self, generation, abspath, error, *, expensive):
        channel = detail_channel(self, expensive)
        if generation != channel.generation:
            return
        detail_instance = channel.instance
        channel.clear()
        if not _database_paths_equal(abspath, self.fileTextbox.text()):
            return
        if isinstance(error, DatabaseInstanceChangedError):
            DatabaseActionsMixin._cancel_database_detail_load(self)
            self._reload_replaced_database(abspath)
            return
        if DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                self, abspath, detail_instance):
            return
        columns = EXPENSIVE_METADATA_COLUMNS if expensive else BASIC_METADATA_COLUMNS
        set_run_metadata_loading(self, columns=columns, loading=False)
        if error is not None:
            diagnostic = "Expensive database detail load failed" if expensive else "Database detail load failed"
            message = "Setpoint and size loading failed" if expensive else "Run detail loading failed"
            log_exception(diagnostic, error, __name__)
            self.show_status(f"{message}: {error}", 5000)
            return
        if not detail_channel(self, not expensive).active:
            self.show_status("Run details loaded.", 5000)


    def _refresh_selected_run_details(self, runs, *, live_only=False):
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            if not live_only:
                return

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
        Reapplying the current selection’s immediate loading model
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
                access_mode = getattr(self, "_database_access_mode", None)
                if access_mode == SNAPSHOT_FALLBACK_MODE:
                    setter_name = "set_snapshot_run_unavailable"
                    setter_value = metadata
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


    def _prepare_selected_run_state(self, guid, run_id, item) -> _SelectedRunState:
        """Apply the common selection mutation after source and row validation.

        Keep the captured generation through dataset-release and widget
        callbacks. Each caller still revalidates it at its publication fences.
        """
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
        sync_controls = getattr(self, "_sync_database_generation_controls", None)
        if callable(sync_controls):
            sync_controls()
        return _SelectedRunState(generation, metadata)


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

        selected = DatabaseActionsMixin._prepare_selected_run_state(self, guid, run_id, item)
        generation, metadata = selected.generation, selected.metadata
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

        selected = DatabaseActionsMixin._prepare_selected_run_state(self, guid, run_id, item)
        generation, metadata = selected.generation, selected.metadata
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

        set_error = getattr(self.infoBox, "set_trusted_run_error", None)
        if callable(set_error):
            set_error("The trusted detail coordinator is unavailable. Reload the database to continue.", metadata)
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
