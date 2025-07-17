from math import log10

import pyqtgraph as pg

import numpy as np

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore 

import qcodes
from qcodes.dataset.sqlite.database import get_DB_location


class plotWidget(qtw.QMainWindow):
    closed = QtCore.pyqtSignal([object])
    
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec,
                 refrate : float=None
                 ):
        super().__init__()
        
        self.ds = dataset
        self.param = param
        self.name = str(self)
        self.monitor = QtCore.QTimer()
        self.initalised = False
        self.ds.cache.load_data_from_db()
        
        self.loadDSdata()
        
        self.layout = qtw.QVBoxLayout()
        
        self.widget = pg.GraphicsLayoutWidget()
        self.plot = self.widget.addPlot()
        self.layout.addWidget(self.widget)
        
        
        
        self.setWindowTitle(str(self))
        
        screenrect = qtw.QApplication.primaryScreen().availableGeometry()
        sizeFrac = 0.47
        self.width = int(sizeFrac * screenrect.width())
        self.height = int(sizeFrac * screenrect.height())
        self.resize(self.width, self.height)
        
        w = qtw.QWidget()
        w.setLayout(self.layout)
        self.setCentralWidget(w)
        
        
    def __str__(self):
        filenameStr = get_DB_location().split('\\')[-1]
        fstr = (f"{filenameStr} | " 
                f"run ID: {self.ds.run_id} | "
                f"{self.param.name} ({self.param.label})"
                )
        return fstr
 
###############################################################################
    #Other Methods
    
    def loadDSdata(self):
        
        self.df = self.ds.cache.to_pandas_dataframe().loc[:, self.param.name:self.param.name]
        self.depvarData = self.df.iloc[:,0].to_numpy(float)
        
        #get non np.nan values
        valid_rows = ~np.isnan(self.depvarData)
        indepData = self.df.index.to_frame()
        
        valid_data = []
        for itr in range(len(indepData.columns)):
            valid_data.append(indepData.iloc[:,itr].loc[valid_rows].to_numpy(float))
        
        self.indepData = valid_data
        self.depvarData = self.depvarData[valid_rows]
        
    
    def initRefresh(self, refrate : float):
        if not self.ds.running:
            return
        
        self.toolbarRef = qtw.QToolBar("Refresh Timer")
        self.addToolBar(QtCore.Qt.TopToolBarArea, self.toolbarRef)
        
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)


        self.toolbarRef.addWidget(qtw.QLabel("Refresh interval (s): "))
        self.toolbarRef.addWidget(self.spinBox)
    
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshWindow)
        
        if refrate > 0:
            self.monitor.start(int(refrate * 1000))
            self.spinBox.setValue(refrate)
        else:
            self.monitor.start(5000)
            self.spinBox.setValue(5.0)
        
        
    def initLabels(self):
        self.toolbarCo_ord = qtw.QToolBar("Co-ordinates")
        self.addToolBar(QtCore.Qt.BottomToolBarArea, self.toolbarCo_ord)
        
        self.posLabelx = qtw.QLabel(text="x=       ")
        self.toolbarCo_ord.addWidget(self.posLabelx)
        
        self.posLabely = qtw.QLabel(text="y=       ")
        self.toolbarCo_ord.addWidget(self.posLabely)
        
        self.toolbarCo_ord.addWidget(qtw.QLabel("  "))
        
        self.plot.scene().sigMouseMoved.connect(self.mouseMoved)
    
    
    def initContextMenu(self):
        vb = self.plot.getViewBox()
        self.vbMenu = vb.menu
        
        actions = []
        for action in self.vbMenu.actions():
            actions.append(action)
            if action.text() == "View All":
                action.setText("Autoscale")
        
        self.autoscaleSep = self.vbMenu.insertSeparator(actions[1])
    
    
    @staticmethod
    def formatNum(num : float, sf : int=3) -> str:
        try:
            log = int(log10(abs(num)))
        except ValueError:
            return f"{0:.{sf}f}"
        
        if log >= sf or log < 0:
            return f"{num:.{sf}e}"
        else:
            return f"{num:.{sf - log}f}"
      
###############################################################################
    #Events
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        if self.monitor:
            self.monitor.stop()
        self.closed.emit(self) 
        del self


    @QtCore.pyqtSlot(object)
    def mouseMoved(self, pos):
        
        if self.plot.sceneBoundingRect().contains(pos):
            mousePoint = self.plot.vb.mapSceneToView(pos)

            self.posLabelx.setText(f"x = {self.formatNum(mousePoint.x())}   ")
            self.posLabely.setText(f"y = {self.formatNum(mousePoint.y())}   ")
            
            
    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000)) #convert to seconds
            
            
    @QtCore.pyqtSlot()
    def refreshWindow(self):
        self.last_df_len = len(self.depvarData)
        self.loadDSdata()
        
        if not self.initalised:
            self.initFrame() #defined in children classes
            return
        
        if not self.ds.running:
            self.monitor.stop()
        
        
        if len(self.depvarData) != self.last_df_len:
            self.refreshPlot()