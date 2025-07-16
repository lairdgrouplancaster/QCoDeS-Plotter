# -*- coding: utf-8 -*-
"""
Created on Sun Jul  6 16:35:38 2025

@author: Benjamin Wordsworth
"""
from PyQt5 import QtWidgets as qtw

import qcodes

import numpy as np

from qplot.tools import unpack_param

from .plotWin import plotWidget

class plot1d(plotWidget):
    def __init__(self, 
                 *args,
                 refrate = None,
                 **kargs
                 ):
        super().__init__(*args, **kargs)
        
        print("Working")
        
        indepParam = unpack_param(self.ds, self.param.depends_on)
        
        
        if isinstance(self.df.index.names, type(None)) or len(self.df.index.names) == 1:
            indepData = self.df.index.to_numpy(float)
        else: 
            name_ind = self.df.index.names.index(indepParam.name)
            unpacked_index = np.array(
                [*self.df.index], #unpack tupple to produce nd array
                dtype=float
                )
            indepData = unpacked_index[:,name_ind]
            
        
        
        # self.plot = self.widget.addPlot(x=indepData, y=self.depvarData)
        self.line = self.plot.plot()
        
        self.line.setData(x=indepData, y=self.depvarData)
        
        self.plot.setLabel(axis="bottom", text=f"{indepParam.label} ({indepParam.unit})")
        self.plot.setLabel(axis="left", text=f"{self.param.label} ({self.param.unit})")
        
        
        
        self.initRefresh(refrate)
        self.initLabels()
        
        self.initalised = True
        print("Graph produced \n")
        
        
    def refreshPlot(self):
        pass
        