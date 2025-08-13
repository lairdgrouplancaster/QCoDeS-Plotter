from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

import pyqtgraph as pg

import numpy as np

from qplot.windows._plotWin import plotWidget
from ._subplots.subplot2d import sweeper

class plot2d(plotWidget):
    """
    Plot window for 2d and higher plots, aka Heatmaps.
    Inherits and wraps several functions from qplot.windows._plotWin.plotWidget.
    PlotWidget handles majority of set up, recommend to view first.
    
    Key functions to see in plot2d:
        initFrame
        refreshPlot
        
    """
    open_subplot = QtCore.pyqtSignal([object, tuple])
    
    def __init__(self, 
                 *args,
                 **kargs,
                 ):
        super().__init__(*args, **kargs)
        self.sweep_id = 0
        self.sweep_lines = {}

        
    def initFrame(self):
        """
        Sets up the initial plot and starting data.

        """
        self.image = pg.ImageItem()
        self.image.setZValue(0) # Like *Send to back*
        
        self.plot.addItem(self.image)
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        
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
        
        actions = self.vbMenu.actions()
        
        sep = self.vbMenu.insertSeparator(actions[3])
        
        h_sweep = qtw.QAction("Plot Horizontal Sweep", self)
        h_sweep.triggered.connect(lambda _: self.openSweep("h"))
        self.vbMenu.insertAction(sep, h_sweep)
        
        v_sweep = qtw.QAction("Plot Vertical Sweep", self)
        v_sweep.triggered.connect(lambda _: self.openSweep("v"))
        self.vbMenu.insertAction(sep, v_sweep)
        
        
    def initLabels(self):
        super().initLabels()
        self.z_index = None
        
        self.pos_labels["y"].setText(self.pos_labels["y"].text() + ";")
        
        posLabelx = qtw.QLabel("z= ")
        self.toolbarCo_ord.addWidget(posLabelx)
        self.pos_labels["z"] = posLabelx
        
###############################################################################
    
    def refreshPlot(self, finished : bool = True):
        """
        Updates plot based on data produced by the thread worker. Data is 
        assigned in plotWidget.refreshPlot, then all plot items are produced
        here.

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.
        """
        super().refreshPlot(finished)
        
        # Produce Heatmap
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
        
        # Link x/y axis values with Heatmap data
        self.rect = pg.QtCore.QRectF(
            xmin,
            ymin, 
            xrange, 
            yrange
        )
        self.image.setRect(self.rect)
        
        # Produce color bar on first run
        if not hasattr(self, "bar"):
            self.bar = self.plot.addColorBar(
                self.image,
                colorMap="magma",
                label=f"{self.param.label} ({self.param.unit})",
                rounding=(np.nanmax(self.dataGrid) - np.nanmin(self.dataGrid))/1e5 #Add 10,000 colours
                )
            self.scaleColorbar()
        
        # Allow new worker to be produced
        self.worker.running = False

###############################################################################    
        
    @QtCore.pyqtSlot(bool)
    def scaleColorbar(self, event = None):
        """
        Sets colorbar range to match heatmap value range, giving effect of
        rescaling color bar

        Parameters
        ----------
        Unused but required by slot

        """
        vmin, vmax = np.nanmin(self.dataGrid) , np.nanmax(self.dataGrid)

        self.bar.setLevels((vmin, vmax))

###############################################################################
# Subplot control

    def openSweep(self, side):
        # Quit out if not on heatmap
        if self.z_index is None:
            return
        
        # Fetch axes names
        axes = self.axis_options
        
        # Get fixed and sweep parameter
        if side == "v":
            fixed_var = axes["x"]
            sweep_var = axes["y"]
            fixed_index = self.z_index[0]
        elif side == "h":
            fixed_var = axes["y"]
            sweep_var = axes["x"]
            fixed_index = self.z_index[1]
        else:
            raise KeyError(f"Invalid sweep side, {side=}, must be 'v' or 'h'.")
            
        # Emit to Main window to open new window
        self.open_subplot.emit(sweeper,
                (
                self.sweep_id,
                sweep_var,
                fixed_var,
                fixed_index, 
                self.ds,
                self.param
                )
            )
        self.sweep_id += 1
            
        
    @QtCore.pyqtSlot(int, str, str, int, object)
    def update_sweep_line(self, sweep_id, sweep_param, fixed_param, fixed_index, line_col):
        
        # Check if display is possible on current axes
        if sweep_param not in self.axis_options.values() or fixed_param not in self.axis_options.values():

            return
        
        # get axis of fixed_param
        index = list(self.axis_options.values()).index(fixed_param)
        axis = list(self.axis_options.keys())[index]
    
        at_value = self.axis_data[axis][fixed_index]
    
        # Check if already has line and remove before adding new
        self.remove_sweep(sweep_id)
        
        # Produce line
        if axis == "x":
            line = self.plot.addLine(x=at_value, pen=line_col)
        else:
            line = self.plot.addLine(y=at_value, pen=line_col)
            
        line.setZValue(1) # Move to top
        self.sweep_lines[sweep_id] = line # Track for delete later
    
    
    @QtCore.pyqtSlot(int)
    def remove_sweep(self, sweep_id):
        """
        Event handler for subplot closing.
        Removes line sweep display from plot

        Parameters
        ----------
        sweep_id : int
            Number Id of Sweep.

        """
        if not self.sweep_lines.get(sweep_id, 0):
            return
        self.plot.removeItem(self.sweep_lines[sweep_id])
        self.sweep_lines.pop(sweep_id)