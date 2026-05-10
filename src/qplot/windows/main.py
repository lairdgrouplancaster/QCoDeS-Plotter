from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )
from PyQt5.QtGui import (
    QDesktopServices,
    QIntValidator,
    QKeySequence,
    )

from qplot.windows import (
    plot1d,
    plot2d,
    )
from ._widgets import (
    RunList,
    moreInfo,
    )
from ._widgets.preview import PREVIEW_SIZE
from ._shortcuts import standard_key_sequences
from ._window_controls import (
    add_confirmation_options,
    add_restore_defaults_option,
    add_standard_window_controls,
    close_all_warning_enabled,
    )
from qplot.datahandling import (
    find_new_runs
    )
from qplot import config

from qcodes.dataset import (
    initialise_or_create_database_at,
    load_by_id,
    load_by_guid
    )
from qcodes.dataset.sqlite.database import (
    get_DB_location
    )

import os
from time import perf_counter

import numpy as np
import pandas as pd


def database_path_from_mime_data(mime_data):
    """
    Return a dropped local .db path, if the drop contains exactly one.

    """
    if not mime_data.hasUrls():
        return None

    urls = mime_data.urls()
    if len(urls) != 1:
        return None

    url = urls[0]
    if not url.isLocalFile():
        return None

    path = url.toLocalFile()
    if os.path.isfile(path) and path.lower().endswith(".db"):
        return path

    return None


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


class MainWindow(qtw.QMainWindow):
    """
    The Main application which connects/initialises QCoDeS database, displays
    available options plots to open, and opens windows.
    
    This window can be opened by calling qplot.run()
    
    Holds a shallow copy of all other open windows to prevent deletion by 
    python's garbarge collector
    """
    
    def __init__(self):
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
        self.x = 0
        self.y = 0
        self.localLastFile = None
        
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


    def initRefresh(self):
        """
        Initialise the main window refresh.Refresh checks for any new runs 
        added to the dataset.
        
        """
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        self.spinBox.setFixedWidth(72)
        self.spinBox.setAlignment(QtCore.Qt.AlignRight)
        self.spinBox.setToolTip("Refresh interval in seconds")
    
        # Slot connections
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshMain)
        
        self.autoPlotBox = qtw.QCheckBox()
        self.autoPlotBox.setToolTip("Automatically open plots for newly detected runs")

        self.closeAllPlotsButton = qtw.QToolButton()
        self.closeAllPlotsButton.setObjectName("closeAllPlotsButton")
        self.closeAllPlotsButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_TitleBarCloseButton)
            )
        self.closeAllPlotsButton.setToolTip("Close all plot windows (Ctrl+Shift+W)")
        self.closeAllPlotsButton.setAccessibleName("Close all plot windows")
        self.closeAllPlotsButton.setFixedSize(28, 26)
        self.closeAllPlotsButton.clicked.connect(self.closeAll)
    
    
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
        
        # Load accessed database
        self.loadLastAction = qtw.QAction("&Load Last", self)
        self.loadLastAction.setShortcut("Ctrl+Shift+L")
        self.loadLastAction.triggered.connect(self.loadLastFile)
        fileMenu.addAction(self.loadLastAction)
        if not self.config.get("file.last_file_path"): # has user openned a DB before?
            self.loadLastAction.setDisabled(True)

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
        
        if os.path.isfile(get_DB_location()):
            self.fileTextbox.setText(str(get_DB_location()))
        
        
    def initRunDisplay(self):
        sublayout = qtw.QHBoxLayout()
        sublayout.setContentsMargins(8, 0, 8, 2)
        sublayout.setSpacing(6)

        sublayout.addWidget(qtw.QLabel("Run:"))
        
        self.selected_run_id = None
        
        # Box for User to enter specific run_id
        self.run_idBox = qtw.QLineEdit()
        self.run_idBox.setMaximumWidth(58)
        self.run_idBox.setFixedWidth(58)
        # Only allow int in box between 1 and 9999999
        self.run_idBox.setValidator(QIntValidator())
        self.run_idBox.setPlaceholderText("ID")
        self.run_idBox.setToolTip("Run ID to plot")
        self.run_idBox.textEdited.connect(self.update_run_id)
        self.run_idBox.editingFinished.connect(self.sync_run_id_selection)
        self.run_idBox.returnPressed.connect(self.openRun)
        sublayout.addWidget(self.run_idBox)

        sublayout.addWidget(qtw.QLabel("Measurement:"))

        self.measurementBox = qtw.QLineEdit()
        self.measurementBox.setMaximumWidth(46)
        self.measurementBox.setFixedWidth(46)
        self.measurementBox.setText("*")
        self.measurementBox.setToolTip("Measurement to plot; * to plot all")
        self.measurementBox.returnPressed.connect(self.openRun)
        sublayout.addWidget(self.measurementBox)

        self.plotRunButton = qtw.QToolButton()
        self.plotRunButton.setObjectName("plotIconButton")
        self.plotRunButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_MediaPlay)
            )
        self.plotRunButton.setToolTip("Plot (Ctrl+Return)")
        self.plotRunButton.setAccessibleName("Plot measurement")
        self.plotRunButton.setFixedSize(28, 26)
        self.plotRunButton.clicked.connect(self.openRun)
        sublayout.addWidget(self.plotRunButton)

        self.exportCsvButton = qtw.QToolButton()
        self.exportCsvButton.setObjectName("exportIconButton")
        self.exportCsvButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DialogSaveButton)
            )
        self.exportCsvButton.setToolTip("Export CSV")
        self.exportCsvButton.setAccessibleName("Export measurement to CSV")
        self.exportCsvButton.setFixedSize(28, 26)
        self.exportCsvButton.clicked.connect(self.exportRunCsv)
        sublayout.addWidget(self.exportCsvButton)

        sublayout.addStretch()

        sublayout.addWidget(qtw.QLabel("Auto-plot"))
        sublayout.addWidget(self.autoPlotBox)

        sublayout.addSpacing(12)
        sublayout.addWidget(qtw.QLabel("Refresh:"))
        sublayout.addWidget(self.spinBox)
        sublayout.addWidget(qtw.QLabel("s"))

        self.l.addLayout(self.targetLayout)
        self.l.addLayout(sublayout)
        
        # Long QTreeWidget/list to display all runs with small detail
        self.RunList = RunList()
        self.RunList.selected.connect(self.updateSelected)
        self.RunList.plot.connect(self.openPlot)
        
        # Show all available info on the selected item in self.RunList
        self.infoBox = moreInfo(preview_size=self.preview_size)
        self.infoBox.preview.plotRequested.connect(self.open_preview_plot)
        if self.fileTextbox.text() and self.RunList.topLevelItemCount():
            self.infoBox.preview.set_database_runs(
                self.fileTextbox.text(),
                self.RunList.all_run_metadata()
                )

        self.runInfoSplitter = qtw.QSplitter(QtCore.Qt.Vertical)
        self.runInfoSplitter.setHandleWidth(8)
        self.runInfoSplitter.setChildrenCollapsible(True)
        self.runInfoSplitter.setOpaqueResize(True)
        self.runInfoSplitter.addWidget(self.RunList)
        self.runInfoSplitter.addWidget(self.infoBox)
        self.runInfoSplitter.setCollapsible(0, False)
        self.runInfoSplitter.setCollapsible(1, True)
        self.runInfoSplitter.setStretchFactor(0, 3)
        self.runInfoSplitter.setStretchFactor(1, 2)
        self.runInfoSplitter.setSizes([380, self._details_pane_height()])
        self.runInfoSplitter.handle(1).setToolTip(
            "Drag to resize the run list and details panes"
            )
        self.l.addWidget(self.runInfoSplitter, 1)


    def initShortcuts(self):
        """
        Register keyboard shortcuts for context menu and common run actions.

        """
        plot_entered = qtw.QAction("Plot Entered Run and Measurement", self)
        plot_entered.setShortcut("Ctrl+Return")
        plot_entered.setShortcutContext(QtCore.Qt.WindowShortcut)
        plot_entered.setStatusTip("Plot the run and measurement entered above")
        plot_entered.triggered.connect(lambda _: self.plotRunButton.click())
        self.addAction(plot_entered)

        plot_selected_all = qtw.QAction("Plot All Measurements in Selected Run", self)
        plot_selected_all.setShortcut("Ctrl+Shift+Return")
        plot_selected_all.setShortcutContext(QtCore.Qt.WindowShortcut)
        plot_selected_all.setStatusTip("Plot all measurements in the selected run")
        plot_selected_all.triggered.connect(self.open_selected_run_all)
        self.addAction(plot_selected_all)

        self.open_param_actions = []
        for itr in range(9):
            action = qtw.QAction(f"Plot Measurement {itr + 1} in Selected Run", self)
            action.setShortcut(f"Ctrl+{itr + 1}")
            action.setShortcutContext(QtCore.Qt.WindowShortcut)
            action.setStatusTip(f"Plot measurement {itr + 1} in the selected run")
            action.triggered.connect(lambda _, index=itr: self.open_param_by_index(index))
            self.addAction(action)
            self.open_param_actions.append(action)
        
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Event handler for closing Main Window.

        Also handles some closing admin        

        """
        # Confirm exit
        if self.config.get("user_preference.confirm_close"):
            reply = qtw.QMessageBox.question(self, "Confirm Exit", "Are you sure you want to exit?",
                                         qtw.QMessageBox.Yes | qtw.QMessageBox.No)
            if reply == qtw.QMessageBox.Yes:
                event.accept()
            else:
                event.ignore()
                return

        self.monitor.stop()
        qtw.QApplication.closeAllWindows()
    
   
    @QtCore.pyqtSlot()
    def closeAll(self):
        """
        Event handler for close all menu button.
        Closes all windows other than the main window.

        """
        plot_windows = self.windows.copy()
        if not plot_windows:
            self.show_status("No plot windows to close.", 3000)
            return

        if close_all_warning_enabled(self.config):
            count = len(plot_windows)
            noun = "window" if count == 1 else "windows"
            reply = qtw.QMessageBox.question(
                self,
                "Close All Plot Windows",
                f"Close {count} plot {noun}?",
                qtw.QMessageBox.Yes | qtw.QMessageBox.No,
                qtw.QMessageBox.No,
                )
            if reply != qtw.QMessageBox.Yes:
                self.show_status("Close all plot windows cancelled.", 3000)
                return

        self.show_status("Closing plot windows...", 3000)
        for win in plot_windows:
            win.close()
        
        
    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        """
        Event handler for closing a Plot window

        Parameters
        ----------
        win : qplot.windows.plotWin.plotWidget
            The window that is closing.


        """
        self.windows.remove(win)
        self.remove_ds_at(win._guid)
        self.post_admin() # Update other plot windows
        self.show_status(f"Closed {win.label}", 3000)
        del win
    
    
    @QtCore.pyqtSlot(object, str, tuple)
    def openWin(self, widget, guid_or_ds, *args, show=True, **kargs):
        """
        Handles opening Plot window, widget.
        Passes all attributes to widget(). Also passes other critical objects.

        Connected to plot2d for openning it's secondary plots.        

        Parameters
        ----------
        widget : qplot.windows.plotWin.plotWidget
            Takes window class to be openned.
        *args 
            Passed to widget.__init__().
        show : bool, optional
            Whether the windows is dsiplayed to the user or held as a 
            background process. Is also passed to widget.__init__(). 
            The default is True.
        **kargs
            Passed to widget.__init__().


        """
        # Convert args to usable form if passed as iterable
        if len(args) == 1 and (isinstance(args[0], tuple) or isinstance(args[0], list)):
            args = tuple(args[0])
        
        # Find if guid or ds was passed
        if isinstance(guid_or_ds, str):
            ds = None
            guid = guid_or_ds
        else: 
            ds = guid_or_ds
            guid = ds.guid
            
        # add dataset to store
        self.add_ds_at(guid, ds=ds)
        
        win = widget(
            guid,
            *args, 
            self.config, 
            self.threadPool,
            self.dataset_holder,
            show=show, 
            **kargs
            )
        
        # Store copy in Main Window to prevent python auto delete
        self.windows.append(win)
        
        # Slot connectons
        win.closed.connect(self.onClose)
        win.make_ds.connect(self.add_ds_at)
        if win.__class__.__name__ == "plot1d":
            win.get_mergables.connect(lambda: self.get_1d_wins(win))
            win.remove_dataset.connect(self.remove_ds_at)
            
        elif win.__class__.__name__ == "plot2d":
            win.open_subplot.connect(self.openWin)
            
        elif win.__class__.__name__ == "sweeper":
            # find win's parent
            for item in self.windows:
                if item.ds == win.ds and item.param == win.param and isinstance(item, plot2d):
                    win.sweep_moved.connect(item.update_sweep_line) # Update event
                    win.remove_sweep.connect(item.remove_sweep) # Close event
                    item.sweep_moved.connect(win.update_sweep_line)
                    break
            
        else:
            raise TypeError(f"Unknown window of type: {win.__class__.__name__}")

        # Place window on screen so it doesnt overlap with last openned
        if show:    
            # match style/theme to main window
            win.update_theme(self.config)
            
            win.move(self.x, self.y)
            win.show()
        
            #set next position
            tolerance = 30
            self.x += win.width
            if self.x + win.width - tolerance > self.screenrect.right():
                self.x = self.screenrect.left()
                self.y += win.height
                
                if self.y + win.height - tolerance > self.screenrect.bottom():
                    self.y = self.screenrect.top()
        
###############################################################################
#Slots
    
    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        """
        Updates the refresh interval for checking for new runs in database

        Parameters
        ----------
        interval : flaot
            Refresh interval to be set, in seconds.

        """
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000)) #convert to seconds


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
                database_path
                )
            return

        opened = QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
        if opened:
            self.show_status(f"Opened database folder: {folder}", 5000)
        else:
            self.show_error(
                "Open Folder Failed",
                "The database folder could not be opened.",
                folder
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


    @QtCore.pyqtSlot()
    def refreshMain(self):
        """
        On self.monitor timer or force refresh, check for new runs in Database        

        """
        if not self.fileTextbox.text(): # If no selected database
            self.show_status("Load a database before refreshing.", 5000)
            return

        self.show_status("Checking for new runs...", 0)

        try:
            # Find any runs after the last highest time
            newRuns = find_new_runs(self.RunList.maxTime)

            # Check runs markes as "Ongoing" to see if they have finished
            self.RunList.checkWatching()
        except Exception as err:
            self.show_error("Refresh Failed", "Could not refresh the run list.", str(err))
            return
        
        if not newRuns: # Nothing found
            self.show_status("No new runs found.", 3000)
            return
        
        # Convert to numpy array to handle Nan/null values which occur in rare cases
        self.RunList.maxTime = max(
            np.array([subDict["run_timestamp"] for subDict in newRuns.values()], dtype=float),
            default=0
            )
        self.RunList.addRuns(newRuns)
        self.infoBox.preview.add_runs(newRuns)
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
        Opens file directory dialog for use to select file and loads that 
        database

        """
        # Fetch user selected load location from config.json
        if os.path.isdir(self.config.get("file.default_load_path")):
            openDir = self.config.get("file.default_load_path")
        else: # Otherwise use console directory
            openDir = os.getcwd()
        
        filename = qtw.QFileDialog.getOpenFileName(
            self, 
            'Open file', # Dialog button display
            openDir, # Default look location
            "Data Base File (*.db)" # What to show
            )[0] # Returns array even if only 1 item is selected
        
        # Confirm user did not cancel
        if os.path.isfile(filename):
            self.load_database_path(filename)
        else:
            self.show_status("Database load cancelled.", 3000)
            
    
    @QtCore.pyqtSlot()
    def change_default_file(self):
        """
        Event handle for for Open Location action in options menu.
        Changes default open location in config.json for usage in
        self.getfile()


        """
        # Open at last default load location
        if os.path.isdir(self.config.get("file.default_load_path")):
            openDir = self.config.get("file.default_load_path")
        else:
            openDir = os.getcwd()
        
        foldername = qtw.QFileDialog.getExistingDirectory(
            self, 
            'Select Folder', # Dialog button display
            openDir, # Default look location
            )
        
        # Confirm user did not cancel
        if os.path.isdir(foldername):
            self.config.update("file.default_load_path", foldername)
            self.show_status(f"Default load folder set to {foldername}", 5000)
        else:
            self.show_status("Default load folder unchanged.", 3000)
              
            
    @QtCore.pyqtSlot()
    def loadLastFile(self):
        """
        Event handler for load last action in file menu.
        Loads last openned file in application or file location stored in
        config.json if no other files have been openned.

        """
        if not self.localLastFile:
            last_file = self.config.get("file.last_file_path")
        else:
            last_file = os.path.abspath(self.localLastFile)
        
        if os.path.isfile(last_file):
            self.load_database_path(last_file)
        else:
            self.show_error(
                "Load Last Failed",
                "The last database file could not be found.",
                str(last_file)
                )


    @QtCore.pyqtSlot(str)
    def load_database_path(self, filename):
        """
        Load a database path chosen from the file dialog or dropped by the user.

        """
        load_started_at = perf_counter()

        if not os.path.isfile(filename):
            self.show_error(
                "Database Load Failed",
                "The selected database file could not be found.",
                str(filename)
                )
            return False

        abspath = os.path.abspath(filename)
        if not abspath.lower().endswith(".db"):
            self.show_error(
                "Database Load Failed",
                "qPlot can only load QCoDeS .db database files.",
                abspath
                )
            return False

        if self.load_file(abspath, load_started_at):
            self.config.update("file.last_file_path", abspath)
            self.remember_recent_database(abspath)
            self.loadLastAction.setEnabled(True)
            return True

        return False


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

        self.config.config.setdefault("file", {})["recent_file_paths"] = paths
        self.config.save_config(self.config.default_file)
        self.refresh_recent_database_menu()


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
            action.triggered.connect(lambda _, filename=path: self.load_database_path(filename))
            self.recentDatabaseMenu.addAction(action)
    
    
    @QtCore.pyqtSlot(str)
    def updateSelected(self, guid):
        """
        Event Handler for clicking on RunList.
        Loads the selected run into memory using the row's guid.
        It then displays metadata and other available info into the InfoList.

        Parameters
        ----------
        guid : str
            The unique id to load the dataset from.

        """
        self.show_status("Loading selected run...", 0)
        try:
            # Load from store if possible
            if self.dataset_holder.get(guid, 0) == 0:
                self.ds = load_by_guid(guid)
            else:
                self.ds = self.dataset_holder[guid]["dataset"]
        except Exception as err:
            self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
            return
        
        self.selected_run_id = self.ds.run_id
        self.run_idBox.blockSignals(True)
        self.run_idBox.setText(str(self.ds.run_id))
        self.run_idBox.blockSignals(False)
        
        # Get metadata (snapshot) from dataset
        if hasattr(self.ds, "snapshot"):
            snap = self.ds.snapshot
        else:
            snap = None
        
        paramspec = self.ds.get_parameters()
        # Create dict to convert into a QTreeWidget for display
        structure = {"Data points" : self.ds.number_of_results}
        # Unpack parameter metadata
        for param in paramspec:
            if len(param.depends_on) > 0:
                structure[param.name] = {"unit" : param.unit,
                                         "label" : param.label,
                                         "axes" : list(param.depends_on_)
                                         }
            else:
                structure[param.name] = {"unit" : param.unit,
                                         "label" : param.label
                                         }
        info = {"Data Structure" : structure,
                "MetaData" : self.ds.metadata,
                "Snapshot" : snap
                }
        # Update infoBox
        self.infoBox.setInfo(info, self.ds)
        self.show_status(
            f"Selected run {self.ds.run_id} with {self.ds.number_of_results:,} points.",
            5000
            )
        
        
    @QtCore.pyqtSlot()
    def openRun(self):
        """
        Event handler for the plot button.
        Plots the requested measurement for the requested run.

        Required in specific cases for error catching.

        """
        ds = self._dataset_for_plot_target()
        if ds is None:
            return

        params = self._selected_measurement_params(ds)
        if params is None:
            return

        self.ds = ds
        self.openPlot(params=params)


    @QtCore.pyqtSlot()
    def open_selected_run_all(self):
        """
        Opens every plottable measurement in the currently selected table row.

        """
        if self.ds is None:
            self.show_status("Select a run before plotting all measurements.", 5000)
            return

        self.openPlot()


    @QtCore.pyqtSlot()
    def exportRunCsv(self):
        """
        Exports the requested run and measurement data to a CSV file.

        """
        ds = self._dataset_for_plot_target()
        if ds is None:
            return

        params = self._selected_measurement_params(ds)
        if params is None:
            return
        if not params:
            self.show_status("No plottable measurements to export for this run.", 5000)
            return

        default_name = self._default_export_filename(ds, params)
        filename = qtw.QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            default_name,
            "CSV files (*.csv)"
            )[0]
        if not filename:
            self.show_status("CSV export cancelled.", 3000)
            return
        if not filename.lower().endswith(".csv"):
            filename = f"{filename}.csv"

        try:
            frame = self._measurement_dataframe(ds, params)
            frame.to_csv(filename, index=False)
        except Exception as err:
            self.show_error(
                "CSV Export Failed",
                "Could not export the selected measurement data.",
                str(err)
                )
            return

        self.show_status(f"Exported CSV: {filename}", 5000)
    
    
    @QtCore.pyqtSlot(str)
    def openPlot(self, 
                 guid : str=None, 
                 params : list=None, 
                 show : bool=True
                 ):
        """
        Event handler for:
            Plot button,
            RunList double click
            RunList context menu actions
        Takes the currently selected run and passes to Open Win to produce 
        new Plot windows.
        
        Parameters
        ----------
        guid : str, optional
            If given, overrides the currently selected dataset and loads a new
            one with the given unique run code, GUID.
        params : list[qcodes.dataset.descriptions.param_spec.ParamSpec], optional
            The parameters in the dataset to be openned. Primarally used in
            RunList context menu actions.
            The default is None, which opens all dependant parameters in the 
            dataset.
        show : bool
            Whether to display the window to the user. The default is True.
        
        """
        if not self.ds and not guid:
            self.show_status("Select a run before opening plots.", 5000)
            return

        self.show_status("Opening plots...", 0)

        try:
            # Get dataset with GUID or default
            if not self.ds or (guid and self.ds.guid != guid):
                # Load from store if possible
                if self.dataset_holder.get(guid, 0) == 0:
                    ds = load_by_guid(guid)
                else:
                    ds = self.dataset_holder[guid]["dataset"]
            else:
                ds = self.ds
        except Exception as err:
            self.show_error("Run Load Failed", "Could not load the selected run.", str(err))
            return
            
        if not params:
            params = ds.get_parameters()
           
        opened = 0
        skipped = 0
        try:
            for param in params:
                if param.depends_on != "":
                    depends_on = param.depends_on_
                    skip = False
                    
                    if len(depends_on) == 1:
                        for win in self.windows:
                            # Check if window is open
                            if win.ds == ds and win.param == param and isinstance(win, plot1d):
                                skipped += 1
                                skip = True
                                break
                        if skip: continue
                        
                        self.openWin(
                            plot1d, 
                            ds, 
                            param, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                        opened += 1
                        
                    else:
                        for win in self.windows:
                            # Check if window is open
                            if (win.ds == ds and win.param == param and isinstance(win, plot2d)):
                                skipped += 1
                                skip = True
                                break
                        if skip: continue
                            
                        self.openWin(
                            plot2d, 
                            ds, 
                            param, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                        opened += 1
                        
            self.post_admin() # Updates currently open windows

            if opened:
                noun = "plot" if opened == 1 else "plots"
                self.show_status(f"Opened {opened} {noun}.", 5000)
            elif skipped:
                self.show_status("Selected plot windows are already open.", 5000)
            else:
                self.show_status("No plottable parameters found for this run.", 5000)
            
        except Exception as err:
            # atempt to prevent SQL lock outs
            try:
                ds.conn.close()
            except Exception:
                pass
            self.show_error("Plot Open Failed", "Could not open plot windows.", str(err))


    def open_param_by_index(self, index : int):
        """
        Open the indexed dependent parameter for the selected run.

        """
        if not self.ds:
            self.show_status("Select a run before opening a parameter.", 5000)
            return

        params = [param for param in self.ds.get_parameters() if param.depends_on != ""]
        if index >= len(params):
            self.show_status(f"Run has no parameter {index + 1}.", 5000)
            return

        self.openPlot(params=[params[index]])


    @QtCore.pyqtSlot(str)
    def open_preview_plot(self, parameter_name):
        """
        Open the plot represented by a double-clicked preview image.

        """
        if not self.ds:
            self.show_status("Select a run before opening a preview plot.", 5000)
            return

        for param in self.ds.get_parameters():
            if param.name == parameter_name and param.depends_on != "":
                self.openPlot(params=[param])
                return

        self.show_status(f"No preview plot found for {parameter_name}.", 5000)


    def _selected_measurement_params(self, dataset):
        """
        Returns the measurement parameters requested by the Measurement field.

        """
        params = [param for param in dataset.get_parameters() if param.depends_on != ""]
        measurement = self.measurementBox.text().strip()

        if measurement in ("", "*"):
            return params

        try:
            index = int(measurement)
        except ValueError:
            self.show_status("Measurement must be a number or *.", 5000)
            return None

        if index < 1 or index > len(params):
            self.show_status(
                f"Run {dataset.run_id} has no measurement {index}.",
                5000
                )
            return None

        return [params[index - 1]]


    def _dataset_for_plot_target(self):
        """
        Loads the dataset requested by the Run field.

        """
        if not self.fileTextbox.text():
            self.show_status("Load a database before plotting or exporting.", 5000)
            return None

        if self.selected_run_id is None:
            self.show_status("Enter a Run ID before plotting or exporting.", 5000)
            return None

        try:
            return load_by_id(self.selected_run_id)
        except Exception as error:
            self.show_error(
                "Run Load Failed",
                f"Could not load Run ID {self.selected_run_id}.",
                str(error)
                )
            return None


    def _measurement_dataframe(self, dataset, params):
        """
        Builds a flat CSV-friendly dataframe for the selected measurement data.

        """
        frames = []
        prefix_columns = len(params) > 1
        for param in params:
            param_data = dataset.get_parameter_data(param.name).get(param.name, {})
            columns = {}
            for name, values in param_data.items():
                column_name = f"{param.name}.{name}" if prefix_columns else name
                columns[column_name] = pd.Series(np.asarray(values).ravel())
            frames.append(pd.DataFrame(columns))

        return pd.concat(frames, axis=1) if frames else pd.DataFrame()


    def _default_export_filename(self, dataset, params):
        """
        Returns a default CSV export path.

        """
        database_folder = os.path.dirname(self.fileTextbox.text())
        measurement = "all" if len(params) != 1 else params[0].name
        filename = self._safe_filename(f"run_{dataset.run_id}_{measurement}.csv")
        return os.path.join(database_folder or os.getcwd(), filename)


    def _safe_filename(self, filename):
        """
        Replaces path-hostile characters in a suggested filename.

        """
        return "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)
    
    
    @QtCore.pyqtSlot(str)
    def update_run_id(self, text):
        """
        Updates the Run ID target entered into the Run text box.

        Parameters
        ----------
        text : str/int
            Run ID number to be plotted.

        """
        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.blockSignals(False)
        self.ds = None
        self.infoBox.clear()

        try:
            self.selected_run_id = int(text)
        except ValueError:
            self.selected_run_id = None
            return


    @QtCore.pyqtSlot()
    def sync_run_id_selection(self):
        """
        Selects the typed Run ID in the table if it is currently visible.

        """
        if self.selected_run_id is None:
            return

        matches = self.RunList.findItems(
            str(self.selected_run_id),
            QtCore.Qt.MatchExactly,
            0
            )
        if not matches:
            return

        item = matches[0]
        self.RunList.setCurrentItem(item)
        self.RunList.scrollToItem(item, qtw.QAbstractItemView.PositionAtCenter)
        
        
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

        self.config.reset_to_defaults()
        self.apply_current_settings()
        self.show_status("Default settings restored.", 5000)


    def apply_current_settings(self):
        """
        Applies config-backed settings that can be updated in open windows.

        """
        self._sync_theme_actions()
        self._sync_preview_size_actions()
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


    def _configured_preview_size(self):
        try:
            return int(self.config.get("GUI.preview_size"))
        except (KeyError, TypeError, ValueError):
            return PREVIEW_SIZE


    def _save_preview_size(self, preview_size):
        gui_config = self.config.config.setdefault("GUI", {})
        if "preview_size" not in gui_config:
            gui_config["preview_size"] = self.preview_size
        self.config.update("GUI.preview_size", int(preview_size))


    def _details_pane_height(self):
        return max(260, int(self.preview_size) + 84)

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
        self.show_status(message, 10000)

        box = qtw.QMessageBox(qtw.QMessageBox.Warning, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec_()


    @QtCore.pyqtSlot(str)
    def add_ds_at(self, guid : str, ds = None):
        """
        Uses the guid of a dataset to update self.dataset_holder with a new 
        tracker of a dataset
        
        If the dataset is already stored, increases the tracker of the number
        of windows that use that dataset (users).
        If the dataset is not stored, load a new dataset with that guid

        Parameters
        ----------
        guid : str
            guid of the dataset being added.
        ds : TYPE, optional
            An already loaded dataset with a guid. 
            This may be from using that dataset elsewhere in the app and is 
            passed to prevent loading again.

        """
        # dataset does not exist
        if self.dataset_holder.get(guid, 0) == 0:
            # load ds unless ds is already provided
            ds = load_by_guid(guid) if ds is None else ds
            assert ds.guid == guid
            
            self.dataset_holder[guid] = {
                "dataset" : ds,
                "users" : 1,
                "del_timer" : None
                }
        # increment users and stop deletion timer if needed
        else:
            self.dataset_holder[guid]["users"] += 1
            if self.dataset_holder[guid]["del_timer"] is not None:
                self.dataset_holder[guid]["del_timer"].stop() # Stop delete timer
                self.dataset_holder[guid]["del_timer"] = None
            
    
    @QtCore.pyqtSlot(str)
    def remove_ds_at(self, guid : str):
        """
        Decreases the count of users for a dataset.
        If dataset has no more users, begin timer to delete object if unused.
        If dataset gets a new user, timer is stopped.
        Timer length can be found in config file.

        Parameters
        ----------
        guid : str
            guid of the dataset being removed.

        """
        # Check dataset is available to be removed
        if self.dataset_holder.get(guid, 0) == 0:
            self.show_status("Trying to remove dataset that does not exist.", 5000)
            return
        
        # Track removal
        self.dataset_holder[guid]["users"] -= 1
        
        # Check for no windows using
        if self.dataset_holder[guid]["users"] <= 0:
            del_time = self.config.get("runtime_settings.del_grace_period")
            
            # Remove now if no grace period
            if del_time == 0: # Remove now if no grace period
                self.dataset_holder.pop(guid)
                
            # Set up removal timer, remove after del_time seconds
            elif self.dataset_holder[guid]["del_timer"] is None:
                del_timer = QtCore.QTimer()
                del_timer.setSingleShot(True)
                self.dataset_holder[guid]["del_timer"] = del_timer
                # Link timer to delete
                del_timer.timeout.connect(lambda guid=guid:
                    self.dataset_holder.pop(guid)
                    )
                    
                del_timer.start(int(del_time*1000)) # convert to seconds
        

    def load_file(self, abspath, load_started_at = None):
        """
        Updates the database for RunList display and loading datasets.
        Used by self.loadLastFile() and self.getFile()

        Parameters
        ----------
        abspath : str
            Path to database.

        """
        if load_started_at is None:
            load_started_at = perf_counter()
        
        if abspath == get_DB_location(): # Already initialised in QCoDeS
            if not self.infoBox.preview.has_database(abspath):
                self.infoBox.preview.set_database_runs(
                    abspath,
                    self.RunList.all_run_metadata()
                    )
            elapsed = perf_counter() - load_started_at
            self.show_status(f"Database is already loaded ({elapsed:.2f} s).", 3000)
            return True

        previous_file = self.fileTextbox.text()
        monitorTimer = self.spinBox.value()
        self.show_status(f"Loading database {os.path.basename(abspath)}...", 0)

        # Pause refresh while working
        self.monitor.stop()

        try:
            # Clear widgets from last Database
            self.run_idBox.setText("")
            self.measurementBox.setText("*")

            self.RunList.clearSelection()
            self.RunList.watching = []
            self.RunList.scrollToTop()

            self.infoBox.clear()
            self.infoBox.scrollToTop()

            # Update internal last file location using self.fileTextbox text
            if self.fileTextbox.text() and self.fileTextbox.text() != self.localLastFile:
                self.localLastFile = self.fileTextbox.text()
                self.loadLastAction.setEnabled(True)

            # Update dsiplay and set database location within QCoDeS
            self.fileTextbox.setText(abspath)

            initialise_or_create_database_at(abspath)

            runs = self.RunList.setRuns()
            self.infoBox.preview.set_database_runs(abspath, runs)
            self.select_default_run()

        except Exception as err:
            self.fileTextbox.setText(previous_file)
            self.show_error(
                "Database Load Failed",
                f"Could not load database {abspath}.",
                str(err)
                )
            if monitorTimer > 0:
                self.monitor.start(int(monitorTimer * 1000))
            return False

        # Restart refresh
        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))
        elapsed = perf_counter() - load_started_at
        self.show_status(
            (
                f"Loaded {self.RunList.topLevelItemCount()} runs from "
                f"{os.path.basename(abspath)} in {elapsed:.2f} s."
                ),
            5000
            )
        return True


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
            
    
    def post_admin(self):
        """
        Updates the Plot windows internal track of other open windows.

        """
        for item in self.windows:
            if isinstance(item, plot1d):
                self.get_1d_wins(item)
                
    
    def get_1d_wins(self, win):
        """
        Finds compatable Plot windows for adding secondary plot to for win.

        Parameters
        ----------
        win : qplot.windows.plotWin.plotWidget
            The window which is being refreshed.

        """
        wins = []
        
        for item in self.windows:
            # Find compatible windows
            try:
                if item.param.depends_on == win.param.depends_on:
                    if not item.label in win.lines.keys():
                        wins.append(item)
                        
                elif (item.__class__.__name__ == "sweeper" 
                      and 
                      item.axis_options["x"] == win.param.depends_on):
                    if not item.label in win.lines.keys():
                        wins.append(item)
                        
                    
            except AttributeError: # If not initisiased properly
                continue
        
        # Update within win
        win.update_line_picker(wins)
