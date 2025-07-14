# -*- coding: utf-8 -*-
"""
Created on Tue Jul  8 12:50:50 2025

@author: Benjamin Wordsworth
"""
from math import log10

import pyqtgraph as pg

from PyQt5 import QtWidgets as qtw
from PyQt5.QtCore import pyqtSignal

import qcodes



class plotWidget(qtw.QWidget):
    sig = pyqtSignal([object])
    
    #Core methods
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec,
                 name : str):
        super().__init__()
        
        self.name = name
        
        self.setWindowTitle(str(self))
        self.resize(960, 540)
        
        self.layout = qtw.QVBoxLayout(self)
        
        self.widget = pg.GraphicsLayoutWidget()
        self.plot = self.widget.addPlot()
        self.layout.addWidget(self.widget)
        
        self.initLabels()
        
        self.plot.scene().sigMouseMoved.connect(self.mouseMoved)
        
        self.df = dataset.to_pandas_dataframe(param)
        self.depvarData = self.df.iloc[:,0].to_numpy(float)
        
        # self.show()
        
        
    def __str__(self):
        return self.name
    
    #Other Methods
    def initLabels(self):
        labelLayout = qtw.QVBoxLayout()
        labelLayout.setSpacing(0)
        
        
        self.layout.addLayout(labelLayout)
        
        self.posLabelx = qtw.QLabel(text=f"x= ")
        labelLayout.addWidget(self.posLabelx)
        
        self.posLabely = qtw.QLabel(text=f"y= ")
        labelLayout.addWidget(self.posLabely)
        
        # self.posLabelz = qtw.QLabel(text=f"")
        # labelLayout.addWidget(self.posLabelz)
    
        
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
        self.sig.emit(self)

    def mouseMoved(self, pos):
        
        if self.plot.sceneBoundingRect().contains(pos):
            mousePoint = self.plot.vb.mapSceneToView(pos)
            # mx = abs(np.ones(len(self.plotx))*mousePoint.x() - self.plotx)
            # mx = abs(np.ones(len(self.ploty))*mousePoint.x() - self.ploty)
            
            self.posLabelx.setText(f"x = {self.formatNum(mousePoint.x())}")
            self.posLabely.setText(f"y = {self.formatNum(mousePoint.y())}")
            
            # print(f"{mousePoint.x()=}, {mousePoint.y()=}")