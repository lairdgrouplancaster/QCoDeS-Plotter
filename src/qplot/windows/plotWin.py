from math import log10

import pyqtgraph as pg

import numpy as np

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore 

import qcodes
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.configuration import config
from qplot.tools import unpack_param

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
        
        self.initAxes()
        
        self.loadDSdata()
        
        self.layout = qtw.QVBoxLayout()
        
        self.widget = pg.GraphicsLayoutWidget()
        self.plot = self.widget.addPlot()
        self.layout.addWidget(self.widget)
        
        
        
        self.setWindowTitle(str(self))
        
        screenrect = qtw.QApplication.primaryScreen().availableGeometry()
        sizeFrac = config().get("GUI.plot_frame_fraction")

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
        depvarData = self.df.iloc[:,0].to_numpy(float)
        
        #get non np.nan values
        valid_rows = ~np.isnan(depvarData)
        indepData = self.df.index.to_frame()
        
        valid_data = []
        for itr in range(len(indepData.columns)):
            valid_data.append(indepData.iloc[:,itr].loc[valid_rows].to_numpy(float))
        
        self.indepData = valid_data
        self.depvarData = depvarData[valid_rows]
        
        for axis in ["x", "y"]:
            name = self.axis_dropdown[axis].currentText()
            param = self.param_dict[name]
            if not param.depends_on:
                data = self.indepData[indepData.columns.get_loc(name)]
            else:
                data = self.depvarData #silence error, is used in exec below
            
            #save to self.<x/y>axis respectively
            exec(f"self.{axis}axis_data = data")
            exec(f"self.{axis}axis_param = param")

    
    def initRefresh(self, refrate : float):
        if not self.ds.running:
            return
        
        toolbarRef = qtw.QToolBar("Refresh Timer")
        self.addToolBar(QtCore.Qt.TopToolBarArea, toolbarRef)
        
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)


        toolbarRef.addWidget(qtw.QLabel("Refresh interval (s): "))
        toolbarRef.addWidget(self.spinBox)
    
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshWindow)
        
        if refrate > 0:
            self.monitor.start(int(refrate * 1000)) #convert from ms to s
            self.spinBox.setValue(refrate)
        else:
            self.monitor.start(5000)
            self.spinBox.setValue(5.0)
        
        
    def initLabels(self):
        toolbarCo_ord = qtw.QToolBar("Co-ordinates")
        self.addToolBar(QtCore.Qt.BottomToolBarArea, toolbarCo_ord)
        
        self.posLabelx = qtw.QLabel(text="x=       ")
        toolbarCo_ord.addWidget(self.posLabelx)
        
        self.posLabely = qtw.QLabel(text="y=       ")
        toolbarCo_ord.addWidget(self.posLabely)
        
        toolbarCo_ord.addWidget(qtw.QLabel("  "))
        
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
        
        
    def initAxes(self):
        indep_params = self.param.depends_on_
        
        self.param_dict = {self.param.name: self.param}
        for param in indep_params:
            param_spec = unpack_param(self.ds, param)
            self.param_dict[param_spec.name] = param_spec
        
        toolbarAxes = qtw.QToolBar("Axes Control")
        
        self.addToolBar(QtCore.Qt.LeftToolBarArea, toolbarAxes)
        
        toolbar_l = qtw.QVBoxLayout()
        
        x_layout = qtw.QHBoxLayout()
        x_layout.addWidget(qtw.QLabel("x axis: "))
        x_dropdown = qtw.QComboBox()
        x_dropdown.addItems(indep_params)
        x_layout.addWidget(x_dropdown)
        toolbar_l.addLayout(x_layout)
        
        y_layout = qtw.QHBoxLayout()
        y_layout.addWidget(qtw.QLabel("y axis: "))
        y_dropdown = qtw.QComboBox()
        y_dropdown.addItems(indep_params)
        y_layout.addWidget(y_dropdown)
        toolbar_l.addLayout(y_layout)
        
        w = qtw.QWidget()
        w.setLayout(toolbar_l)
        toolbarAxes.addWidget(w)
        self.axis_dropdown = {"x": x_dropdown, "y": y_dropdown}
        
        if len(indep_params) == 1:
            self.axis_dropdown["y"].addItems([self.param.name])
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(indep_params[0])
                )
            self.axis_dropdown["y"].setCurrentIndex(
                self.axis_dropdown["y"].findText(self.param.name)
                )
        else:
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(indep_params[1])
                )
            self.axis_dropdown["y"].setCurrentIndex(
                self.axis_dropdown["y"].findText(indep_params[0])
                )
        
        self.axis_dropdown["x"].currentIndexChanged.connect(self.change_axis)
        self.axis_dropdown["y"].currentIndexChanged.connect(self.change_axis)
        
        
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
            
    
    @QtCore.pyqtSlot(int)
    def change_axis(self, index):
        
        
        
        self.refreshPlot()
        
        self.plot.setLabel(axis="bottom", text=f"{self.xaxis_param.label} ({self.xaxis_param.unit})")
        self.plot.setLabel(axis="left", text=f"{self.yaxis_param.label} ({self.yaxis_param.unit})")