# -*- coding: utf-8 -*-
"""
Created on Tue Jul  8 12:50:50 2025

@author: Benjamin Wordsworth
"""
import pyqtgraph as pg
from PyQt5 import QtWidgets as qtw
from PyQt5.QtCore import pyqtSignal
import qcodes



class plotWidget(qtw.QWidget):
    sig = pyqtSignal([object])
    
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec,
                 name : str):
        super().__init__()
        
        self.name = name
        
        
        self.layout = qtw.QVBoxLayout(self)
        
        self.widget = pg.GraphicsLayoutWidget()
        self.layout.addWidget(self.widget)
        
        self.setWindowTitle(str(self))
        self.resize(960, 540)
        
        self.df = dataset.to_pandas_dataframe(param)
        self.depvarData = self.df.iloc[:,0].to_numpy(float)
        
        self.show()
        
        
    def __str__(self):
        return self.name
    
    def closeEvent(self, event):
        self.sig.emit(self)

def initSetup(win : qtw.QWidget, name : str):
    
    win.name = name
    win.__str__ = __str__
    
    win.layout = qtw.QHBoxLayout(win)
    
    win.widget = pg.GraphicsLayoutWidget()
    win.layout.addWidget(win.widget)
    
    win.setWindowTitle(win.name)
    win.resize(960, 540)
    
def __str__(self):
    return str(self.name)
    
    
    

def closeEvent(self, arr, *args, **kargs):
    super().closeEvent(*args, **kargs)
    
    arr.pop(self) #remove item from running list
    print(f"{arr=}")