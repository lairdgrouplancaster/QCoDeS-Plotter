import pyqtgraph as pg

from PyQt5 import QtCore
from PyQt5.QtGui import QColor

class subplot1d(pg.PlotDataItem):
    """
    Class for handling secondary line plots on plot1d
    """
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
        """
        Fetches data from source window and updates view on parent window

        """
        parent = self.parent
        from_win = self.from_win
        
        # Update live state
        self.running = from_win.ds.running
        
        data = {}
        
        # Get which data is on which axis
        parent_options = parent.axis_options()
        from_win_options = from_win.axis_options()

        # for subplot, must share 1 axis parameter name, so check if flipped
        if parent_options["x"] == from_win_options["x"] or parent_options["y"] == from_win_options["y"]:
            choose_from = ["x", "y"]
        else:
            choose_from = ["y", "x"]
            
        # Assign data to correct axis
        for itr, axis in enumerate(["x", "y"]):
            data[axis] = from_win.axis_data[choose_from[itr]]
                    
        # Updates display
        self.setData(
            x=data["x"], 
            y=data["y"],
            )
        # parent.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
    
    
    @QtCore.pyqtSlot(QColor)
    def set_color(self, col):
        """
        Event handler connect to qplot.windows._widgets.dropbox.picker_1d.color_box
        Updates the display color of line based on color_box selection

        Parameters
        ----------
        col : PyQt5.QtGui.QColor
            The color to change line to.

        """
        self.setPen(col)
     
        
    @QtCore.pyqtSlot(str)
    def set_side(self, side):
        """
        Event handler connect to qplot.windows._widgets.dropbox.picker_1d.axis_side
        Changes the axis the line is attached to on axis_side selection
        
        Parameters
        ----------
        side : str
            'left' or 'right', connects plot display to the corresponding y axis.

        """
        side = side.lower()
        parent = self.parent
        
        # Change cancelled
        if self.side == side:
            return
        
        # Remove from other viewbox and add to new viewbox
        if side == "right":
            parent.plot.removeItem(self)
            parent.right_vb.addItem(self)
        else:
            parent.right_vb.removeItem(self)
            self.parent.plot.addItem(self)
            
        parent.vb.enableAutoRange()
        self.side = side
        
        
class custom_viewbox(pg.ViewBox):
    """
    A custom view box used in qplot.windows.plotWin.PlotWidget which builds on
    the default viewbox of the plotItem
    
    The additional functions allow the main (left) viewbox to control the right
    viewbox and scale by the same relative amount.
    
    Each function emits a signal which tells qplot.windows.plot1d.plot1d.right_vb
    to do the same.
    """
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
