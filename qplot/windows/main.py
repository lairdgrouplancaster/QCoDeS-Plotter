from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

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

from qcodes.dataset import (
    initialise_or_create_database_at,
    # load_by_id,
    load_by_guid
    )
from qcodes.dataset.sqlite.database import get_DB_location

import os


class MainWindow(qtw.QMainWindow):
    
    
    def __init__(self):
        super().__init__()
       
        #vars
        self.windows = [] #prevent auto delete of windows
        self.ds = None
        self.monitorTimer = None
        
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
       
        self.resize(750, 700)
        self.setWindowTitle("qPlot")
        
        #bring window to top
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint) 
        self.show()



    def initRefresh(self):
        self.toolbar = self.addToolBar("Refresh Timer")
        
        self.monitor = QtCore.QTimer()
        
        # sublayout = qtw.QFormLayout()
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        # sublayout.addRow("Refresh interval (s)", self.spinBox)
    
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
        
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)
        
        
    def initFile(self):
        self.l.addWidget(qtw.QLabel("File Directory:"))
        
        
        self.fileTextbox = qtw.QLineEdit()
        self.fileTextbox.setReadOnly(True)
        self.l.addWidget(self.fileTextbox)
        if os.path.isfile(get_DB_location()):
            self.fileTextbox.setText(str(get_DB_location()))
        
        
    def initRunDisplay(self):
        sublayout = qtw.QHBoxLayout()
        
        sublayout.addWidget(qtw.QLabel("Run id:"))

        pltbutton = qtw.QPushButton("Open Plots")
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

    def closeEvent(self, event):
        if self.monitor.isActive():
            self.monitor.stop()
        
        qtw.QApplication.closeAllWindows()
        
        
    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        self.windows.remove(win)
        # del win
        # print(f"Closed {str(win)}, remaining: {self.windows}")
            
    
    def openWin(self, widget, *args, **kargs):
        win = widget(*args, **kargs)
        self.windows.append(win)
        win.closed.connect(self.onClose)
        win.show()


    
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
        # print("Monitoring")
        newRuns = find_new_runs(self.listWidget.maxTime)
        
        if not newRuns:
            return
        
        # print(newRuns)
        
        self.listWidget.maxTime = max([subDict["run_timestamp"] for subDict in newRuns.values()])
        self.listWidget.addRuns(newRuns)
        # print(f"New run found at {newRuns.keys()}")

        if self.autoPlotBox.checkState():
            for run in newRuns.values():
                print(run["guid"])
                self.openPlot(run["guid"])
        else:
            print("unticked")
                

    @QtCore.pyqtSlot()
    def getfile(self):
        filename = qtw.QFileDialog.getOpenFileName(self, 
                                                   'Open file', 
                                                   os.getcwd(),
                                                   "Data Base File (*.db)"
                                                   )[0]
        if os.path.isfile(filename):
            self.monitor.stop()
            self.fileTextbox.setText(filename)
            
            abspath = os.path.abspath(filename)
            if abspath != get_DB_location():
                initialise_or_create_database_at(abspath)
          
            self.listWidget.setRuns()
            
            monitorTimer = self.spinBox.value()
            if monitorTimer > 0:
                self.monitor.start(int(monitorTimer * 1000))
        
        
    @QtCore.pyqtSlot()
    def openRun(self):
        try:
            assert self.ds is not None
            self.openPlot()
        except AssertionError:
            pass

        
    @QtCore.pyqtSlot(str)
    def updateSelected(self, guid):
        self.ds = load_by_guid(guid)
        
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
    
###############################################################################        
#Depreicated

    def openDataset(self):
        run_id = int(self.pltTextbox.text())
        ds = dataset.init_and_load_by_spec(
            self.fileTextbox.text(),
            captured_run_id=run_id
            )
        return ds
