from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

import pyqtgraph as pg

from qplot.tools import data2matrix
from .plotWin import plotWidget

class plot2d(plotWidget):
    
    def __init__(self, 
                 *args,
                 **kargs,
                 ):
        super().__init__(*args, **kargs)

        
    def initFrame(self):
        if self.df.empty:
            return
        
        self.image = pg.ImageItem()
        
        self.refreshPlot()
        
        self.plot.addItem(self.image)
        
        self.bar = self.plot.addColorBar(
            self.image,
            colorMap="magma",
            label=f"{self.param.label} ({self.param.unit})",
            rounding=(max(self.depvarData) - min(self.depvarData))/1e5 #Add 10,000 colours
            )
        
        self.plot.setLabel('left', f"{self.yaxis_param.label} ({self.yaxis_param.unit})")
        self.plot.setLabel('bottom', f"{self.xaxis_param.label} ({self.xaxis_param.unit})")
    
        self.scaleColorbar()
        
        self.initalised = True
        print("graph produced \n")
      
    def initRefresh(self, refresh):
        super().initRefresh(refresh)
        
        self.toolbarRef.addWidget(qtw.QLabel("| "))
        self.toolbarRef.addWidget(qtw.QLabel("Re-Map Colors "))
        
        self.relevel_refresh = qtw.QCheckBox()
        self.toolbarRef.addWidget(self.relevel_refresh)
     
    
    def initContextMenu(self):
        super().initContextMenu()

        autoColor = qtw.QAction("Autoscale Color", self)
        autoColor.triggered.connect(self.scaleColorbar)
        self.vbMenu.insertAction(self.autoscaleSep, autoColor)
        
    
    def refreshPlot(self):
        dataGrid = data2matrix(
            self.xaxis_data.copy(), 
            self.yaxis_data.copy(), 
            self.depvarData
        )
        
        self.image.setImage(
            dataGrid.to_numpy(float),
            autoLevels=bool(self.relevel_refresh.isChecked()),
            autoRange=bool(self.rescale_refresh.isChecked())
            )
        
        #set axis values
        xmin = min(self.xaxis_data)
        ymin = min(self.yaxis_data)
        xrange = max(self.xaxis_data) - xmin
        yrange = max(self.yaxis_data) - ymin
        
        if xrange == 0:
            xrange = xmin / 100 
        if yrange == 0:
            yrange = ymin / 100 
        
        self.image.setRect(
            pg.QtCore.QRectF(
                xmin,
                ymin, 
                xrange, 
                yrange
            ))
        
        
    @QtCore.pyqtSlot(bool)
    def scaleColorbar(self, event = None):
        vmin, vmax = min(self.depvarData), max(self.depvarData)

        self.bar.setLevels((vmin, vmax))
        