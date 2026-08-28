import os
import sys
import threading
from time import monotonic, perf_counter
from typing import cast

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)

from qplot import config
from qplot.datahandling.database import (
    DatabaseDetailWorker as DatabaseDetailWorker,
)
from qplot.datahandling.database import (
    DatabaseExpensiveDetailWorker as DatabaseExpensiveDetailWorker,
)
from qplot.datahandling.database import (
    DatabaseLoadWorker as DatabaseLoadWorker,
)
from qplot.datahandling.database import (
    DatabaseRefreshWorker as DatabaseRefreshWorker,
)
from qplot.datahandling.database import (
    DatabaseSelectedRunWorker as DatabaseSelectedRunWorker,
)
from qplot.datahandling.database import (
    database_info_report as database_info_report,
)
from qplot.datahandling.database import (
    database_path_from_mime_data as database_path_from_mime_data,
)
from qplot.diagnostics import get_logger, log_bounded_shutdown, log_user_error

from ._commands import create_action
from ._config_persistence import (
    persist_config_action,
    persist_config_value,
    set_widget_value_without_signals,
)
from ._database_actions import DatabaseActionsMixin, TestDatabaseGenerationWorker
from ._dataset_handle import DatasetHandle, DatasetKey
from ._help import add_help_menu
from ._plot_actions import PlotActionsMixin
from ._preferences import (
    PreferencesDialog,
    create_preferences_action,
)
from ._run_controls import RunControlsMixin
from ._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    add_application_quit_action,
    add_restore_defaults_option,
    add_standard_window_controls,
    ask_confirmation_with_dont_ask_again,
    close_all_warning_enabled,
)

MAIN_WINDOW_READABLE_WIDTH = 780
_APPLICATION_SHUTDOWN_TIMEOUT_SECONDS = 15.0
_APPLICATION_SHUTDOWN_DIAGNOSTIC_GRACE_SECONDS = 0.25
_APPLICATION_FORCED_SHUTDOWN_EXIT_CODE = 70
_RETIRED_SERVICE_REAPER_INTERVAL_MS = 100


def _flush_shutdown_diagnostics():
    """Flush qPlot's persistent diagnostics and process text streams."""

    for handler in tuple(get_logger().handlers):
        try:
            handler.flush()
            stream = getattr(handler, "stream", None)
            if stream is not None:
                os.fsync(stream.fileno())
        except BaseException:
            pass
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.flush()
        except BaseException:
            pass


def _persist_shutdown_diagnostics(*, started_at, total_timeout, diagnostics):
    """Persist an exact bounded-shutdown snapshot before a hard process exit."""

    elapsed = max(0.0, monotonic() - started_at)
    if not diagnostics:
        diagnostics = ("background work remained active without diagnostics",)
    details = f"elapsed={elapsed:.3f}s\n" + "\n".join(diagnostics)
    try:
        log_bounded_shutdown(
            "Bounded Application Shutdown: "
            f"the {total_timeout:g}-second monotonic shutdown deadline was "
            "exhausted; qPlot will terminate at the process boundary if "
            "Qt-owned work still cannot be destroyed.",
            details,
            logger_name=__name__,
        )
    finally:
        _flush_shutdown_diagnostics()


class _ProcessShutdownFailSafe:
    """Pair a direct-parent supervisor with a deadline-only local backstop."""

    def __init__(
        self,
        supervisor_client=None,
        *,
        startup_diagnostic=None,
        force_exit=os._exit,
    ):
        self._supervisor_client = supervisor_client
        self._startup_diagnostic = (
            None if startup_diagnostic is None else str(startup_diagnostic)
        )
        self._force_exit = force_exit
        self._lock = threading.Lock()
        self._persistence_io_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._fallback_diagnostic_started = threading.Event()
        self._fallback_diagnostic_completed = threading.Event()
        self._armed = False
        self._started_at = 0.0
        self._diagnostic_deadline = 0.0
        self._hard_deadline = 0.0
        self._total_timeout = 0.0
        self._diagnostics: tuple[str, ...] = ()
        self._supervisor_diagnostics: tuple[str, ...] = ()
        self._diagnostic_version = 0
        self._persisted_version = -1
        self._supervisor_acknowledged = False
        self._fallback_active = False
        self._persistence_active = False

    @property
    def armed(self):
        with self._lock:
            return self._armed

    def arm(self, *, started_at, diagnostic_deadline, hard_deadline):
        """Arm once after shutdown confirmation; later calls never extend it."""

        with self._lock:
            if self._armed:
                return None
            self._armed = True
            self._started_at = started_at
            self._diagnostic_deadline = diagnostic_deadline
            self._hard_deadline = hard_deadline
            self._total_timeout = max(0.0, hard_deadline - started_at)
            self._diagnostics = ()
            self._supervisor_diagnostics = ()
            self._diagnostic_version = 0
            self._persisted_version = -1
            self._supervisor_acknowledged = False
            self._fallback_active = False
            self._persistence_active = False
            self._cancelled.clear()
            self._fallback_diagnostic_started.clear()
            self._fallback_diagnostic_completed.clear()

        if self._startup_diagnostic is not None:
            self._append_supervisor_diagnostic(self._startup_diagnostic)
        client = self._supervisor_client
        if client is None:
            self._activate_fallback()
            diagnostic = self._startup_diagnostic
            if diagnostic is None:
                diagnostic = (
                    "process shutdown supervisor is unavailable; "
                    "only the in-process deadline fallback is armed"
                )
                self._append_supervisor_diagnostic(diagnostic)
            return diagnostic

        try:
            arm_diagnostic = client.arm(hard_deadline)
        except BaseException as error:
            arm_diagnostic = (
                "process shutdown supervisor ARM raised "
                f"{type(error).__name__}: {error}"
            )
        # The direct parent has either installed its immutable deadline or
        # returned an exact failure by this point.  Install the independent
        # GUI-local backstop before doing any diagnostic persistence.
        self._activate_fallback()
        if arm_diagnostic:
            exact = str(arm_diagnostic)
            self._append_supervisor_diagnostic(exact)
            return exact
        with self._lock:
            self._supervisor_acknowledged = True
        return self._latest_supervisor_diagnostic()

    def update_diagnostics(self, diagnostics):
        """Publish an in-memory liveness snapshot without doing diagnostic I/O."""

        exact = tuple(str(item) for item in diagnostics)
        with self._lock:
            if exact == self._diagnostics:
                return
            self._diagnostics = exact
            self._diagnostic_version += 1

    def persist_now(self):
        """Persist the newest exact snapshot, once per diagnostic version."""

        with self._persistence_io_lock:
            with self._lock:
                if not self._armed:
                    return
                version = self._diagnostic_version
                if version <= self._persisted_version:
                    return
                started_at = self._started_at
                total_timeout = self._total_timeout
                diagnostics = self._effective_diagnostics_locked()
            _persist_shutdown_diagnostics(
                started_at=started_at,
                total_timeout=total_timeout,
                diagnostics=diagnostics,
            )
            with self._lock:
                self._persisted_version = max(self._persisted_version, version)

    def persist_async(self):
        """Persist on a daemon thread so diagnostic I/O cannot delay exit."""

        with self._lock:
            if not self._armed or self._persistence_active:
                return
            self._persistence_active = True
        try:
            threading.Thread(
                target=self._persist_async_worker,
                name="qplot-shutdown-diagnostic-persistence",
                daemon=True,
            ).start()
        except BaseException as error:
            with self._lock:
                self._persistence_active = False
            self._append_supervisor_diagnostic(
                "process shutdown diagnostic thread setup raised "
                f"{type(error).__name__}: {error}"
            )

    def _persist_async_worker(self):
        try:
            while True:
                self.persist_now()
                with self._lock:
                    if (
                        not self._armed
                        or self._persisted_version >= self._diagnostic_version
                    ):
                        self._persistence_active = False
                        return
        finally:
            with self._lock:
                self._persistence_active = False

    def disarm(self):
        """Cancel only the GUI-local fallback after quiescent Qt teardown."""

        # There is deliberately no supervisor DISARM message.  Once ARM was
        # accepted, the direct parent remains armed until it reaps this process.
        with self._lock:
            self._armed = False
        self._cancelled.set()

    def wait_for_forced_exit(self):
        """Reach the process boundary before any possibly blocking destructor."""

        with self._lock:
            hard_deadline = self._hard_deadline
            armed = self._armed
        if not armed:
            return
        if self._cancelled.wait(max(0.0, hard_deadline - monotonic())):
            return
        self._force_exit(_APPLICATION_FORCED_SHUTDOWN_EXIT_CODE)

    def watchdog_operational(self):
        """Return whether the direct parent acknowledged immutable ARM."""

        with self._lock:
            return self._armed and self._supervisor_acknowledged

    def _fallback_diagnostic_watchdog(self):
        self._fallback_diagnostic_started.set()
        if self._cancelled.wait(max(0.0, self._diagnostic_deadline - monotonic())):
            return
        # This watchdog already runs away from the GUI thread.  Persist here
        # instead of scheduling a second daemon so the hard-deadline watchdog
        # cannot overtake diagnostic publication merely because that extra
        # thread has not yet been scheduled.
        try:
            self.persist_now()
        finally:
            self._fallback_diagnostic_completed.set()

    def _fallback_termination_watchdog(self):
        # Prefer a completed diagnostic snapshot before termination without
        # ever allowing slow or blocked diagnostic I/O to extend the immutable
        # hard deadline. This also prevents the two fallback threads racing at
        # the deadline after ordinary, fast persistence has already started.
        self._fallback_diagnostic_completed.wait(
            max(0.0, self._hard_deadline - monotonic())
        )
        if self._cancelled.wait(max(0.0, self._hard_deadline - monotonic())):
            return
        self._force_exit(_APPLICATION_FORCED_SHUTDOWN_EXIT_CODE)

    def _activate_fallback(self):
        with self._lock:
            if self._fallback_active or not self._armed:
                return
            self._fallback_active = True
        for name, target in (
            (
                "qplot-shutdown-diagnostic-fallback",
                self._fallback_diagnostic_watchdog,
            ),
            (
                "qplot-shutdown-process-fallback",
                self._fallback_termination_watchdog,
            ),
        ):
            try:
                threading.Thread(target=target, name=name, daemon=True).start()
                if target == self._fallback_diagnostic_watchdog:
                    readiness_deadline = min(
                        self._diagnostic_deadline,
                        self._hard_deadline,
                    )
                    self._fallback_diagnostic_started.wait(
                        max(0.0, readiness_deadline - monotonic())
                    )
            except BaseException as error:
                self._append_supervisor_diagnostic(
                    "process shutdown local fallback setup raised "
                    f"{type(error).__name__}: {error}"
                )

    def _append_supervisor_diagnostic(self, diagnostic):
        exact = str(diagnostic)
        with self._lock:
            if exact in self._supervisor_diagnostics:
                return
            self._supervisor_diagnostics = (*self._supervisor_diagnostics, exact)
            self._diagnostic_version += 1

    def _latest_supervisor_diagnostic(self):
        with self._lock:
            if not self._supervisor_diagnostics:
                return None
            return self._supervisor_diagnostics[-1]

    def _effective_diagnostics_locked(self):
        return tuple(dict.fromkeys((*self._supervisor_diagnostics, *self._diagnostics)))


class DatabasePathLineEdit(qtw.QLineEdit):
    """
    Read-only database path field that accepts dropped QCoDeS database files.

    """
    databaseDropped = QtCore.pyqtSignal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._database_path = ""
        self.setAcceptDrops(True)

    def setText(self, text):
        self._database_path = str(text or "")

        if not self._database_path:
            super().setText("")
            self.setToolTip("Current database path. Drop a QCoDeS .db file here to load it.")
            return

        super().setText(os.path.basename(self._database_path) or self._database_path)
        self.setCursorPosition(0)
        self.setToolTip(
            "Current database:\n"
            f"{self._database_path}\n\n"
            "Drop a QCoDeS .db file here to load it."
            )

    def text(self):
        return self._database_path

    def dragEnterEvent(self, event):
        if database_path_from_mime_data(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        path = database_path_from_mime_data(event.mimeData())
        if path is None:
            event.ignore()
            return

        event.acceptProposedAction()
        self.databaseDropped.emit(os.path.abspath(path))


class MainWindow(
    DatabaseActionsMixin,
    PlotActionsMixin,
    RunControlsMixin,
    qtw.QMainWindow,
    ):
    """
    The Main application which connects/initialises QCoDeS database, displays
    available options plots to open, and opens windows.
    
    This window can be opened by calling qplot.run()
    
    Holds a shallow copy of all other open windows to prevent deletion by 
    python's garbarge collector
    """
    
    def __init__(self, startup_database_path=None):
        startup_start = perf_counter()
        super().__init__()
       
        #vars
        self.config = config() # Connect to config.json in :/users/<user>/.qplot/
        self.windows = [] # prevent auto delete of windows
        self.ds = None
        self._selected_dataset_key: DatasetKey | None = None
        self.preview_size = self._configured_preview_size()
        self.dataset_holder: dict[DatasetKey, DatasetHandle] = {}
        self.monitor = QtCore.QTimer()
        self.threadPool = QtCore.QThreadPool()
        self.threadPool.setMaxThreadCount(self.config.get("runtime_settings.max_threads"))
        self._plot_workers: set[object] = set()
        self.threadPool._qplot_workers = self._plot_workers  # type: ignore[attr-defined]
        self.databaseLoadThreadPool = QtCore.QThreadPool(self)
        self.databaseLoadThreadPool.setMaxThreadCount(1)
        self.databaseDetailThreadPool = QtCore.QThreadPool(self)
        self.databaseDetailThreadPool.setMaxThreadCount(1)
        self.databaseExpensiveDetailThreadPool = QtCore.QThreadPool(self)
        self.databaseExpensiveDetailThreadPool.setMaxThreadCount(1)
        self.databaseRefreshThreadPool = QtCore.QThreadPool(self)
        self.databaseRefreshThreadPool.setMaxThreadCount(1)
        self.databaseSelectedRunThreadPool = QtCore.QThreadPool(self)
        self.databaseSelectedRunThreadPool.setMaxThreadCount(1)
        self.testDatabaseGenerationThreadPool = QtCore.QThreadPool(self)
        self.testDatabaseGenerationThreadPool.setMaxThreadCount(1)
        self._database_load_generation = 0
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._loaded_database_identity = None
        self._loaded_database_instance = None  # type: ignore[assignment]
        self._database_detail_generation = 0
        self._database_detail_active = False
        self._database_detail_worker = None
        self._database_detail_instance = None
        self._database_expensive_detail_generation = 0
        self._database_expensive_detail_active = False
        self._database_expensive_detail_worker = None
        self._database_expensive_detail_instance = None
        self._database_refresh_generation = 0
        self._database_refresh_active = False
        self._database_refresh_pending = False
        self._database_refresh_worker: DatabaseRefreshWorker | None = None
        self._database_refresh_identity = None
        self._database_refresh_instance = None
        self._database_refresh_staged_new_runs = {}
        self._database_refresh_publication_active = False
        self._database_selected_run_generation = 0
        self._database_selected_run_worker: DatabaseSelectedRunWorker | None = None
        self._database_selected_run_instance = None
        self._database_selected_run_mode = None
        self._restoring_selected_run_publication = False
        self._selected_run_restore_pending = False
        self._selected_run_guid = None
        self._selected_run_detail_cache = {}
        self._selected_run_partial_detail_keys = set()
        self._snapshot_setpoint_summary_cache = {}
        self._trusted_read_service = None
        self._pending_trusted_read_services = {}
        self._retired_trusted_read_services = set()
        self._retired_service_reap_diagnostics: dict[int, str] = {}
        self._database_access_mode = None
        self._database_fallback_reason = None
        self._test_database_generation_active = False
        self._test_database_generation_worker: TestDatabaseGenerationWorker | None = None
        self._test_database_replacement_state = None
        self._database_view_released_for_generation = False
        self._shutdown_started = False
        self._shutdown_ready = False
        self._shutdown_started_at = None
        self._shutdown_deadline = None
        self._shutdown_hard_deadline = None
        self._shutdown_cleanup_escalated = False
        self._shutdown_escalation_diagnostics: tuple[str, ...] = ()
        self._shutdown_liveness_diagnostics: tuple[str, ...] = ()
        self._shutdown_last_diagnostics: tuple[str, ...] = ()
        self._shutdown_diagnostics: tuple[str, ...] = ()
        self._shutdown_deadline_exhausted = False
        # The process entry point installs and owns the fail-safe.  Keeping an
        # ordinary embedded/test MainWindow inert avoids an unexpected hard
        # exit in a host process that owns its own Qt lifecycle.
        self._shutdown_process_fail_safe = None
        self._shutdown_timer = QtCore.QTimer(self)
        self._shutdown_timer.setInterval(25)
        self._shutdown_timer.timeout.connect(self._finish_deferred_shutdown)
        self._retired_service_reaper_timer = QtCore.QTimer(self)
        self._retired_service_reaper_timer.setInterval(
            _RETIRED_SERVICE_REAPER_INTERVAL_MS
        )
        self._retired_service_reaper_timer.timeout.connect(
            self._reap_retired_trusted_read_services
        )
        self._next_plot_x = 0
        self._next_plot_y = 0
        self.localLastFile = None
        self.startup_database_path = startup_database_path
        
        # Set GUI color and style from user choice in qplot.configuration.themes
        self.setStyleSheet(self.config.theme.main)
        
        #widgets
        self.l = qtw.QVBoxLayout()
        self.l.setContentsMargins(8, 8, 8, 4)
        self.l.setSpacing(6)
        
        #Core initialisation functions
        self.initRefresh()
        self.initMenu()
        self.initFile()
        self.initRunDisplay()
        self.initShortcuts()
        self.startupDatabaseTimer = QtCore.QTimer(self)
        self.startupDatabaseTimer.setSingleShot(True)
        self.startupDatabaseTimer.timeout.connect(self.load_startup_database)
        
        #Final Setup
        w = qtw.QFrame()
        w.setLayout(self.l)
        self.setCentralWidget(w)
       
        # Fetch window size from config.json, but keep the run list readable.
        configured_width, configured_height = self.config.get("GUI.main_frame_size")
        self.resize(
            max(configured_width, MAIN_WINDOW_READABLE_WIDTH),
            configured_height,
            )
        self.setWindowTitle("qPlot")
        startup_elapsed = perf_counter() - startup_start
        self.show_status(f"Ready - QPlot opened in {startup_elapsed:.2f} s")
        
        # Get user's window dimensions to control new window position
        primary_screen = cast(QtGui.QScreen, qtw.QApplication.primaryScreen())
        self.screenrect = primary_screen.availableGeometry()
        self._next_plot_x = self.screenrect.left()
        self._next_plot_y = self.screenrect.top()
        
        # Try to bring window to top 
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowType.WindowStaysOnTopHint) 
        self.show()
        startup_warning = getattr(self.config, "startup_warning", None)
        if startup_warning:
            self.show_status(startup_warning, 10_000)
        self.startupDatabaseTimer.start(0)


    def initMenu(self):
        """
        Produces the menu bar and all menu's contained at the top of the window

        """
        menu = cast(qtw.QMenuBar, self.menuBar())
        # First dropdown menu
        fileMenu = cast(qtw.QMenu, menu.addMenu("&File")) # Not sure why these all have &, but they do
        
        # Load database file
        loadAction = create_action("database.load", self)
        loadAction.triggered.connect(self.getfile)
        fileMenu.addAction(loadAction)
        
        self.recentDatabaseMenu = cast(qtw.QMenu, fileMenu.addMenu("Load &Recent Database"))
        self.refresh_recent_database_menu()

        open_folder_action = create_action(
            "database.open_folder",
            self,
            )
        open_folder_action.triggered.connect(self.open_database_location)
        fileMenu.addAction(open_folder_action)

        self.closeDatabaseAction = create_action("database.close", self)
        self.closeDatabaseAction.triggered.connect(self.close_current_database)
        fileMenu.addAction(self.closeDatabaseAction)
        
        # Force update check on database
        refreshAction = create_action("window.refresh", self)
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)

        fileMenu.addSeparator()

        test_data_menu = cast(qtw.QMenu, fileMenu.addMenu("Generate &Test Data"))
        create_csv_action = create_action("testdata.create_csv", self)
        create_csv_action.triggered.connect(self.create_test_database_csv)
        test_data_menu.addAction(create_csv_action)
        export_collection_action = create_action(
            "testdata.export_collection",
            self,
        )
        export_collection_action.triggered.connect(
            self.export_test_database_csv_collection
        )
        test_data_menu.addAction(export_collection_action)
        self.generateTestDatabaseAction = create_action(
            "testdata.generate_database",
            self,
        )
        self.generateTestDatabaseAction.triggered.connect(
            self.generate_test_database_from_csv
        )
        test_data_menu.addAction(self.generateTestDatabaseAction)

        fileMenu.addSeparator()

        self.closeAllPlotsAction = create_action(
            "plots.close_all",
            self,
            status_tip="Close all open plot windows",
            )
        self.closeAllPlotsAction.triggered.connect(self.closeAll)
        fileMenu.addAction(self.closeAllPlotsAction)

        closeAction = create_action(
            "window.close",
            self,
            status_tip="Close the main qPlot window",
            )
        closeAction.triggered.connect(self.close)
        fileMenu.addAction(closeAction)

        add_application_quit_action(self, fileMenu, self.quit_application)

        add_standard_window_controls(self)
        
        # Second dropdown menu
        prefMenu = cast(qtw.QMenu, menu.addMenu("&Options"))

        prefMenu.addAction(
            create_preferences_action(self, self.show_preferences_dialog)
            )
        prefMenu.addSeparator()
        add_restore_defaults_option(self, prefMenu)
        add_help_menu(self)

    def initFile(self):
        """
        Display text box for current selected database
        
        """
        self.targetLayout = qtw.QHBoxLayout()
        style = cast(qtw.QStyle, self.style())
        self.targetLayout.setContentsMargins(8, 2, 8, 2)
        self.targetLayout.setSpacing(6)

        database_label = qtw.QLabel("Database:")
        database_label.setToolTip("Current QCoDeS database")
        self.targetLayout.addWidget(database_label)

        self.fileTextbox = DatabasePathLineEdit()
        self.fileTextbox.setObjectName("databasePathField")
        self.fileTextbox.setReadOnly(True)
        self.fileTextbox.setPlaceholderText("Drop a QCoDeS .db file here or use File -> Load")
        self.fileTextbox.setToolTip(
            "Current database path. Drop a QCoDeS .db file here to load it."
            )
        self.fileTextbox.databaseDropped.connect(self.load_database_path)
        self.targetLayout.addWidget(self.fileTextbox, 1)

        self.copyDatabasePathButton = qtw.QToolButton()
        self.copyDatabasePathButton.setObjectName("databaseIconButton")
        self.copyDatabasePathButton.setIcon(
            style.standardIcon(qtw.QStyle.StandardPixmap.SP_FileDialogDetailedView)
            )
        self.copyDatabasePathButton.setToolTip("Copy the full database path")
        self.copyDatabasePathButton.setAccessibleName("Copy database path")
        self.copyDatabasePathButton.setFixedSize(28, 26)
        self.copyDatabasePathButton.clicked.connect(self.copy_database_path)
        self.targetLayout.addWidget(self.copyDatabasePathButton)

        self.databaseInfoButton = qtw.QToolButton()
        self.databaseInfoButton.setObjectName("databaseIconButton")
        self.databaseInfoButton.setIcon(
            style.standardIcon(qtw.QStyle.StandardPixmap.SP_MessageBoxInformation)
            )
        self.databaseInfoButton.setToolTip("Show database information")
        self.databaseInfoButton.setAccessibleName("Show database information")
        self.databaseInfoButton.setFixedSize(28, 26)
        self.databaseInfoButton.clicked.connect(self.show_database_info)
        self.targetLayout.addWidget(self.databaseInfoButton)

        self.loadDatabaseButton = qtw.QToolButton()
        self.loadDatabaseButton.setObjectName("databaseIconButton")
        self.loadDatabaseButton.setIcon(
            style.standardIcon(qtw.QStyle.StandardPixmap.SP_DialogOpenButton)
            )
        self.loadDatabaseButton.setToolTip("Load a QCoDeS .db database (Ctrl+L)")
        self.loadDatabaseButton.setAccessibleName("Load database")
        self.loadDatabaseButton.setFixedSize(28, 26)
        self.loadDatabaseButton.clicked.connect(self.getfile)
        self.targetLayout.addWidget(self.loadDatabaseButton)

        self.openDatabaseFolderButton = qtw.QToolButton()
        self.openDatabaseFolderButton.setObjectName("databaseIconButton")
        self.openDatabaseFolderButton.setIcon(
            style.standardIcon(qtw.QStyle.StandardPixmap.SP_DirOpenIcon)
            )
        self.openDatabaseFolderButton.setToolTip(
            "Open the folder containing the current database (Ctrl+Shift+D)"
        )
        self.openDatabaseFolderButton.setAccessibleName("Open database folder")
        self.openDatabaseFolderButton.setFixedSize(28, 26)
        self.openDatabaseFolderButton.clicked.connect(self.open_database_location)
        self.targetLayout.addWidget(self.openDatabaseFolderButton)

        self.targetLayout.addStretch()
        self.targetLayout.addSpacing(18)
        self.targetLayout.addWidget(self.closeAllPlotsButton)

        self.databaseLoadFrame = qtw.QFrame()
        self.databaseLoadFrame.setObjectName("databaseLoadFrame")
        database_load_layout = qtw.QHBoxLayout(self.databaseLoadFrame)
        database_load_layout.setContentsMargins(8, 0, 8, 2)
        database_load_layout.setSpacing(6)

        self.databaseLoadProgress = qtw.QProgressBar()
        self.databaseLoadProgress.setObjectName("databaseLoadProgress")
        self.databaseLoadProgress.setRange(0, 0)
        self.databaseLoadProgress.setTextVisible(False)
        self.databaseLoadProgress.setFixedWidth(120)
        self.databaseLoadProgress.setMaximumHeight(16)
        self.databaseLoadProgress.setAccessibleName("Database load progress")
        database_load_layout.addWidget(self.databaseLoadProgress)

        self.databaseLoadLabel = qtw.QLabel("")
        self.databaseLoadLabel.setObjectName("databaseLoadLabel")
        self.databaseLoadLabel.setSizePolicy(
            qtw.QSizePolicy.Policy.Expanding,
            qtw.QSizePolicy.Policy.Preferred,
            )
        database_load_layout.addWidget(self.databaseLoadLabel, 1)

        self.databaseLoadCancelButton = qtw.QToolButton()
        self.databaseLoadCancelButton.setObjectName("databaseIconButton")
        self.databaseLoadCancelButton.setIcon(
            style.standardIcon(qtw.QStyle.StandardPixmap.SP_DialogCancelButton)
            )
        self.databaseLoadCancelButton.setText("Cancel")
        self.databaseLoadCancelButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        self.databaseLoadCancelButton.setToolTip("Cancel the current database load")
        self.databaseLoadCancelButton.setAccessibleName("Cancel database load")
        self.databaseLoadCancelButton.setFixedSize(78, 24)
        self.databaseLoadCancelButton.clicked.connect(self.cancel_database_load)
        database_load_layout.addWidget(self.databaseLoadCancelButton)
        self.databaseLoadFrame.setVisible(False)
        
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot()
    def close_current_database(self):
        """
        Closes plot windows before releasing the current database.

        """
        if not self.fileTextbox.text():
            self.show_status("No database is loaded.", 3000)
            return

        if not self.close_plot_windows(confirm=True, status=False):
            self.show_status("Database close cancelled.", 3000)
            return

        self.close_database()


    @QtCore.pyqtSlot()
    def quit_application(self):
        """
        Routes the Quit command through the main window's shutdown handler.

        """
        self.close()


    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Event handler for closing Main Window.

        Also handles some closing admin        

        """
        if getattr(self, "_shutdown_ready", False):
            event.accept()
            return
        if getattr(self, "_shutdown_started", False):
            event.ignore()
            return

        # Confirm exit
        if self.config.get(CONFIRM_QUIT_KEY):
            reply = ask_confirmation_with_dont_ask_again(
                self,
                "Confirm Exit",
                "Are you sure you want to exit?",
                CONFIRM_QUIT_KEY,
                )
            if reply != qtw.QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        # One monotonic total bound covers cancellation, cleanup escalation,
        # diagnostic persistence, and hard process termination.  The final
        # grace is inside that bound, never appended to it.
        shutdown_started_at = monotonic()
        total_timeout = max(0.0, _APPLICATION_SHUTDOWN_TIMEOUT_SECONDS)
        diagnostic_grace = min(
            _APPLICATION_SHUTDOWN_DIAGNOSTIC_GRACE_SECONDS,
            total_timeout * 0.25,
        )
        hard_deadline = shutdown_started_at + total_timeout
        self._shutdown_started_at = shutdown_started_at
        self._shutdown_deadline = hard_deadline - diagnostic_grace
        self._shutdown_hard_deadline = hard_deadline
        self._shutdown_cleanup_escalated = False
        self._shutdown_escalation_diagnostics = ()
        self._shutdown_liveness_diagnostics = ()
        self._shutdown_last_diagnostics = ()
        self._shutdown_diagnostics = ()
        self._shutdown_deadline_exhausted = False
        process_fail_safe = getattr(self, "_shutdown_process_fail_safe", None)
        if process_fail_safe is not None:
            launch_diagnostic = process_fail_safe.arm(
                started_at=shutdown_started_at,
                diagnostic_deadline=self._shutdown_deadline,
                hard_deadline=hard_deadline,
            )
            if launch_diagnostic:
                self._shutdown_escalation_diagnostics = (launch_diagnostic,)
                MainWindow._publish_shutdown_diagnostics(self)

        # Mark the refresh lifecycle as shut down before cancelling workers.
        # This also invalidates a QTimer timeout that was already queued.
        self._automatic_refresh_shutdown = True
        stop_refresh_timer = getattr(self, "_stop_automatic_refresh_timer", None)
        if callable(stop_refresh_timer):
            stop_refresh_timer()

        preview = getattr(getattr(self, "infoBox", None), "preview", None)
        if preview is not None:
            preview.shutdown()
        self.startupDatabaseTimer.stop()
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()
        detail_worker = getattr(self, "_database_detail_worker", None)
        if detail_worker is not None:
            detail_worker.cancel()
        expensive_detail_worker = getattr(
            self,
            "_database_expensive_detail_worker",
            None,
            )
        if expensive_detail_worker is not None:
            expensive_detail_worker.cancel()
        refresh_worker = getattr(self, "_database_refresh_worker", None)
        if refresh_worker is not None:
            refresh_worker.cancel()
        selected_run_worker = getattr(self, "_database_selected_run_worker", None)
        if selected_run_worker is not None:
            selected_run_worker.cancel()
        generation_worker = getattr(self, "_test_database_generation_worker", None)
        if generation_worker is not None:
            generation_worker.cancel()
        MainWindow._cancel_plot_work(self)
        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
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
        self._database_refresh_generation = (
            getattr(self, "_database_refresh_generation", 0) + 1
            )
        self._database_refresh_active = False
        self._database_refresh_pending = False
        self._database_refresh_worker = None
        self._database_refresh_identity = None
        self._database_refresh_instance = None
        self._database_selected_run_generation = (
            getattr(self, "_database_selected_run_generation", 0) + 1
            )
        self._database_selected_run_worker = None
        self._database_selected_run_instance = None
        self._database_selected_run_mode = None
        self._restoring_selected_run_publication = False
        self._selected_run_restore_pending = False
        self._test_database_generation_active = False
        self._test_database_generation_worker = None
        self._test_database_replacement_state = None
        self._database_view_released_for_generation = False
        stop_refresh_timer = getattr(self, "_stop_automatic_refresh_timer", None)
        if callable(stop_refresh_timer):
            stop_refresh_timer()
        else:
            self.monitor.stop()
        self.close_plot_windows(confirm=False, status=False)
        self.close_database(status=False)

        if MainWindow._shutdown_background_work_active(self):
            self._shutdown_started = True
            event.ignore()
            hide = getattr(self, "hide", None)
            if callable(hide):
                hide()
            shutdown_timer = getattr(self, "_shutdown_timer", None)
            if shutdown_timer is not None:
                shutdown_timer.start()
            else:
                QtCore.QTimer.singleShot(25, self._finish_deferred_shutdown)
            return

        self._shutdown_ready = True
        event.accept()
        qtw.QApplication.closeAllWindows()


    def _cancel_plot_work(self):
        """Cancel running plot loads and remove work that has not started."""

        workers = set(getattr(self, "_plot_workers", ()))
        for window in list(getattr(self, "windows", ())):
            worker = getattr(window, "worker", None)
            if worker is not None:
                workers.add(worker)

        for worker in workers:
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()

        pool = getattr(self, "threadPool", None)
        clear = getattr(pool, "clear", None)
        if callable(clear):
            clear()


    def _publish_shutdown_diagnostics(self):
        """Merge durable escalation errors with the newest liveness scan."""

        diagnostics = tuple(
            (
                *getattr(self, "_shutdown_escalation_diagnostics", ()),
                *getattr(self, "_shutdown_liveness_diagnostics", ()),
            )
        )
        self._shutdown_last_diagnostics = diagnostics
        process_fail_safe = getattr(self, "_shutdown_process_fail_safe", None)
        if process_fail_safe is not None:
            process_fail_safe.update_diagnostics(diagnostics)


    def _shutdown_background_work_active(self):
        """
        Reports whether a qPlot worker still needs the Qt event loop.

        """
        diagnostics = []
        active = False
        pool_names = (
            "threadPool",
            "databaseLoadThreadPool",
            "databaseDetailThreadPool",
            "databaseExpensiveDetailThreadPool",
            "databaseRefreshThreadPool",
            "databaseSelectedRunThreadPool",
            "testDatabaseGenerationThreadPool",
        )
        for pool_name in pool_names:
            pool = getattr(self, pool_name, None)
            if pool is None:
                continue
            try:
                count = pool.activeThreadCount()
            except BaseException as error:
                active = True
                diagnostics.append(
                    f"pool {pool_name}: liveness raised "
                    f"{type(error).__name__}: {error}"
                )
                continue
            if count > 0:
                active = True
                diagnostics.append(f"pool {pool_name}: active_threads={count}")

        reap_services = getattr(self, "_reap_retired_trusted_read_services", None)
        if callable(reap_services):
            try:
                reap_services()
            except BaseException as error:
                active = True
                diagnostics.append(
                    "retired-service reaper raised "
                    f"{type(error).__name__}: {error}"
                )
        diagnostics.extend(
            getattr(self, "_retired_service_reap_diagnostics", {}).values()
        )
        services = set(getattr(self, "_retired_trusted_read_services", ()))
        active_service = getattr(self, "_trusted_read_service", None)
        if active_service is not None:
            services.add(active_service)
        services.update(
            getattr(self, "_pending_trusted_read_services", {}).values()
        )
        for service in services:
            service_label = (
                f"{type(service).__module__}.{type(service).__qualname__}"
                f"@{id(service):x}"
            )
            service_errors = []
            for error_name in ("fatal_error", "close_error"):
                try:
                    service_error = getattr(service, error_name, None)
                except BaseException as error:
                    active = True
                    service_errors.append(
                        f"{error_name} probe raised {type(error).__name__}: {error}"
                    )
                    continue
                if service_error is not None:
                    service_errors.append(
                        f"{error_name}={type(service_error).__name__}: {service_error}"
                    )
            try:
                liveness = service.liveness()
            except BaseException as error:
                active = True
                diagnostics.append(
                    f"service {service_label}: liveness raised "
                    f"{type(error).__name__}: {error}"
                    + ("; " + "; ".join(service_errors) if service_errors else "")
                )
                continue
            field_names = (
                "dispatcher_alive",
                "control_alive",
                "helper_alive",
                "helper_pid",
                "receiver_alive",
                "open_supervisor_endpoints",
                "unreaped_incarnations",
                "resource_cleanup_pending",
                "outstanding_requests",
                "closing",
                "closed",
            )
            values = {
                field_name: getattr(liveness, field_name, None)
                for field_name in field_names
            }
            service_active = bool(
                values["dispatcher_alive"]
                or values["control_alive"]
                or values["helper_alive"]
                or values["receiver_alive"]
                or values["open_supervisor_endpoints"]
                or values["unreaped_incarnations"]
                or values["resource_cleanup_pending"]
                or values["outstanding_requests"]
                or not values["closed"]
            )
            if service_active:
                active = True
                diagnostics.append(
                    f"service {service_label}: "
                    + ", ".join(
                        f"{field_name}={values[field_name]!r}"
                        for field_name in field_names
                    )
                    + ("; " + "; ".join(service_errors) if service_errors else "")
                )

        preview = getattr(getattr(self, "infoBox", None), "preview", None)
        preview_workers = getattr(preview, "_workers", {})
        if preview_workers:
            active = True
            diagnostics.append(f"preview_workers={len(preview_workers)}")
        self._shutdown_liveness_diagnostics = tuple(diagnostics)
        MainWindow._publish_shutdown_diagnostics(self)
        return active


    def _escalate_shutdown_cleanup(self):
        """Repeat cancellation and zero-wait cleanup once during shutdown."""

        if getattr(self, "_shutdown_cleanup_escalated", False):
            return
        self._shutdown_cleanup_escalated = True
        escalation_diagnostics = []

        try:
            MainWindow._cancel_plot_work(self)
        except BaseException as error:
            escalation_diagnostics.append(
                f"plot cancellation raised {type(error).__name__}: {error}"
            )

        for pool_name in (
            "threadPool",
            "databaseLoadThreadPool",
            "databaseDetailThreadPool",
            "databaseExpensiveDetailThreadPool",
            "databaseRefreshThreadPool",
            "databaseSelectedRunThreadPool",
            "testDatabaseGenerationThreadPool",
        ):
            pool = getattr(self, pool_name, None)
            clear = getattr(pool, "clear", None)
            if not callable(clear):
                continue
            try:
                clear()
            except BaseException as error:
                escalation_diagnostics.append(
                    f"pool {pool_name} clear raised {type(error).__name__}: {error}"
                )

        preview = getattr(getattr(self, "infoBox", None), "preview", None)
        shutdown = getattr(preview, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except BaseException as error:
                escalation_diagnostics.append(
                    f"preview shutdown raised {type(error).__name__}: {error}"
                )

        services = set(getattr(self, "_retired_trusted_read_services", ()))
        active_service = getattr(self, "_trusted_read_service", None)
        if active_service is not None:
            services.add(active_service)
        services.update(
            getattr(self, "_pending_trusted_read_services", {}).values()
        )
        for service in services:
            escalate = getattr(service, "escalate_cleanup_async", None)
            if not callable(escalate):
                escalate = getattr(service, "close_async", None)
            if not callable(escalate):
                continue
            try:
                escalate()
            except BaseException as error:
                escalation_diagnostics.append(
                    "service cleanup escalation raised "
                    f"{type(error).__name__}: {error}"
                )

        reap_services = getattr(self, "_reap_retired_trusted_read_services", None)
        if callable(reap_services):
            try:
                reap_services()
            except BaseException as error:
                escalation_diagnostics.append(
                    "retired-service escalation reap raised "
                    f"{type(error).__name__}: {error}"
                )
        if escalation_diagnostics:
            self._shutdown_escalation_diagnostics = tuple(
                (
                    *getattr(self, "_shutdown_escalation_diagnostics", ()),
                    *escalation_diagnostics,
                )
            )
            MainWindow._publish_shutdown_diagnostics(self)


    def _complete_deferred_shutdown(self, *, deadline_exhausted=False):
        """Stop shutdown polling and make the next close event unconditional."""

        shutdown_timer = getattr(self, "_shutdown_timer", None)
        if shutdown_timer is not None:
            shutdown_timer.stop()
        reaper_timer = getattr(self, "_retired_service_reaper_timer", None)
        if reaper_timer is not None:
            reaper_timer.stop()

        if deadline_exhausted:
            diagnostics = tuple(getattr(self, "_shutdown_last_diagnostics", ()))
            if not diagnostics:
                diagnostics = ("background work remained active without diagnostics",)
            self._shutdown_diagnostics = diagnostics
            process_fail_safe = getattr(self, "_shutdown_process_fail_safe", None)
            if process_fail_safe is not None:
                process_fail_safe.update_diagnostics(diagnostics)
                process_fail_safe.persist_async()
            else:
                started_at = getattr(self, "_shutdown_started_at", None)
                if started_at is None:
                    started_at = monotonic()
                _persist_shutdown_diagnostics(
                    started_at=started_at,
                    total_timeout=_APPLICATION_SHUTDOWN_TIMEOUT_SECONDS,
                    diagnostics=diagnostics,
                )
        self._shutdown_deadline_exhausted = bool(deadline_exhausted)

        self._shutdown_started = False
        self._shutdown_ready = True
        qtw.QApplication.closeAllWindows()
        application = qtw.QApplication.instance()
        if application is not None and isinstance(self, qtw.QWidget):
            application.quit()


    @QtCore.pyqtSlot()
    def _finish_deferred_shutdown(self):
        """
        Finishes Quit after cancelled background workers have returned.

        """
        if not getattr(self, "_shutdown_started", False):
            return
        active = MainWindow._shutdown_background_work_active(self)
        if active and not getattr(self, "_shutdown_cleanup_escalated", False):
            MainWindow._escalate_shutdown_cleanup(self)
            active = MainWindow._shutdown_background_work_active(self)

        deadline = getattr(self, "_shutdown_deadline", None)
        if deadline is None:
            deadline = monotonic() + _APPLICATION_SHUTDOWN_TIMEOUT_SECONDS
            self._shutdown_deadline = deadline
        if active and monotonic() < deadline:
            return

        MainWindow._complete_deferred_shutdown(
            self,
            deadline_exhausted=active,
        )
    
   
    @QtCore.pyqtSlot()
    def closeAll(self):
        """
        Event handler for close all menu button.
        Closes all windows other than the main window.

        """
        self.close_plot_windows(confirm=True, status=True)


    def close_plot_windows(self, confirm=True, status=True):
        """
        Closes all plot windows, optionally asking for confirmation.

        """
        plot_windows = self.windows.copy()
        if not plot_windows:
            if status:
                self.show_status("No plot windows to close.", 3000)
            return True

        if confirm and close_all_warning_enabled(self.config):
            count = len(plot_windows)
            noun = "window" if count == 1 else "windows"
            reply = ask_confirmation_with_dont_ask_again(
                self,
                "Close All Plot Windows",
                f"Close {count} plot {noun}?",
                CONFIRM_CLOSE_ALL_KEY,
                qtw.QMessageBox.StandardButton.No,
                )
            if reply != qtw.QMessageBox.StandardButton.Yes:
                if status:
                    self.show_status("Close all plot windows cancelled.", 3000)
                return False

        if status:
            self.show_status("Closing plot windows...", 3000)
        for win in plot_windows:
            win.close()
        return True
        
        
    def change_theme(self, theme, action):
        """
        Event handler for changing style/theme.
        Updates Main Window theme and all other Plot windows.

        Parameters
        ----------
        theme : str
            Name of the theme to change to.
        action : PyQt6.QtWidgets.QAction
            Button which sent the signal for the action.

        """
        if self.config.get("user_preference.theme") == theme: #already selected
            set_widget_value_without_signals(action, action.setChecked, True)
            self.show_status(f"{theme.title()} theme already selected.", 3000)
            return True

        def sync_theme_actions():
            current_theme = self.config.get("user_preference.theme")
            actions = list(getattr(self, "themes", []))
            if action not in actions:
                actions.append(action)
            blocked_states = [
                theme_action.blockSignals(True)
                for theme_action in actions
                ]
            try:
                for theme_action in actions:
                    action_theme = theme_action.text().replace("&", "").lower()
                    theme_action.setChecked(action_theme == current_theme)
            finally:
                for theme_action, signals_were_blocked in zip(
                        actions,
                        blocked_states,
                        strict=True,
                        ):
                    theme_action.blockSignals(signals_were_blocked)

        if not persist_config_value(
                self,
                self.config,
                "user_preference.theme",
                theme,
                "the theme preference",
                sync_theme_actions,
                ):
            return False

        sync_theme_actions()
        
        # Update all windows.
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)
        self.show_status(f"Theme changed to {theme}.", 2000)
        return True


    def change_preview_size(self, preview_size):
        """
        Updates preview image size and regenerates preview thumbnails.

        """
        preview_size = int(preview_size)
        if preview_size == self.preview_size:
            return True

        if not self._save_preview_size(preview_size):
            return False

        self._apply_preview_size(preview_size)
        self.show_status(f"Preview size set to {preview_size} px.", 3000)
        return True


    @QtCore.pyqtSlot()
    def restore_default_settings(self):
        """
        Confirms and resets all user settings to schema defaults.

        """
        reply = qtw.QMessageBox.question(
            self,
            "Reset All Settings",
            "Reset all qPlot settings to their defaults? "
            "This will also close the current database and all plot windows.",
            qtw.QMessageBox.StandardButton.Yes | qtw.QMessageBox.StandardButton.No,
            qtw.QMessageBox.StandardButton.No,
            )
        if reply != qtw.QMessageBox.StandardButton.Yes:
            self.show_status("Settings reset cancelled.", 3000)
            return

        if not persist_config_action(
                self,
                self.config.reset_to_defaults,
                "the default settings",
                ):
            return False

        self.close_plot_windows(confirm=False, status=False)
        self.apply_current_settings()
        self.close_database(status=False)
        self.show_status("Settings reset to defaults.", 5000)
        return True


    def show_preferences_dialog(self):
        """
        Opens the preferences dialog.

        """
        dialog = PreferencesDialog(self.config, self)
        dialog.preferencesApplied.connect(self.apply_current_settings)
        dialog.preferencesApplied.connect(
            lambda: self.show_status("Preferences saved.", 3000)
            )
        dialog.exec()


    def apply_current_settings(self):
        """
        Applies config-backed settings that can be updated in open windows.

        """
        self._sync_theme_actions()
        self._sync_preview_size_actions()
        self._sync_refresh_interval()
        self._sync_thread_pool_settings()
        run_list = getattr(self, "RunList", None)
        if run_list is not None:
            if hasattr(run_list, "apply_configured_column_widths"):
                run_list.apply_configured_column_widths()
            if hasattr(run_list, "apply_configured_column_visibility"):
                run_list.apply_configured_column_visibility()
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)
            apply_major_ticks = getattr(
                win,
                "apply_axis_major_tick_count_preference",
                None,
                )
            if callable(apply_major_ticks):
                apply_major_ticks()
            apply_colorbar_width = getattr(win, "apply_colorbar_width_preference", None)
            if callable(apply_colorbar_width):
                apply_colorbar_width()
        self._sync_mouse_mode_settings()


    def _sync_theme_actions(self):
        current_theme = self.config.get("user_preference.theme")
        for action in getattr(self, "themes", []):
            action.blockSignals(True)
            action.setChecked(action.text().replace("&", "").lower() == current_theme)
            action.blockSignals(False)


    def _sync_preview_size_actions(self):
        self._apply_preview_size(self._configured_preview_size())


    def _apply_preview_size(self, preview_size):
        """Apply a persisted preview size to the live UI once.

        Persistence is intentionally kept outside this method.  This lets menu
        actions, Preferences, and resetting settings share the same runtime
        cache invalidation and visible-row regeneration path.
        """
        preview_size = int(preview_size)
        size_changed = preview_size != self.preview_size
        self.preview_size = preview_size
        for action in getattr(self, "previewSizeActions", []):
            action.blockSignals(True)
            action.setChecked(action.data() == self.preview_size)
            action.blockSignals(False)

        if size_changed and hasattr(self, "infoBox"):
            self.infoBox.set_preview_size(self.preview_size)
            prioritize_previews = getattr(self, "_prioritize_preview_runs", None)
            if callable(prioritize_previews):
                prioritize_previews()
            if hasattr(self, "runInfoSplitter"):
                self.runInfoSplitter.setSizes([380, self._details_pane_height()])

        return size_changed


    def _sync_thread_pool_settings(self):
        if hasattr(self, "threadPool"):
            self.threadPool.setMaxThreadCount(
                self.config.get("runtime_settings.max_threads")
                )


    def _sync_mouse_mode_settings(self):
        for win in getattr(self, "windows", []):
            if hasattr(win, "apply_mouse_mode_preference"):
                win.apply_mouse_mode_preference()


    def _save_preview_size(self, preview_size):
        previous_size = self._configured_preview_size()

        def rollback():
            actions = list(getattr(self, "previewSizeActions", []))
            blocked_states = [action.blockSignals(True) for action in actions]
            try:
                for action in actions:
                    action.setChecked(action.data() == previous_size)
            finally:
                for action, signals_were_blocked in zip(
                        actions,
                        blocked_states,
                        strict=True,
                        ):
                    action.blockSignals(signals_were_blocked)

        return persist_config_value(
            self,
            self.config,
            "GUI.preview_size",
            int(preview_size),
            "the preview size",
            rollback,
            )


###############################################################################
#Other funcs

    def show_status(self, message : str, timeout : int = 5000):
        """
        Shows a short message in the main window status bar.

        """
        status_bar = cast(qtw.QStatusBar, self.statusBar())
        status_bar.showMessage(message, timeout)

        # A preview-drop action is initiated in a separate plot window, which
        # can cover the main window and its status bar.  Mirror feedback to
        # that plot for the duration of the action so rejected drops have an
        # immediately visible explanation.
        target = getattr(self, "_preview_drop_feedback_window", None)
        target_show_status = getattr(target, "show_status", None)
        if callable(target_show_status):
            target_show_status(message, timeout)


    def show_error(self, title : str, message : str, details : str | None = None):
        """
        Shows an error both in the status bar and in a message box.

        """
        log_user_error(title, message, details, __name__)
        self.show_status(message, 10_000)

        box = qtw.QMessageBox(qtw.QMessageBox.Icon.Warning, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec()
