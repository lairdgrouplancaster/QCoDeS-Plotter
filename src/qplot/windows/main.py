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
    
    def __init__(self):
        super().__init__()
       
        #vars
        self.windows = [] #prevent auto delete of windows
        self.ds = None
        self.monitor = QtCore.QTimer()
        self.x = 0
        self.y = 0
        self.config = config()
        self.localLastFile = None
        
        self.setStyleSheet(self.config.theme.main)
        
        #widgets
        self.l = qtw.QVBoxLayout()
        
        self.initRefresh()
        self.initAutoplot()
        self.initMenu()
        self.initFile()
        self.initRunDisplay()
        
        
        #Final Setup
        w = qtw.QFrame()
        w.setLayout(self.l)
        self.setCentralWidget(w)
       
        
        self.resize(*self.config.get("GUI.main_frame_size"))
        self.setWindowTitle("qPlot")
        
        self.screenrect = qtw.QApplication.primaryScreen().availableGeometry()
        self.x = self.screenrect.left() #control new window position
        self.y = self.screenrect.top()
        
        #bring window to top
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint) 
        self.show()


    def initRefresh(self):
        self.toolbar = self.addToolBar("Refresh Timer")
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        
        self.toolbar.addWidget(qtw.QLabel("Refresh interval (s): "))
        self.toolbar.addWidget(self.spinBox)
    
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshMain)
    
    
    def initAutoplot(self):
        self.toolbar.addSeparator()
        
        self.toolbar.addWidget(qtw.QLabel("Toggle Auto-plot "))
        
        self.autoPlotBox = qtw.QCheckBox()
        self.toolbar.addWidget(self.autoPlotBox)
    
    
    def initMenu(self):
        menu = self.menuBar()
        fileMenu = menu.addMenu("&File")
        
        loadAction = qtw.QAction("&Load", self)
        loadAction.setShortcut("Ctrl+L")
        loadAction.triggered.connect(self.getfile)
        fileMenu.addAction(loadAction)
        
        self.loadLastAction = qtw.QAction("&Load Last", self)
        self.loadLastAction.setShortcut("Ctrl+Shift+L")
        self.loadLastAction.triggered.connect(self.loadLastFile)
        fileMenu.addAction(self.loadLastAction)
        if not self.config.get("file.last_file_path"):
            self.loadLastAction.setDisabled(True)
        
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)
        
        prefMenu = menu.addMenu("&Options")
        
        default_load_picker = qtw.QAction("&Open Location", self)
        default_load_picker.triggered.connect(self.change_default_file)
        prefMenu.addAction(default_load_picker)
        
        themeMenu = prefMenu.addMenu("&Theme")
        
        current_theme = self.config.get("user_preference.theme")
        self.themes = []
        for itr, theme in enumerate(["Light", "Dark", "PyQt"]):
            self.themes.append(qtw.QAction(f'&{theme}', self, checkable=True))
            self.themes[itr].triggered.connect(lambda _, theme=theme.lower(), action=self.themes[itr]:
                                               self.change_theme(theme, action))
            themeMenu.addAction(self.themes[itr])
            if theme.lower() == current_theme:
                self.themes[itr].setChecked(True)
        
        
    def initFile(self):
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
        self.run_idBox = qtw.QLineEdit()
        self.run_idBox.setMaximumWidth(50)
        self.run_idBox.setValidator(QIntValidator(1, 9999999, self))
        self.run_idBox.textEdited.connect(self.update_run_id)
        sublayout.addWidget(self.run_idBox)
        
        sublayout.addStretch()

        pltbutton = qtw.QPushButton("Open Plots")
        pltbutton.setFixedWidth(200)
        pltbutton.clicked.connect(self.openRun)
        sublayout.addWidget(pltbutton)
        self.l.addLayout(sublayout)
        
        self.listWidget = RunList()
        self.l.addWidget(self.listWidget)
        self.listWidget.selected.connect(self.updateSelected)
        self.listWidget.plot.connect(self.openPlot)
        
        self.infoBox = moreInfo()
        self.l.addWidget(self.infoBox)
        
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        self.monitor.stop()
        qtw.QApplication.closeAllWindows()
        
        
    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        self.windows.remove(win)
        self.post_admin()
        del win
    
    
    def openWin(self, widget, *args, show=True, **kargs):
        win = widget(*args, show=True, **kargs)
        
        self.windows.append(win)
        win.closed.connect(self.onClose)

        win.update_theme(self.config)
        
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
#Signals 
    
    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000)) #convert to seconds


    @QtCore.pyqtSlot()
    def refreshMain(self):
        if not self.fileTextbox.text():
            return
        
        newRuns = find_new_runs(self.listWidget.maxTime)
        
        self.listWidget.checkWatching()
        
        if not newRuns:
            return
        
        self.listWidget.maxTime = max(
            np.array([subDict["run_timestamp"] for subDict in newRuns.values()], dtype=float),
            default=0
            )
        self.listWidget.addRuns(newRuns)


        if self.autoPlotBox.isChecked():
            for run in newRuns.values():
                # print(run["guid"])
                self.openPlot(run["guid"])

                
    @QtCore.pyqtSlot()
    def change_default_file(self):
        if os.path.isdir(self.config.get("file.default_load_path")):
            openDir = self.config.get("file.default_load_path")
        else:
            openDir = os.getcwd()
        
        foldername = qtw.QFileDialog.getExistingDirectory(
            self, 
            'Select Folder', 
            openDir,
            )
        
        if os.path.isdir(foldername):
            self.config.update("file.default_load_path", foldername)

    @QtCore.pyqtSlot()
    def getfile(self):
        if os.path.isdir(self.config.get("file.default_load_path")):
            openDir = self.config.get("file.default_load_path")
        else:
            openDir = os.getcwd()
        
        filename = qtw.QFileDialog.getOpenFileName(
            self, 
            'Open file', 
            openDir,
            "Data Base File (*.db)"
            )[0]
        
        if os.path.isfile(filename):
            
            abspath = os.path.abspath(filename)
            
            self.load_file(abspath)
            
            self.config.update("file.last_file_path", abspath)
              
            
    @QtCore.pyqtSlot()
    def loadLastFile(self):
        if not self.localLastFile:
            last_file = self.config.get("file.last_file_path")
        else:
            last_file = os.path.abspath(self.localLastFile)
        
        if os.path.isfile(last_file):
            self.load_file(last_file)
        
        
    @QtCore.pyqtSlot()
    def openRun(self):
        if self.selected_run_id and self.fileTextbox.text():
            try:
                ds = load_by_id(self.selected_run_id)
            except NameError as error:
                print(type(error), error)
                return
            self.ds = ds
        
        if self.ds:
            self.openPlot()
    
    
    @QtCore.pyqtSlot(str)
    def openPlot(self, guid : str=None, params : list=None, show=True):
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
                            self.config, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                    else:
                        self.openWin(
                            plot2d, 
                            ds, 
                            param, 
                            self.config, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
            self.post_admin() #
        except Exception as err:
            # atempt to prevent SQL locks
            ds.conn.close()
            raise err
        
        
    @QtCore.pyqtSlot(str)
    def updateSelected(self, guid):
        self.ds = load_by_guid(guid)
        
        self.selected_run_id = None
        self.run_idBox.setText(str(self.ds.run_id))
        
        if hasattr(self.ds, "snapshot"):
            snap = self.ds.snapshot
        else:
            snap = None
        
        paramspec = self.ds.get_parameters()
        structure = {"Data points" : self.ds.number_of_results}
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
        self.infoBox.setInfo(info)
    
    
    @QtCore.pyqtSlot(str)
    def update_run_id(self, text):
        try:
            self.selected_run_id = int(text)
        except ValueError:
            self.selected_run_id = None
            return
        
        
    def change_theme(self, theme, action):
        if self.config.get("user_preference.theme") == theme:
            action.setChecked(True)
            return
        for QActions in self.themes:
            if QActions != action:
                QActions.setChecked(False)
                
        self.config.update("user_preference.theme", theme)
        
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)

###############################################################################
#Other funcs

    def load_file(self, abspath):
        
        if abspath == get_DB_location():
            return
        
        self.monitor.stop()
        
        self.run_idBox.setText("")
        
        self.listWidget.clearSelection()
        self.listWidget.watching = []
        self.listWidget.scrollToTop()
        
        self.infoBox.clear()
        self.infoBox.scrollToTop()
        
        if self.fileTextbox.text() and self.fileTextbox.text() != self.localLastFile:
            self.localLastFile = self.fileTextbox.text()
            self.loadLastAction.setEnabled(True)
        
        self.fileTextbox.setText(abspath)
        
        initialise_or_create_database_at(abspath)
            
        self.listWidget.setRuns()
        
        monitorTimer = self.spinBox.value()
        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))
            
    
    def post_admin(self):
        
        for item in self.windows:
            if isinstance(item, plot1d):
                self.get_1d_wins(item)
                
            else:
                #do 2d admin
                pass
    
    def get_1d_wins(self, win):
        
        wins = []
        
        for item in self.windows:
            if item.param.depends_on == win.param.depends_on and not item.label in win.lines.keys():
                wins.append(item)
        
        win.update_line_picker(wins)
        
