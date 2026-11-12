import os
from time import perf_counter
from typing import cast

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)

from qplot import config
from qplot._metadata import version_info
from qplot._process_shutdown import persist_shutdown_diagnostics
from qplot.datahandling.database import (
    DatabaseDetailWorker,
    DatabaseExpensiveDetailWorker,
    DatabaseLoadWorker,
    DatabaseRefreshWorker,
    DatabaseSelectedRunWorker,
    database_info_report,
    database_path_from_mime_data,
)
from qplot.diagnostics import log_user_error
from qplot.windows._database_detail_state import detail_channel

from ._commands import create_action
from ._complex_plots import apply_complex_preference
from ._config_persistence import (
    persist_config_action,
    persist_config_value,
    set_widget_value_without_signals,
)
from ._database_actions import DatabaseActionsMixin, TestDatabaseGenerationWorker
from ._database_load_state import DatabaseLoadSession
from ._dataset_handle import DatasetHandle, DatasetKey
from ._error_dialog import ErrorDialog
from ._help import add_help_menu
from ._plot_actions import PlotActionsMixin
from ._preferences import (
    PreferencesDialog,
    create_preferences_action,
)
from ._run_controls import AUTO_PLOT_KEY, RunControlsMixin
from ._shutdown_coordinator import ShutdownCoordinator
from ._signal_blocking import blocked_signals
from ._status import StatusLabel
from ._trusted_derived_qt import TrustedDerivedQtBridge
from ._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    add_application_quit_action,
    add_restore_defaults_option,
    add_standard_window_controls,
    ask_confirmation_with_dont_ask_again,
    close_all_warning_enabled,
)

# Public window types and the worker/report aliases exposed by this module.
__all__ = [
    "MainWindow",
    "DatabasePathLineEdit",
    "DatabaseDetailWorker",
    "DatabaseExpensiveDetailWorker",
    "DatabaseLoadWorker",
    "DatabaseRefreshWorker",
    "DatabaseSelectedRunWorker",
    "TestDatabaseGenerationWorker",
    "database_info_report",
    "database_path_from_mime_data",
]

MAIN_WINDOW_READABLE_WIDTH = 804
_APPLICATION_SHUTDOWN_TIMEOUT_SECONDS = 15.0
_APPLICATION_SHUTDOWN_DIAGNOSTIC_GRACE_SECONDS = 0.25
_RETIRED_SERVICE_REAPER_INTERVAL_MS = 100


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
        # The process entry point installs and owns the fail-safe. Embedded
        # windows remain inert at the process boundary.
        self._shutdown_process_fail_safe = None
        self._shutdown = ShutdownCoordinator(
            self,
            timeout_seconds=_APPLICATION_SHUTDOWN_TIMEOUT_SECONDS,
            diagnostic_grace_seconds=_APPLICATION_SHUTDOWN_DIAGNOSTIC_GRACE_SECONDS,
            persist_diagnostics=persist_shutdown_diagnostics,
        )
        resources = self._shutdown.resources
        self.threadPool = resources.create_pool(
            "threadPool",
            max_threads=self.config.get("runtime_settings.max_threads"),
        )
        self._plot_workers: set[object] = set()
        self.threadPool._qplot_workers = self._plot_workers  # type: ignore[attr-defined]
        self.databaseLoadThreadPool = resources.create_pool(
            "databaseLoadThreadPool", parent=self,
            worker_attribute="_database_load_worker",
        )
        self.databaseDetailThreadPool = resources.create_pool(
            "databaseDetailThreadPool", parent=self,
        )
        resources.watch_worker("basic details", cancel=detail_channel(self).cancel_worker)
        self.databaseExpensiveDetailThreadPool = resources.create_pool(
            "databaseExpensiveDetailThreadPool", parent=self,
        )
        resources.watch_worker("expensive details", cancel=detail_channel(self, True).cancel_worker)
        self.databaseRefreshThreadPool = resources.create_pool(
            "databaseRefreshThreadPool", parent=self,
            worker_attribute="_database_refresh_worker",
        )
        # Information jobs share the detail pool but have a separate cancellation
        # hook that invalidates their own generation before later work is closed.
        resources.watch_worker(
            "_database_info_worker", cancel=self._cancel_database_info,
        )
        self.testDatabaseGenerationThreadPool = resources.create_pool(
            "testDatabaseGenerationThreadPool", parent=self,
            worker_attribute="_test_database_generation_worker",
        )
        self._database_load_generation = 0
        self._database_load_active = False
        self._database_load_publication_active = False
        self._database_load_state: DatabaseLoadSession | None = None
        self._database_load_worker = None
        self._database_info_generation = 0
        self._database_info_worker = None
        self._database_info_instance = None
        self._database_info_active = False
        self._loaded_database_identity = None
        self._loaded_database_instance = None  # type: ignore[assignment]
        self._database_refresh_generation = 0
        self._database_refresh_active = False
        self._database_refresh_pending = False
        self._database_refresh_worker: DatabaseRefreshWorker | None = None
        self._database_refresh_identity = None
        self._database_refresh_instance = None
        self._database_refresh_staged_new_runs = {}
        self._database_refresh_publication_active = False
        self._database_selected_run_generation = 0
        self._restoring_selected_run_publication = False
        self._selected_run_restore_pending = False
        self._selected_run_guid = None
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
        self.infoBox.preview.set_complex_preferences((
            self.config.get("user_preference.complex_1d"),
            self.config.get("user_preference.complex_nd"),
        ))
        self._trusted_derived_bridge = TrustedDerivedQtBridge(self)
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
        self._version_info = version_info()
        self.setWindowTitle(f"qPlot - {self._version_info['version']}")
        self.status_label = StatusLabel(self)
        self.status_label.setObjectName("mainStatusMessage")
        self.status_label.setAccessibleName("Main window status")
        status_bar = cast(qtw.QStatusBar, self.statusBar())
        status_bar.addPermanentWidget(self.status_label, 1)
        startup_elapsed = perf_counter() - startup_start
        self.show_status(f"Ready - QPlot opened in {startup_elapsed:.2f} s")
        
        # Get user's window dimensions to control new window position
        primary_screen = cast(QtGui.QScreen, qtw.QApplication.primaryScreen())
        self.screenrect = primary_screen.availableGeometry()
        
        # Try to bring window to top 
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowType.WindowStaysOnTopHint) 
        self.show()
        startup_warning = getattr(self.config, "startup_warning", None)
        if startup_warning:
            self.show_status(startup_warning, 10_000)
        self.startupDatabaseTimer.start(0)
        from ._updates import configure_launch_check

        configure_launch_check(self, self._version_info)


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
        refreshAction.triggered.connect(self.refresh_all)
        fileMenu.addAction(refreshAction)

        fileMenu.addSeparator()

        export_runs = create_action("run.export_selected", self)
        export_runs.triggered.connect(self.export_selected_runs_csv)
        fileMenu.addAction(export_runs)
        from ._database_export import export_database

        export_database_action = create_action("database.export_copy", self)
        export_database_action.triggered.connect(lambda: export_database(self))
        fileMenu.addAction(export_database_action)
        exports_progress = create_action("exports.show", self)
        exports_progress.triggered.connect(self.show_csv_exports)
        fileMenu.addAction(exports_progress)
        fileMenu.addSeparator()

        test_data_menu = cast(qtw.QMenu, fileMenu.addMenu("Generate &Test Data"))
        bundled_csv_action = create_action("testdata.open_bundled_folder", self)
        bundled_csv_action.triggered.connect(self.open_bundled_test_database_csv_folder)
        test_data_menu.addAction(bundled_csv_action)
        test_data_menu.addSeparator()
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
        self.loadDatabaseButton.setText("Open…")
        self.loadDatabaseButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        self.loadDatabaseButton.setToolTip("Load a QCoDeS .db database (Ctrl+L)")
        self.loadDatabaseButton.setAccessibleName("Load database")
        self.loadDatabaseButton.setFixedHeight(26)
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
        self.databaseLoadProgress.setToolTip(
            "Database loading progress. A moving indicator means the total work is not yet known."
        )
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
        """Confirm exit, then delegate bounded shutdown to the coordinator."""
        if self._shutdown.ready:
            event.accept()
            return
        if self._shutdown.waiting:
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

        self._shutdown.begin(event)


    @property
    def _shutdown_started(self) -> bool:
        return self._shutdown.waiting


    @property
    def _shutdown_ready(self) -> bool:
        return self._shutdown.ready


    @property
    def _shutdown_deadline_exhausted(self) -> bool:
        return self._shutdown.state.deadline_exhausted


    def _cancel_plot_work(self):
        """Cancel plot work during database replacement or application shutdown."""
        self._shutdown.resources.cancel_plot_work()


    def _shutdown_background_work_active(self) -> bool:
        """Also used by the process entry point before destroying Qt ownership."""
        return self._shutdown.background_work_active()


    @QtCore.pyqtSlot()
    def _finish_deferred_shutdown(self):
        self._shutdown.poll()


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
            PlotActionsMixin._cancel_pending_plot_launches(self)
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

        PlotActionsMixin._cancel_pending_plot_launches(self)
        # Auto-plot and pending plot loads can publish while the modal
        # confirmation runs its nested event loop. Close those windows too,
        # before Close Database releases their dataset handles and readers.
        plot_windows = self.windows.copy()
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
            with blocked_signals(*actions):
                for theme_action in actions:
                    action_theme = theme_action.text().replace("&", "").lower()
                    theme_action.setChecked(action_theme == current_theme)

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
            return False

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
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()


    def apply_current_settings(self):
        """
        Applies config-backed settings that can be updated in open windows.

        """
        self._sync_theme_actions()
        self._sync_preview_size_actions()
        self._sync_refresh_interval()
        set_widget_value_without_signals(
            self.autoPlotBox,
            self.autoPlotBox.setChecked,
            self.config.get(AUTO_PLOT_KEY),
            )
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
        complex_changed = self.infoBox.preview.set_complex_preferences((
            self.config.get("user_preference.complex_1d"),
            self.config.get("user_preference.complex_nd"),
        ))
        bridge = self.__dict__.get("_trusted_derived_bridge")
        if bridge is not None:
            bridge.update_complex_preferences()
        if complex_changed:
            self._prioritize_preview_runs()
        for win in self.windows:
            apply_complex_preference(win)


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
            derived_bridge = getattr(self, "_trusted_derived_bridge", None)
            if derived_bridge is not None:
                derived_bridge.update_preview_size(self.preview_size)
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
            with blocked_signals(*actions):
                for action in actions:
                    action.setChecked(action.data() == previous_size)

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
        Show persistent feedback; timeout is retained for plot feedback callers.

        """
        self.status_label.show_message(message)

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

        box = ErrorDialog(title, message, details, parent=self)
        box.exec()
