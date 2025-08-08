import pyqtgraph as pg

from PyQt5 import QtCore
from PyQt5.QtGui import QColor

class subplot1d(pg.PlotDataItem):
    def __init__(self, parent, from_win, *args, **kargs):
        super().__init__(*args, **kargs)
        
        self.label = from_win.label
        self.param_dict = from_win.param_dict
        self.running = from_win.ds.running
        
        self.parent = parent
        self.from_win = from_win
        
        self.refresh()
        
        self.side = "left"
        self.parent.plot.addItem(self)
            
            
    def refresh(self):
        
        parent = self.parent
        from_win = self.from_win
        
        self.running = from_win.ds.running
        
        data = {}
        
    
        parent_options = parent.axis_options()
        from_win_options = from_win.axis_options()

        # for subplot, must share 1 axis parameter name, so check if flipped
        if parent_options["x"] == from_win_options["x"] or parent_options["y"] == from_win_options["y"]:
            choose_from = ["x", "y"]
        else:
            choose_from = ["y", "x"]
            
        for itr, axis in enumerate(["x", "y"]):
            data[axis] = from_win.axis_data[choose_from[itr]]
                    
                
        self.setData(
            x=data["x"], 
            y=data["y"],
            )
        # parent.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
    
    @QtCore.pyqtSlot(QColor)
    def set_color(self, col):
        self.setPen(col)
     
        
    @QtCore.pyqtSlot(str)
    def set_side(self, side):
        side = side.lower()
        parent = self.parent
        
        if self.side == side:
            return
        
        if side == "right":
            parent.plot.removeItem(self)
            parent.right_vb.addItem(self)
        else:
            parent.right_vb.removeItem(self)
            self.parent.plot.addItem(self)
            
        parent.vb.enableAutoRange()
        self.side = side
        
        
class custom_viewbox(pg.ViewBox):
    main_moved = QtCore.pyqtSignal([object])
    autoRange_triggered = QtCore.pyqtSignal()
    
    def mouseDragEvent(self, ev, axis=None):
        super().mouseDragEvent(ev, axis=axis)
        
        if axis is None:
            self.main_moved.emit(ev)
         
    def wheelEvent(self, ev, axis=None):
        super().wheelEvent(ev, axis=axis)
        
        if axis is None:
            self.main_moved.emit(ev)
       
    def autoRange(self, padding=None, items=None, item=None):
        super().autoRange(padding=padding, items=items, item=item)
        
        self.autoRange_triggered.emit()
