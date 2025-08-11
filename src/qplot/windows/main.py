from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore
from PyQt5.QtGui import QIntValidator

from qplot.windows import (
    plot1d,
    plot2d,
    )
from qplot.windows._widgets import (
    RunList,
    moreInfo,
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

import numpy as np


class MainWindow(qtw.QMainWindow):
    """
    The Main application which connects/initialises QCoDeS database, displays
    available options plots to open, and opens windows.
    
    This window can be opened by calling qplot.run()
    
    Holds a shallow copy of all other open windows to prevent deletion by 
    python's garbarge collector
    """
    
    def __init__(self):
        super().__init__()
       
        #vars
        self.windows = [] #prevent auto delete of windows
        self.ds = None
        self.monitor = QtCore.QTimer()
        self.threadPool = QtCore.QThreadPool()
        self.x = 0
        self.y = 0
        self.config = config() # Connect to config.json in /users/<user>/.qplot/
        self.localLastFile = None
        
        # Set GUI color and style from user choice in qplot.configuration.themes
        self.setStyleSheet(self.config.theme.main)
        
        #widgets
        self.l = qtw.QVBoxLayout()
        
        #Core initialisation functions
        self.initRefresh()
        self.initAutoplot()
        self.initMenu()
        self.initFile()
        self.initRunDisplay()
        
        
        #Final Setup
        w = qtw.QFrame()
        w.setLayout(self.l)
        self.setCentralWidget(w)
       
        # Fetch window size from config.json
        self.resize(*self.config.get("GUI.main_frame_size"))
        self.setWindowTitle("qPlot")
        
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
        self.toolbar = self.addToolBar("Refresh Timer")
        
        # Widget production
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        
        self.toolbar.addWidget(qtw.QLabel("Refresh interval (s): "))
        self.toolbar.addWidget(self.spinBox)
    
        # Slot connections
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshMain)
    
    
    def initAutoplot(self):
        """
        Produces tick box for whether to automatically open newly found plots
        
        """
        self.toolbar.addSeparator() # Toolbar produced in self.initRefresh()
        
        self.toolbar.addWidget(qtw.QLabel("Toggle Auto-plot "))
        
        self.autoPlotBox = qtw.QCheckBox()
        self.toolbar.addWidget(self.autoPlotBox)
    
    
    def initMenu(self):
        """
        Produces the menu bar and all menu's contained at the top of the window

        """
        menu = self.menuBar()
        # First dropdown menu
        fileMenu = menu.addMenu("&File") # Not sure why these all have &, but they do
        
        # Load database file
        loadAction = qtw.QAction("&Load", self)
        loadAction.setShortcut("Ctrl+L")
        loadAction.triggered.connect(self.getfile)
        fileMenu.addAction(loadAction)
        
        # Load accessed database
        self.loadLastAction = qtw.QAction("&Load Last", self)
        self.loadLastAction.setShortcut("Ctrl+Shift+L")
        self.loadLastAction.triggered.connect(self.loadLastFile)
        fileMenu.addAction(self.loadLastAction)
        if not self.config.get("file.last_file_path"): # has user openned a DB before?
            self.loadLastAction.setDisabled(True)
        
        # Force update check on database
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)
        
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
        
        
    def initFile(self):
        """
        Display text box for current selected database
        
        """
        self.l.addWidget(qtw.QLabel("File Directory:"))
        
        self.fileTextbox = qtw.QLineEdit()
        self.fileTextbox.setDisabled(True)
        self.l.addWidget(self.fileTextbox)
        
        if os.path.isfile(get_DB_location()):
            self.fileTextbox.setText(str(get_DB_location()))
        
        
    def initRunDisplay(self):
        sublayout = qtw.QHBoxLayout()
        
        sublayout.addWidget(qtw.QLabel("Run id:"))
        
        self.selected_run_id = None
        
        # Box for User to enter specific run_id
        self.run_idBox = qtw.QLineEdit()
        self.run_idBox.setMaximumWidth(50)
        # Only allow int in box between 1 and 9999999
        self.run_idBox.setValidator(QIntValidator(1, 9999999, self))
        self.run_idBox.textEdited.connect(self.update_run_id)
        sublayout.addWidget(self.run_idBox)
        
        sublayout.addStretch() # Force widgets on either side to the edge

        # Opens all plots at run_id in self.run_idBox
        pltbutton = qtw.QPushButton("Open Plots")
        pltbutton.setFixedWidth(200)
        pltbutton.clicked.connect(self.openRun)
        sublayout.addWidget(pltbutton)
        self.l.addLayout(sublayout)
        
        # Long QTreeWidget/list to display all runs with small detail
        self.RunList = RunList()
        self.l.addWidget(self.RunList)
        self.RunList.selected.connect(self.updateSelected)
        self.RunList.plot.connect(self.openPlot)
        
        # Show all available info on the selected item in self.RunList
        self.infoBox = moreInfo()
        self.l.addWidget(self.infoBox)
        
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Event handler for closing Main Window.

        Also handles some closing admin        

        """
        self.monitor.stop()
        qtw.QApplication.closeAllWindows()
        # Add self.ds.conn.close()?
        
        
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
        self.post_admin() # Update other plot windows
        del win
    
    
    def openWin(self, widget, *args, show=True, **kargs):
        """
        Handles opening Plot window, widget.
        Passes all attributes to widget(). Also passes other critical objects.

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
        win = widget(
            *args, 
            self.config, 
            self.threadPool, 
            show=show, 
            **kargs
            )
        
        # Store copy in Main Window to prevent python auto delete
        self.windows.append(win)
        
        # Slot connectons
        win.closed.connect(self.onClose)
        if hasattr(win, "get_mergables"): #get_mergables only in 1d
            win.get_mergables.connect(lambda: self.get_1d_wins(win))

        # match style/theme to main window
        win.update_theme(self.config)
        
        # Place window on screen so it doesnt overlap with last openned
        if show:
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
    def refreshMain(self):
        """
        On self.monitor timer or force refresh, check for new runs in Database        

        """
        if not self.fileTextbox.text(): # If no selected database
            return
        
        # Find any runs after the last highest time
        newRuns = find_new_runs(self.RunList.maxTime)
        
        # Check runs markes as "Ongoing" to see if they have finished
        self.RunList.checkWatching() 
        
        if not newRuns: # Nothing found
            return
        
        # Convert to numpy array to handle Nan/null values which occur in rare cases
        self.RunList.maxTime = max(
            np.array([subDict["run_timestamp"] for subDict in newRuns.values()], dtype=float),
            default=0
            )
        self.RunList.addRuns(newRuns)


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
            
            # Convert to python friendly str
            abspath = os.path.abspath(filename)
            
            self.load_file(abspath)
            
            self.config.update("file.last_file_path", abspath)
            
    
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
            self.load_file(last_file)
    
    
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
        self.ds = load_by_guid(guid)
        
        self.selected_run_id = None # Prevents reloading the dataset through run_idbox
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
        self.infoBox.setInfo(info)
        
        
    @QtCore.pyqtSlot()
    def openRun(self):
        """
        Event handler for Open Plots button.
        Confirms that a run dataset is loaded into memory then passes to 
        self.openPlot

        Required in specific cases for error catching.

        """
        if self.selected_run_id and self.fileTextbox.text():
            try:
                ds = load_by_id(self.selected_run_id)
            except NameError as error:
                print(type(error), error)
                return
            self.ds = ds
        
        # Last check that a dataset is loaded
        if self.ds:
            self.openPlot()
    
    
    @QtCore.pyqtSlot(str)
    def openPlot(self, 
                 guid : str=None, 
                 params : list=None, 
                 show : bool=True
                 ):
        """
        Event handler for:
            Open Plots button,
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
        # Get dataset with GUID or default
        if not self.ds:
            ds = load_by_guid(guid)
        elif guid and self.ds.guid != guid:
            ds = load_by_guid(guid)
        else:
            ds = self.ds
            
        if not params:
            params = ds.get_parameters()
           
        try:
            for param in params:
                if param.depends_on != "":
                    depends_on = param.depends_on_
                    if len(depends_on) == 1:
                        self.openWin(
                            plot1d, 
                            ds, 
                            param, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                        
                    else:
                        self.openWin(
                            plot2d, 
                            ds, 
                            param, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                        
            self.post_admin() # Updates currently open windows
            
        except Exception as err:
            # atempt to prevent SQL lock outs
            ds.conn.close()
            raise err
    
    
    @QtCore.pyqtSlot(str)
    def update_run_id(self, text):
        """
        Updates the select run id which is entered into the run id: text box.
        Note that self.selected_run_id is set to None on RunList selection

        Parameters
        ----------
        text : str/int
            Run id number to be openned.

        """
        try:
            self.selected_run_id = int(text)
        except ValueError:
            self.selected_run_id = None
            return
        
        
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

###############################################################################
#Other funcs

    def load_file(self, abspath):
        """
        Updates the database for RunList display and loading datasets.
        Used by self.loadLastFile() and self.getFile()

        Parameters
        ----------
        abspath : str
            Path to database.

        """
        
        if abspath == get_DB_location(): # Already initialised in QCoDeS
            return
        
        # Pause refresh while working
        self.monitor.stop()
        
        # Clear widgets from last Database
        self.run_idBox.setText("")
        
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
            
        self.RunList.setRuns()
        
        # Restart refresh
        monitorTimer = self.spinBox.value()
        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))
            
    
    def post_admin(self):
        """
        Updates the Plot windows internal track of other open windows.

        """
        
        for item in self.windows:
            if isinstance(item, plot1d):
                self.get_1d_wins(item)
                
            else:
                # to do 2d admin
                pass
    
    
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
            if item.param.depends_on == win.param.depends_on and not item.label in win.lines.keys():
                wins.append(item)
        
        # Update within win
        win.update_line_picker(wins)
