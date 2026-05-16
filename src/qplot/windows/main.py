from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )
from PyQt5.QtGui import QKeySequence

from ._database_actions import DatabaseActionsMixin
from ._plot_actions import PlotActionsMixin
from ._preferences import PreferencesDialog
from ._run_controls import RunControlsMixin
from ._shortcuts import standard_key_sequences
from ._help import add_help_menu
from ._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    add_confirmation_options,
    add_restore_defaults_option,
    add_standard_window_controls,
    ask_confirmation_with_dont_ask_again,
    close_all_warning_enabled,
    )
from qplot.diagnostics import log_user_error
from qplot.datahandling.database import (
    DatabaseLoadWorker as DatabaseLoadWorker,
    database_info_report as database_info_report,
    database_path_from_mime_data as database_path_from_mime_data,
    )
from qplot import config

from qcodes.dataset.sqlite.database import (
    get_DB_location
    )

import os
from time import perf_counter

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
        self.preview_size = self._configured_preview_size()
        self.dataset_holder = {}
        self.monitor = QtCore.QTimer()
        self.threadPool = QtCore.QThreadPool()
        self.threadPool.setMaxThreadCount(self.config.get("runtime_settings.max_threads"))
        self.databaseLoadThreadPool = QtCore.QThreadPool(self)
        self.databaseLoadThreadPool.setMaxThreadCount(1)
        self._database_load_generation = 0
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self.x = 0
        self.y = 0
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
       
        # Fetch window size from config.json
        self.resize(*self.config.get("GUI.main_frame_size"))
        self.setWindowTitle("qPlot")
        startup_elapsed = perf_counter() - startup_start
        self.show_status(f"Ready - QPlot opened in {startup_elapsed:.2f} s")
        
        # Get user's window dimensions to control new window position
        self.screenrect = qtw.QApplication.primaryScreen().availableGeometry()
        self.x = self.screenrect.left() 
        self.y = self.screenrect.top()
        
        # Try to bring window to top 
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint) 
        self.show()
        self.startupDatabaseTimer.start(0)


    def initMenu(self):
        """
        Produces the menu bar and all menu's contained at the top of the window

        """
        menu = self.menuBar()
        # First dropdown menu
        fileMenu = menu.addMenu("&File") # Not sure why these all have &, but they do
        
        # Load database file
        loadAction = qtw.QAction("&Load Database...", self)
        loadAction.setShortcut("Ctrl+L")
        loadAction.setStatusTip("Load a QCoDeS database")
        loadAction.triggered.connect(self.getfile)
        fileMenu.addAction(loadAction)
        
        self.recentDatabaseMenu = fileMenu.addMenu("Load &Recent Database")
        self.refresh_recent_database_menu()

        open_folder_action = qtw.QAction("Open Database &Folder", self)
        open_folder_action.setShortcut("Ctrl+Shift+D")
        open_folder_action.setStatusTip("Open the folder containing the current database")
        open_folder_action.triggered.connect(self.open_database_location)
        fileMenu.addAction(open_folder_action)
        
        # Force update check on database
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)

        fileMenu.addSeparator()

        self.closeAllPlotsAction = qtw.QAction("Close All &Plot Windows", self)
        self.closeAllPlotsAction.setShortcut("Ctrl+Shift+W")
        self.closeAllPlotsAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        self.closeAllPlotsAction.setStatusTip("Close all open plot windows")
        self.closeAllPlotsAction.triggered.connect(self.closeAll)
        fileMenu.addAction(self.closeAllPlotsAction)

        closeAction = qtw.QAction("&Close Window", self)
        closeAction.setShortcuts(
            standard_key_sequences(QKeySequence.Close, ["Ctrl+W"])
            )
        closeAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        closeAction.setStatusTip("Close the main qPlot window")
        closeAction.triggered.connect(self.close)
        fileMenu.addAction(closeAction)

        quitAction = qtw.QAction("&Quit qPlot", self)
        quitAction.setShortcuts(
            standard_key_sequences(QKeySequence.Quit, ["Ctrl+Q"])
            )
        quitAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        quitAction.setStatusTip("Quit qPlot")
        quitAction.triggered.connect(self.close)
        fileMenu.addAction(quitAction)

        add_standard_window_controls(self)
        
        # Second dropdown menu
        prefMenu = menu.addMenu("&Options")

        preferences_action = qtw.QAction("&Preferences...", self)
        preferences_action.setShortcut("Ctrl+,")
        preferences_action.setStatusTip("Open qPlot preferences")
        preferences_action.triggered.connect(self.show_preferences_dialog)
        prefMenu.addAction(preferences_action)
        prefMenu.addSeparator()
        
        # Sets default open location for loadACtion
        default_load_picker = qtw.QAction("&Open Location", self)
        default_load_picker.triggered.connect(self.change_default_file)
        prefMenu.addAction(default_load_picker)
        
        # Change app stylesheet/theme
        themeMenu = prefMenu.addMenu("&Theme")
        
        current_theme = self.config.get("user_preference.theme")
        self.themes = []
        # Add all options to menu
        for itr, theme in enumerate(["Light", "Dark", "PyQt"]):
            self.themes.append(qtw.QAction(f'&{theme}', self, checkable=True))
            
            self.themes[itr].triggered.connect(
                lambda _, theme=theme.lower(), action=self.themes[itr]:
                    self.change_theme(theme, action) 
                )
                
            themeMenu.addAction(self.themes[itr])
            if theme.lower() == current_theme:
                self.themes[itr].setChecked(True)

        preview_menu = prefMenu.addMenu("&Preview Size")
        self.previewSizeGroup = qtw.QActionGroup(self)
        self.previewSizeGroup.setExclusive(True)
        self.previewSizeActions = []
        for size in (100, 150, 200, 300, 500):
            action = qtw.QAction(f"{size} px", self, checkable=True)
            action.setData(size)
            action.setChecked(size == self.preview_size)
            action.triggered.connect(
                lambda _, preview_size=size: self.change_preview_size(preview_size)
                )
            self.previewSizeGroup.addAction(action)
            self.previewSizeActions.append(action)
            preview_menu.addAction(action)

        prefMenu.addSeparator()
        add_restore_defaults_option(self, prefMenu)
        prefMenu.addSeparator()
        add_confirmation_options(self, prefMenu)
        add_help_menu(self)

    def initFile(self):
        """
        Display text box for current selected database
        
        """
        self.targetLayout = qtw.QHBoxLayout()
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
            self.style().standardIcon(qtw.QStyle.SP_FileDialogDetailedView)
            )
        self.copyDatabasePathButton.setToolTip("Copy the full database path")
        self.copyDatabasePathButton.setAccessibleName("Copy database path")
        self.copyDatabasePathButton.setFixedSize(28, 26)
        self.copyDatabasePathButton.clicked.connect(self.copy_database_path)
        self.targetLayout.addWidget(self.copyDatabasePathButton)

        self.databaseInfoButton = qtw.QToolButton()
        self.databaseInfoButton.setObjectName("databaseIconButton")
        self.databaseInfoButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_MessageBoxInformation)
            )
        self.databaseInfoButton.setToolTip("Show database information")
        self.databaseInfoButton.setAccessibleName("Show database information")
        self.databaseInfoButton.setFixedSize(28, 26)
        self.databaseInfoButton.clicked.connect(self.show_database_info)
        self.targetLayout.addWidget(self.databaseInfoButton)

        self.loadDatabaseButton = qtw.QToolButton()
        self.loadDatabaseButton.setObjectName("databaseIconButton")
        self.loadDatabaseButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DialogOpenButton)
            )
        self.loadDatabaseButton.setToolTip("Load a QCoDeS .db database (Ctrl+L)")
        self.loadDatabaseButton.setAccessibleName("Load database")
        self.loadDatabaseButton.setFixedSize(28, 26)
        self.loadDatabaseButton.clicked.connect(self.getfile)
        self.targetLayout.addWidget(self.loadDatabaseButton)

        self.openDatabaseFolderButton = qtw.QToolButton()
        self.openDatabaseFolderButton.setObjectName("databaseIconButton")
        self.openDatabaseFolderButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DirOpenIcon)
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
            qtw.QSizePolicy.Expanding,
            qtw.QSizePolicy.Preferred,
            )
        database_load_layout.addWidget(self.databaseLoadLabel, 1)

        self.databaseLoadCancelButton = qtw.QToolButton()
        self.databaseLoadCancelButton.setObjectName("databaseIconButton")
        self.databaseLoadCancelButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DialogCancelButton)
            )
        self.databaseLoadCancelButton.setText("Cancel")
        self.databaseLoadCancelButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonTextBesideIcon
            )
        self.databaseLoadCancelButton.setToolTip("Cancel the current database load")
        self.databaseLoadCancelButton.setAccessibleName("Cancel database load")
        self.databaseLoadCancelButton.setFixedSize(78, 24)
        self.databaseLoadCancelButton.clicked.connect(self.cancel_database_load)
        database_load_layout.addWidget(self.databaseLoadCancelButton)
        self.databaseLoadFrame.setVisible(False)
        
        if os.path.isfile(get_DB_location()):
            self.fileTextbox.setText(str(get_DB_location()))
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Event handler for closing Main Window.

        Also handles some closing admin        

        """
        # Confirm exit
        if self.config.get(CONFIRM_QUIT_KEY):
            reply = ask_confirmation_with_dont_ask_again(
                self,
                "Confirm Exit",
                "Are you sure you want to exit?",
                CONFIRM_QUIT_KEY,
                )
            if reply == qtw.QMessageBox.Yes:
                event.accept()
            else:
                event.ignore()
                return

        self.startupDatabaseTimer.stop()
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()
        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self.monitor.stop()
        qtw.QApplication.closeAllWindows()
    
   
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
                qtw.QMessageBox.No,
                )
            if reply != qtw.QMessageBox.Yes:
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
        action : PyQt5.QtWidgets.QAction
            Button which sent the signal for the action.

        """
        if self.config.get("user_preference.theme") == theme: #already selected
            action.setChecked(True)
            self.show_status(f"{theme.title()} theme already selected.", 3000)
            return
        for QActions in self.themes: # Untick other options
            if QActions != action:
                QActions.setChecked(False)
                
        # Update config.jon
        self.config.update("user_preference.theme", theme)
        
        # Update all windows.
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)
        self.show_status(f"Theme changed to {theme}.", 2000)


    def change_preview_size(self, preview_size):
        """
        Updates preview image size and regenerates preview thumbnails.

        """
        preview_size = int(preview_size)
        if preview_size == self.preview_size:
            return

        self.preview_size = preview_size
        self._save_preview_size(preview_size)
        if hasattr(self, "infoBox"):
            self.infoBox.set_preview_size(preview_size)
            if hasattr(self, "runInfoSplitter"):
                self.runInfoSplitter.setSizes([380, self._details_pane_height()])
        self.show_status(f"Preview size set to {preview_size} px.", 3000)


    @QtCore.pyqtSlot()
    def restore_default_settings(self):
        """
        Confirms and restores all user settings to schema defaults.

        """
        reply = qtw.QMessageBox.question(
            self,
            "Restore Default Settings",
            "Restore all qPlot settings to their defaults?",
            qtw.QMessageBox.Yes | qtw.QMessageBox.No,
            qtw.QMessageBox.No,
            )
        if reply != qtw.QMessageBox.Yes:
            self.show_status("Default settings restore cancelled.", 3000)
            return

        self.close_plot_windows(confirm=False, status=False)
        self.config.reset_to_defaults()
        self.apply_current_settings()
        self.close_database(status=False)
        self.show_status("Default settings restored.", 5000)


    def show_preferences_dialog(self):
        """
        Opens the preferences dialog.

        """
        dialog = PreferencesDialog(self.config, self)
        dialog.preferencesApplied.connect(self.apply_current_settings)
        dialog.preferencesApplied.connect(
            lambda: self.show_status("Preferences saved.", 3000)
            )
        dialog.exec_()


    def apply_current_settings(self):
        """
        Applies config-backed settings that can be updated in open windows.

        """
        self._sync_theme_actions()
        self._sync_preview_size_actions()
        self._sync_refresh_interval()
        self._sync_thread_pool_settings()
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)


    def _sync_theme_actions(self):
        current_theme = self.config.get("user_preference.theme")
        for action in getattr(self, "themes", []):
            action.blockSignals(True)
            action.setChecked(action.text().replace("&", "").lower() == current_theme)
            action.blockSignals(False)


    def _sync_preview_size_actions(self):
        self.preview_size = self._configured_preview_size()
        for action in getattr(self, "previewSizeActions", []):
            action.blockSignals(True)
            action.setChecked(action.data() == self.preview_size)
            action.blockSignals(False)

        if hasattr(self, "infoBox"):
            self.infoBox.set_preview_size(self.preview_size)
            if hasattr(self, "runInfoSplitter"):
                self.runInfoSplitter.setSizes([380, self._details_pane_height()])


    def _sync_thread_pool_settings(self):
        if hasattr(self, "threadPool"):
            self.threadPool.setMaxThreadCount(
                self.config.get("runtime_settings.max_threads")
                )


    def _save_preview_size(self, preview_size):
        gui_config = self.config.config.setdefault("GUI", {})
        if "preview_size" not in gui_config:
            gui_config["preview_size"] = self.preview_size
        self.config.update("GUI.preview_size", int(preview_size))


###############################################################################
#Other funcs

    def show_status(self, message : str, timeout : int = 5000):
        """
        Shows a short message in the main window status bar.

        """
        self.statusBar().showMessage(message, timeout)


    def show_error(self, title : str, message : str, details : str = None):
        """
        Shows an error both in the status bar and in a message box.

        """
        log_user_error(title, message, details, __name__)
        self.show_status(message, 10000)

        box = qtw.QMessageBox(qtw.QMessageBox.Warning, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec_()


