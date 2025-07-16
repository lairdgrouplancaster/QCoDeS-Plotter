# -*- coding: utf-8 -*-
"""
Created on Tue Jul  8 12:50:50 2025

@author: Benjamin Wordsworth
"""
from math import log10

import pyqtgraph as pg

import numpy as np

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore 

import qcodes


class plotWidget(qtw.QMainWindow):
    closed = QtCore.pyqtSignal([object])
    runEnd = QtCore.pyqtSignal(str)
    
    #Core methods
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec,
                 refrate : float=None
                 ):
        super().__init__()
        
        self.ds = dataset
        self.name = str(self.ds.run_id)
        self.param = param
        self.monitor = QtCore.QTimer()
        self.initalised = False
        self.ds.cache.load_data_from_db()
        
        self.loadDSdata()
        
        self.layout = qtw.QVBoxLayout(self)
        
        self.widget = pg.GraphicsLayoutWidget()
        self.plot = self.widget.addPlot()
        self.layout.addWidget(self.widget)
        
        
        w = qtw.QWidget()
        w.setLayout(self.layout)
        self.setCentralWidget(w)
        
        self.setWindowTitle(str(self))
        self.resize(960, 540)
        
        # self.show()
        
        
    def __str__(self):
        return self.name
    
    #Other Methods
    
    def loadDSdata(self):
        # self.ds.cache.data()
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
        
        self.toolbarRef = self.addToolBar("Refresh Timer")
        
        
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
        self.toolbarCo_ord = self.addToolBar("Co-ordinates")
        
        self.posLabelx = qtw.QLabel(text="x=       ")
        self.toolbarCo_ord.addWidget(self.posLabelx)
        
        self.posLabely = qtw.QLabel(text="y=       ")
        self.toolbarCo_ord.addWidget(self.posLabely)
        
        self.toolbarCo_ord.addWidget(qtw.QLabel("  "))
        
        self.plot.scene().sigMouseMoved.connect(self.mouseMoved)
    
        
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
        
    #Events
    
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
        if not self.ds.running:
            self.monitor.stop()
            self.runEnd.emit(self.ds.guid)
            return
        last_df_len = len(self.depvarData)
        self.loadDSdata()
        
        if len(self.depvarData) != last_df_len:
            self.refreshPlot()
        