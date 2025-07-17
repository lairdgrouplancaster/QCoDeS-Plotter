from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

import pyqtgraph as pg

from qplot.tools import unpack_param, data2matrix
from .plotWin import plotWidget

class plot2d(plotWidget):
    
    def __init__(self, 
                 *args,
                 refrate = None,
                 **kargs,
                 ):
        super().__init__(*args, **kargs)
        
        self.initFrame()
        self.initRefresh(refrate)
        
        
    def initFrame(self):
        if self.df.empty:
            return
        
        print("Working")
        self.image = pg.ImageItem()
        
        indepNames = self.param.depends_on_
        self.indepParams = [unpack_param(self.ds, name) for name in indepNames]
        
        
        dataGrid = data2matrix(
            self.indepData[1].copy(),
            self.indepData[0].copy(), 
            self.depvarData
        )
        
        self.image.setImage(dataGrid.to_numpy(float), autoLevels=True, autoRange=True)
        
        #set axis values
        idepData_xmin = min(self.indepData[1])
        idepData_ymin = min(self.indepData[0])
        self.image.setRect(
            pg.QtCore.QRectF(
                idepData_xmin,
                idepData_ymin, 
                max(self.indepData[1]) - idepData_xmin, 
                max(self.indepData[0]) - idepData_ymin
            ))
        
        
        self.plot.addItem(self.image)
        
        self.bar = self.plot.addColorBar(
            self.image,
            colorMap="magma",
            label=f"{self.param.label} ({self.param.unit})",
            rounding=(max(self.depvarData) - min(self.depvarData))/1e5 #Add 10,000 colours
            )
        
        self.plot.setLabel('left', f"{self.indepParams[0].label} ({self.indepParams[0].unit})")
        self.plot.setLabel('bottom', f"{self.indepParams[1].label} ({self.indepParams[1].unit})")
        
        self.initContextMenu()
        
        self.initalised = True
        print("graph produced \n")
        
        self.initLabels()
        
        
    def refreshPlot(self):
        dataGrid = data2matrix(
            self.indepData[1].copy(), 
            self.indepData[0].copy(), 
            self.depvarData
        )
        
        self.image.setImage(
            dataGrid.to_numpy(float),
            autoLevels=False,
            autoRange=False
            )
        
        #set axis values
        idepData_xmin = min(self.indepData[1])
        idepData_ymin = min(self.indepData[0])
        self.image.setRect(
            pg.QtCore.QRectF(
                idepData_xmin,
                idepData_ymin, 
                max(self.indepData[1]) - idepData_xmin, 
                max(self.indepData[0]) - idepData_ymin
            ))
        
    
    def initContextMenu(self):
        super().initContextMenu()

        autoColor = qtw.QAction("Autoscale Color", self)
        autoColor.triggered.connect(self.scaleColorbar)
        self.vbMenu.insertAction(self.autoscaleSep, autoColor)
        
        
    @QtCore.pyqtSlot(bool)
    def scaleColorbar(self, event):
        vmin, vmax = min(self.depvarData), max(self.depvarData)

        self.bar.setLevels((vmin, vmax))
        
        