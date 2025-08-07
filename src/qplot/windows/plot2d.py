from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

import pyqtgraph as pg

from qplot.tools import loader_2d as loader
from qplot.windows.plotWin import plotWidget

class plot2d(plotWidget):
    
    def __init__(self, 
                 *args,
                 **kargs,
                 ):
        super().__init__(*args, **kargs)

        
    def initFrame(self):
        if self.loader.df.empty:
            return
        
        self.image = pg.ImageItem()
        
        self.plot.addItem(self.image)
        
        # Wait for loader to finish to enure needed data is collected.
        self.wait_on_thread()
        
        self.bar = self.plot.addColorBar(
            self.image,
            colorMap="magma",
            label=f"{self.param.label} ({self.param.unit})",
            rounding=(max(self.depvarData) - min(self.depvarData))/1e5 #Add 10,000 colours
            )
        self.scaleColorbar()
        
        self.plot.setLabel('left', f"{self.axis_param['y'].label} ({self.axis_param['y'].unit})")
        self.plot.setLabel('bottom', f"{self.axis_param['x'].label} ({self.axis_param['x'].unit})")
    
        self.initalised = True
        print("graph produced \n")
      
        
    def initRefresh(self, refresh):
        self.loader = loader(self.thread, self.ds, self.param, self.param_dict)
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
        
        
    def initLabels(self):
        super().initLabels()
        
        self.pos_labels["y"].setText(self.pos_labels["y"].text() + ";")
        
        posLabelx = qtw.QLabel("z= ")
        self.toolbarCo_ord.addWidget(posLabelx)
        self.pos_labels["z"] = posLabelx
        
###############################################################################
    
    def refreshPlot(self):
        super().refreshPlot()
        
        self.dataGrid = self.loader.dataGrid
        
        self.image.setImage(
            self.dataGrid,
            autoLevels=bool(self.relevel_refresh.isChecked()),
            autoRange=bool(self.rescale_refresh.isChecked()) #currently redundant
            )
        
        #set axis values
        xmin = min(self.axis_data["x"])
        ymin = min(self.axis_data["y"])
        xrange = max(self.axis_data["x"]) - xmin
        yrange = max(self.axis_data["y"]) - ymin
        
        if xrange == 0:
            xrange = xmin / 100 
        if yrange == 0:
            yrange = ymin / 100 
        
        self.rect = pg.QtCore.QRectF(
            xmin,
            ymin, 
            xrange, 
            yrange
        )
        self.image.setRect(self.rect)

###############################################################################    
        
    @QtCore.pyqtSlot(bool)
    def scaleColorbar(self, event = None):
        vmin, vmax = min(self.depvarData), max(self.depvarData)

        self.bar.setLevels((vmin, vmax))
        