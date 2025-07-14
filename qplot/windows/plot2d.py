# -*- coding: utf-8 -*-
"""
Created on Tue Jul  8 09:41:16 2025

@author: Benjamin Wordsworth
"""
import qcodes
import numpy as np

from PyQt5 import QtWidgets as qtw
import pyqtgraph as pg

from qplot.tools import unpack_param, data2matrix
from .setup import plotWidget

class plot2d(plotWidget):
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec
                 ):
        super().__init__(dataset, param, str(dataset.run_id))
        
        print("Working")
        
        
        indepNames = param.depends_on.split(", ")
        indepParams = [unpack_param(dataset, name) for name in indepNames]
        
        indepData = []
        unpacked_index = np.array(
            [*self.df.index], #unpack tupple to produce nd array
            dtype=float
            )
        for indpara in indepParams:
            name_ind = self.df.index.names.index(indpara.name)
            indepData.append(unpacked_index[:,name_ind])
            
        
        dataGrid = data2matrix(
            indepData[1].copy(), 
            indepData[0].copy(), 
            self.depvarData
        )
        
        # plot = self.widget.addPlot()
        image = pg.ImageItem(dataGrid.to_numpy(float))
        
        #set axis values
        image.setRect(
            pg.QtCore.QRectF(
                indepData[1].copy()[0], 
                indepData[0].copy()[0], 
                indepData[1].copy()[-1] - indepData[1].copy()[0], 
                indepData[0].copy()[-1] - indepData[0].copy()[0]
            ))
        
        
        self.plot.addItem(image)
        self.plot.addColorBar(
            image,
            colorMap="magma",
            label=f"{param.label} ({param.unit})",
            rounding=(max(self.depvarData) - min(self.depvarData))/1e5 #Add 10,000 colours
            )
        
        
        self.plot.setLabel('left', f"{indepParams[0].label} ({indepParams[0].unit})")
        self.plot.setLabel('bottom', f"{indepParams[1].label} ({indepParams[1].unit})")
        
        print("graph produced \n")
        