# -*- coding: utf-8 -*-
"""
Created on Sun Jul  6 16:35:38 2025

@author: Benjamin Wordsworth
"""
import pyqtgraph as pg
from PyQt5 import QtWidgets as qtw

import qcodes

import numpy as np

from qplot.tools import unpack_param

class plot1d(qtw.QWidget):
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec
                 ):
        super().__init__()
        
        # layout = qtw.QVBoxLayout()
        self.widget = pg.GraphicsLayoutWidget(parent=self)
        
        
        # self.setLayout(layout)
        # layout.addWidget(widget)
        
        # self.setCentralWidget(layout)
        self.setWindowTitle(str(dataset.run_id))
        
        
        indepPara = unpack_param(dataset, param.depends_on)
        
        
        df = dataset.to_pandas_dataframe(param)
        depvarData = df.iloc[:,0].to_numpy(float)
        
        if isinstance(df.index.names, type(None)) or len(df.index.names) == 1:
            indepvarData = df.index.to_numpy(float)
        else: 
            name_ind = df.index.names.index(indepPara.name)
            unpacked_index = np.array(
                [*df.index], #unpack tupple to produce nd array
                dtype=float
                )
            indepvarData = unpacked_index[:,name_ind]
            
        
        print(len(indepvarData))
        print(len(depvarData))
        
        plot = self.widget.addPlot(x=indepvarData, y=depvarData)
        plot.setLabel(axis="bottom", text=f"{indepPara.label} ({indepPara.unit})")
        plot.setLabel(axis="left", text=f"{param.label} (param.unit)")
        
        
        
        
        
        