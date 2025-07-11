from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

from . import (
    plot1d,
    plot2d,
    RunList,
    moreInfo,
    )
# from .treeWidgets import MainList
from qplot.datahandling import dataset

from qcodes.dataset import (
    initialise_or_create_database_at,
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
        
        
        self.l = qtw.QVBoxLayout()
        
        
        #Menu
        menu = self.menuBar()
        fileMenu = menu.addMenu("&File")
        
        loadAction = qtw.QAction("&Load", self)
        loadAction.setShortcut("Ctrl+L")
        loadAction.triggered.connect(self.getfile)
        fileMenu.addAction(loadAction)
        
        #File Picker
        self.l.addWidget(qtw.QLabel("File Directory:"))
        
        self.fileTextbox = qtw.QLineEdit("C:/Users/Benjamin Wordsworth/.qcodes/code/WN7C_first cooldown.db")
        self.fileTextbox.setReadOnly(True)
        self.l.addWidget(self.fileTextbox)
        
        #Run Display and plot
        sublayout = qtw.QHBoxLayout()
        
        sublayout.addWidget(qtw.QLabel("Run id:"))

        pltbutton = qtw.QPushButton("Open Plots")
        pltbutton.clicked.connect(self.openRuns)
        sublayout.addWidget(pltbutton)
        self.l.addLayout(sublayout)
        
        self.listWidget = RunList()
        self.l.addWidget(self.listWidget)
        self.listWidget.selected.connect(self.updateSelected)
        
        self.infoBox = moreInfo()
        self.l.addWidget(self.infoBox)
        
        #Final Setup
        w = qtw.QWidget()
        w.setLayout(self.l)
        self.setCentralWidget(w)
       
        self.resize(750, 600)
        
        #bring window to top
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint) 
        self.show()
        
###############################################################################
#Open/Close events

    def closeEvent(self, event):
        qtw.QApplication.closeAllWindows()
        
        
    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        self.windows.remove(win)
        del win
        # print(f"Closed {str(win)}, remaining: {self.windows}")
            
    
    def openWin(self, widget, *args, **kargs):
        win = widget(*args, **kargs)
        self.windows.append(win)
        win.sig.connect(self.onClose)
        win.show()


    
    def openPlot(self):
        # ds = self.openDataset()
        
        for param in self.ds.get_parameters():
            if param.depends_on != "":
                depends_on = param.depends_on_
                if len(depends_on) == 1:
                    self.openWin(plot1d, self.ds, param)
                elif len(depends_on) == 2:
                    self.openWin(plot2d, self.ds, param)
                else:
                    raise IndexError(
                        f"Parameter: {param.name}, depends on too many variables ({depends_on}, {len(depends_on)=})"
                       )
        
###############################################################################
#Button signals 

    @QtCore.pyqtSlot()
    def getfile(self):
        filename = qtw.QFileDialog.getOpenFileName(self, 
                                                   'Open file', 
                                                   os.getcwd(),
                                                   "Data Base File (*.db)"
                                                   )[0]
        self.fileTextbox.setText(filename)
        if os.path.isfile(get_DB_location()):
            initialise_or_create_database_at(os.path.abspath(filename))
          
        self.listWidget.refresh()
        
        
    @QtCore.pyqtSlot()
    def openRuns(self):
        assert self.ds is not None
        self.openPlot()

        
    @QtCore.pyqtSlot(list)
    def updateSelected(self, items):
        self.ds = load_by_id(items[0])
        
        if hasattr(self.ds, "snapshot"):
            snap = self.ds.snapshot
        else:
            snap = None
        
        paramspec = self.ds.get_parameters()
        structure = {}
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
