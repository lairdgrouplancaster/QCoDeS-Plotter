from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui

import pyqtgraph as pg

import numpy as np

from . import _colorbar
from ._plot2d_colorbar import Plot2DColorbarMixin
from ._plot2d_sweeps import Plot2DSweepMixin
from ._plotWin import plotWidget


_COLORBAR_COLORMAPS = _colorbar._COLORBAR_COLORMAPS


class plot2d(Plot2DSweepMixin, Plot2DColorbarMixin, plotWidget):
    """
    Plot window for 2d and higher plots, aka Heatmaps.
    Inherits and wraps several functions from qplot.windows._plotWin.plotWidget.
    PlotWidget handles majority of set up, recommend to view first.
    
    Key functions to see in plot2d:
        initFrame
        refreshPlot
        
    """
    open_subplot = QtCore.pyqtSignal([object, str, tuple])
    sweep_moved = QtCore.pyqtSignal([int, int])
    close_sweeps_requested = QtCore.pyqtSignal([object, object])
    
    def __init__(self, 
                 *args,
                 **kargs,
                 ):
        super().__init__(*args, **kargs)
        self.sweep_id = 0
        self.sweep_lines = {}
        self.active_sweep_line_id = None
        self.rotate = None # FOR SUBPLOT CURSOR
        self._colorbar_manual_levels = None

        
    def initFrame(self):
        """
        Sets up the initial plot and starting data.

        """
        self.image = pg.ImageItem(axisOrder='row-major')
        self.image.setZValue(0) # Like *Send to back*
        # self.image.setPxMode(True)
        
        self.plot.addItem(self.image)
        self._init_color_autoscale_button()
        self.hover_pixel_outline = qtw.QGraphicsRectItem()
        self.hover_pixel_outline.setPen(
            pg.mkPen((255, 255, 255, 190), width=1.5, cosmetic=True)
        )
        self.hover_pixel_outline.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self.hover_pixel_outline.setZValue(10)
        self.hover_pixel_outline.hide()
        self.plot.addItem(self.hover_pixel_outline)
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        self.show_status("Heatmap ready; loading data...", 5000)
      

    def initRefresh(self, refresh):
        super().initRefresh(refresh)
        
        self.toolbarRef.addSeparator()
        self.toolbarRef.addWidget(qtw.QLabel("On refresh:  "))
        
        self.toolbarRef.addWidget(qtw.QLabel("Re-Map Colors "))
        
        self.relevel_refresh = qtw.QCheckBox()
        self.relevel_refresh.setToolTip("Autoscale the heatmap colour range on each refresh")
        self.relevel_refresh.toggled.connect(self._colorbar_auto_refresh_changed)
        self.toolbarRef.addWidget(self.relevel_refresh)
     
    
    def initContextMenu(self):
        super().initContextMenu()

        autoColor = qtw.QAction("Autoscale Color", self)
        self.register_shortcut(autoColor, "Ctrl+Shift+C", "Autoscale color range")
        autoColor.triggered.connect(self.scaleColorbar)
        self.vbMenu.insertAction(self.autoscaleSep, autoColor)

        actions = self.vbMenu.actions()
        
        sep = self.vbMenu.insertSeparator(actions[3])
        
        ### Sweep control
        h_sweep = qtw.QAction("Horizontal Cut", self)
        self.register_shortcut(h_sweep, "H", "Plot horizontal cut")
        h_sweep.triggered.connect(lambda _: self.openSweep("h"))
        self.vbMenu.insertAction(sep, h_sweep)
        
        v_sweep = qtw.QAction("Vertical Cut", self)
        self.register_shortcut(v_sweep, "V", "Plot vertical cut")
        v_sweep.triggered.connect(lambda _: self.openSweep("v"))
        self.vbMenu.insertAction(sep, v_sweep)
        
        # Link finish update with check for rotation of sweep cursor
        self.end_wait.connect(self.rotate_sweeps)
        self.vbMenu.insertSeparator(h_sweep)

        self._init_colorbar_scale_controls()

        for key, text in (
                (QtCore.Qt.Key_Left, "Move selected cut left"),
                (QtCore.Qt.Key_Right, "Move selected cut right"),
                (QtCore.Qt.Key_Up, "Move selected cut up"),
                (QtCore.Qt.Key_Down, "Move selected cut down"),
                ):
            action = qtw.QAction(text, self)
            action.setShortcut(QtGui.QKeySequence(key))
            action.setShortcutContext(QtCore.Qt.WindowShortcut)
            action.triggered.connect(
                lambda _, key=key: self.move_sweep_with_arrow_key(key)
                )
            self.addAction(action)
        
        
    def initLabels(self):
        super().initLabels()
        self.z_index = None
        
        self.pos_labels["y"].setText(self.pos_labels["y"].text() + ";")
        
        posLabelx = qtw.QLabel("z= ")
        self.toolbarCo_ord.addWidget(posLabelx)
        self.pos_labels["z"] = posLabelx
        
###############################################################################
    
    def refreshPlot(self, finished : bool = True, worker=None):
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
        plot_worker = worker if worker is not None else self.worker
        if not super().refreshPlot(finished, worker=worker):
            plot_worker.running = False
            return

        try:
            if not self._has_plottable_heatmap_data():
                self.show_status(
                    f"Waiting for plottable data for {self.param.name}...",
                    5000,
                    )
                return

            autoLevels=self.relevel_refresh.isChecked()
            # Produce Heatmap
            self.image.setImage(
                self.dataGrid,
                autoLevels=autoLevels,
                autoRange=True
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
                    colorMap=self._colorbar_colormap(),
                    rounding=(
                        np.nanmax(self.dataGrid) - np.nanmin(self.dataGrid)
                        ) / 1e5,  # Add 10,000 colours
                    colorMapMenu=False,
                    )
                self._set_colorbar_tick_formatter()
                if self._colorbar_manual_levels is None:
                    self.scaleColorbar()
                else:
                    self._set_colorbar_levels(*self._colorbar_manual_levels)

            if autoLevels:
                self._colorbar_manual_levels = None
                self.scaleColorbar()
            elif self._colorbar_manual_levels is not None:
                self._set_colorbar_levels(*self._colorbar_manual_levels)
            
            self._update_hover_pixel_outline_from_index()
            if self.marquee is not None:
                self.set_marquee_rect(self.marquee)
            self._snap_sweep_lines_to_pixel_centres()
        finally:
            # Allow new workers after empty live loads or display errors.
            plot_worker.running = False


    def _has_plottable_heatmap_data(self):
        x_data = np.asarray(self.axis_data.get("x", []), dtype=float)
        y_data = np.asarray(self.axis_data.get("y", []), dtype=float)
        z_data = np.asarray(self.dataGrid, dtype=float)

        return (
            x_data.size > 0
            and y_data.size > 0
            and z_data.size > 0
            and np.any(np.isfinite(z_data))
            )


    def show_hover_pixel_outline(self, i, j):
        """
        Move the hover outline to the heatmap pixel at the given data indices.

        Parameters
        ----------
        i : int
            Column index within the heatmap data grid.
        j : int
            Row index within the heatmap data grid.
        """
        self.z_index = [i, j]
        self._update_hover_pixel_outline_from_index()


    def hide_hover_pixel_outline(self):
        """
        Hide the heatmap hover outline and clear the saved hover index.

        """
        self.z_index = None
        if hasattr(self, "hover_pixel_outline"):
            self.hover_pixel_outline.hide()


    def _update_hover_pixel_outline_from_index(self):
        if (
                not hasattr(self, "hover_pixel_outline")
                or not hasattr(self, "rect")
                or not hasattr(self, "dataGrid")
                or getattr(self, "z_index", None) is None
                ):
            if hasattr(self, "hover_pixel_outline"):
                self.hover_pixel_outline.hide()
            return

        i, j = self.z_index
        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or i < 0 or j < 0 or i >= cols or j >= rows:
            self.hover_pixel_outline.hide()
            return

        cell_width = self.rect.width() / cols
        cell_height = self.rect.height() / rows
        if cell_width <= 0 or cell_height <= 0:
            self.hover_pixel_outline.hide()
            return

        self.hover_pixel_outline.setRect(QtCore.QRectF(
            self.rect.x() + i * cell_width,
            self.rect.y() + j * cell_height,
            cell_width,
            cell_height,
        ))
        self.hover_pixel_outline.show()


    def _snap_marquee_rect(self, rect):
        """
        Snap marquee edges to heatmap pixel boundaries.

        """
        if not hasattr(self, "rect") or not hasattr(self, "dataGrid"):
            return rect

        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or self.rect.width() <= 0 or self.rect.height() <= 0:
            return rect

        left, right = self._snap_marquee_axis_to_cells(
            rect.left(),
            rect.right(),
            self.rect.x(),
            self.rect.width(),
            cols,
            )
        bottom, top = self._snap_marquee_axis_to_cells(
            rect.top(),
            rect.bottom(),
            self.rect.y(),
            self.rect.height(),
            rows,
            )

        return QtCore.QRectF(left, bottom, right - left, top - bottom)


    def _snap_marquee_axis_to_cells(self, low, high, origin, span, count):
        cell_size = span / count
        min_value = origin
        max_value = origin + span
        low = min(max(low, min_value), max_value)
        high = min(max(high, min_value), max_value)

        low_index = int(np.floor((low - origin) / cell_size))
        high_index = int(np.ceil((high - origin) / cell_size))
        low_index = min(max(low_index, 0), count - 1)
        high_index = min(max(high_index, low_index + 1), count)

        return (
            origin + low_index * cell_size,
            origin + high_index * cell_size,
            )


    def _add_marquee_color_context_action(self, menu):
        action = self._add_marquee_context_action(
            menu,
            "Zoom color",
            self.zoom_marquee_color,
            )
        if self._marquee_color_levels() is None:
            action.setEnabled(False)
            action.setToolTip("No finite data range inside the marquee.")
        return action


    def zoom_marquee_color(self):
        levels = self._marquee_color_levels()
        if levels is None:
            return False

        return self.setColorbarManualRange(*levels)


    def _marquee_stats_text(self):
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[np.isfinite(selected)]
        if values.size == 0:
            return None

        rows, cols = selected.shape
        rect = self._snap_marquee_rect(self.marquee.normalized())
        return self._format_marquee_stats_text(f"{cols}×{rows} points", values, rect)


    def _marquee_color_levels(self):
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[np.isfinite(selected)]
        if values.size == 0:
            return None

        vmin = float(values.min())
        vmax = float(values.max())
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            return None

        return vmin, vmax


    def _marquee_selected_data(self):
        if (
                self.__dict__.get("marquee") is None
                or "rect" not in self.__dict__
                or "dataGrid" not in self.__dict__
                ):
            return None

        slices = self._marquee_cell_slices()
        if slices is None:
            return None

        row_slice, col_slice = slices
        selected = np.asarray(self.dataGrid[row_slice, col_slice], dtype=float)
        if selected.size == 0:
            return None

        return selected


    def _marquee_cell_slices(self):
        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or self.rect.width() <= 0 or self.rect.height() <= 0:
            return None

        rect = self._snap_marquee_rect(self.marquee.normalized())
        if rect is None:
            return None

        col_slice = self._marquee_axis_slice(
            rect.left(),
            rect.right(),
            self.rect.x(),
            self.rect.width(),
            cols,
            )
        row_slice = self._marquee_axis_slice(
            rect.top(),
            rect.bottom(),
            self.rect.y(),
            self.rect.height(),
            rows,
            )
        if row_slice is None or col_slice is None:
            return None

        return row_slice, col_slice


    def _marquee_axis_slice(self, low, high, origin, span, count):
        if count <= 0 or span <= 0:
            return None

        cell_size = span / count
        min_value = origin
        max_value = origin + span
        low = min(max(low, min_value), max_value)
        high = min(max(high, min_value), max_value)

        start = int(np.floor((low - origin) / cell_size))
        stop = int(np.ceil((high - origin) / cell_size))
        start = min(max(start, 0), count - 1)
        stop = min(max(stop, start + 1), count)

        return slice(start, stop)
