import pyqtgraph as pg

from PyQt5 import QtCore
from PyQt5.QtGui import QColor

class subplot1d(pg.PlotDataItem):
    def __init__(self, parent, from_win, *args, **kargs):
        super().__init__(*args, **kargs)
        
        self.label = from_win.label
        self.param_dict = from_win.param_dict
        self.df = from_win.df
        self.running = from_win.ds.running
        
        self.parent = parent
        self.from_win = from_win
        
        #Create viewbox for line and add viewbox to main plot widget
        self.vb = pg.ViewBox()
        self.parent.plot.scene().addItem(self.vb)
        
        self.parent.plot.getAxis('right').linkToView(self.vb)
        self.vb.setXLink(self.parent.plot)
        
        self.updateViews()
        self.parent.vb.sigResized.connect(self.updateViews)
        
        self.refresh()
        
        self.side = "left"
        self.parent.plot.addItem(self)
            
            
    def refresh(self):
        
        parent = self.parent
        from_win = self.from_win
        
        self.running = from_win.ds.running
        
        data = {}
        
        
        if self.df.empty:
            data["x"] = []
            data["y"] = []
        
        else:
            indepDataNames = self.df.index.names

            for axis in ["x", "y"]:
                name = parent.axis_dropdown[axis].currentText()
                
                
                if self.param_dict.get(name, 0):
                    param = self.param_dict.get(name)
                else:
                    param = parent.param_dict.get(name)
                    
                if not param.depends_on:
                    data[axis] = from_win.valid_data[indepDataNames.index(name)]
                    
                else:
                    data[axis] = from_win.depvarData #ignore error, is used in exec below
                
        self.setData(
            x=data["x"], 
            y=data["y"],
            )
        # parent.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
        
    
    def updateViews(self):
        self.vb.setGeometry(self.parent.vb.sceneBoundingRect())
        self.vb.linkedViewChanged(self.parent.vb, self.vb.XAxis)
    
    
    @QtCore.pyqtSlot(QColor)
    def set_color(self, col):
        self.setPen(col)
     
        
    @QtCore.pyqtSlot(str)
    def set_side(self, side):
        side = side.lower()
        
        if self.side == side:
            return
        
        if side == "right":
            self.parent.plot.removeItem(self)
            self.vb.addItem(self)
        else:
            self.vb.removeItem(self)
            self.parent.plot.addItem(self)
            
        self.parent.vb.enableAutoRange()
        self.side = side
        