from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore
from PyQt5.QtGui import QIntValidator

from . import (
    plot1d,
    plot2d,
    RunList,
    moreInfo,
    )
from qplot.datahandling import (
    dataset,
    find_new_runs
    )
from qplot.configuration import config

from qcodes.dataset import (
    initialise_or_create_database_at,
    load_by_guid,
    load_by_id
    )
from qcodes.dataset.sqlite.database import get_DB_location

import os


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
        
        #widgets
        self.l = qtw.QVBoxLayout()
        
        self.initRefresh()
        self.initAutoplot()
        self.initMenu()
        self.initFile()
        self.initRunDisplay()
        
        
        #Final Setup
        w = qtw.QWidget()
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
        
        loadLastAction = qtw.QAction("&Load Last")
        # loadLastAction.setShortcut("Ctrl+Shift+L")
        loadLastAction.triggered.connect(self.loadLastFile)
        fileMenu.addAction(loadLastAction)
        if not self.config.get("file.last_file_path"):
            loadLastAction.setDisabled(True)
        
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)
        
        
    def initFile(self):
        self.l.addWidget(qtw.QLabel("File Directory:"))
        
        self.fileTextbox = qtw.QLineEdit()
        self.fileTextbox.setReadOnly(True)
        self.fileTextbox.setStyleSheet("""
            QLineEdit {
                color: #4a4a4a;    
                background-color: #eaeaea;
                border: 1px solid #cccccc
            }
        """)
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
        
        self.infoBox = moreInfo()
        self.l.addWidget(self.infoBox)
        
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        if self.monitor.isActive():
            self.monitor.stop()
        
        qtw.QApplication.closeAllWindows()
        
        
    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        self.windows.remove(win)
        del win
    
    
    def openWin(self, widget, *args, **kargs):
        win = widget(*args, **kargs)
        
        self.windows.append(win)
        win.closed.connect(self.onClose)
        
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
        


    def openPlot(self, guid : str=None):
        if guid:
            ds = load_by_guid(guid)
        else:
            ds = self.ds
            
        for param in ds.get_parameters():
            if param.depends_on != "":
                depends_on = param.depends_on_
                if len(depends_on) == 1:
                    self.openWin(plot1d, ds, param, refrate = self.spinBox.value())
                elif len(depends_on) == 2:
                    self.openWin(plot2d, ds, param, refrate = self.spinBox.value())
                else:
                    raise IndexError(
                        f"Parameter: {param.name}, depends on too many variables ({depends_on}, {len(depends_on)=})"
                       )
        
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
        
        if not newRuns:
            return
        
        self.listWidget.maxTime = max([subDict["run_timestamp"] for subDict in newRuns.values()])
        self.listWidget.addRuns(newRuns)

        if self.autoPlotBox.checkState():
            for run in newRuns.values():
                self.openPlot(run["guid"])
        else:
            print("unticked")
                

    @QtCore.pyqtSlot()
    def getfile(self):
        filename = qtw.QFileDialog.getOpenFileName(
            self, 
            'Open file', 
            os.getcwd(),
            "Data Base File (*.db)"
            )[0]
        
        if os.path.isfile(filename):
            
            abspath = os.path.abspath(filename)
            
            self.load_file(abspath)
            
            self.config.update("file.last_file_path", abspath)
              
            
    @QtCore.pyqtSlot()
    def loadLastFile(self):
        last_file = self.config.get("file.last_file_path")
        
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
        try:
            assert self.ds is not None
            self.openPlot()
        except AssertionError:
            pass

        
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
    
###############################################################################
#Other funcs

    def load_file(self, abspath):
        
        self.listWidget.clearSelection()
        self.infoBox.clear()
        
        self.monitor.stop()
        self.fileTextbox.setText(abspath)
        
        
        if abspath != get_DB_location():
            initialise_or_create_database_at(abspath)
            
        self.listWidget.setRuns()
        
        monitorTimer = self.spinBox.value()
        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))
    
###############################################################################        
#Depreicated

    def openDataset(self):
        run_id = int(self.pltTextbox.text())
        ds = dataset.init_and_load_by_spec(
            self.fileTextbox.text(),
            captured_run_id=run_id
            )
        return ds
