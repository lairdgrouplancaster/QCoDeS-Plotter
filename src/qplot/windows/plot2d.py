from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui

import pyqtgraph as pg

import numpy as np

from . import _colorbar
from ._plot2d_colorbar import Plot2DColorbarMixin
from ._plotWin import plotWidget
from ._subplots.subplot2d import sweeper


_COLORBAR_COLORMAPS = _colorbar._COLORBAR_COLORMAPS


class plot2d(Plot2DColorbarMixin, plotWidget):
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


    def openSweep(self, side):
        """
        Emits a signal to the Main window to open the sweep via 
        MainWindow.openWin()

        Parameters
        ----------
        side : str
            "h": horizontal, or "v": vertical. Along which axes the sweep will
            be performed.

        Raises
        ------
        KeyError
            Invalid side parameter.

        """
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
        self.open_subplot.emit(
                sweeper,
                self._guid,
                (
                self.sweep_id,
                sweep_var,
                fixed_var,
                fixed_index,
                self.param
                )
            )
        # Update interal id for multiple sweeps
        self.sweep_id += 1
        

    @QtCore.pyqtSlot(int, str, str, int, object)
    def update_sweep_line(self, sweep_id, sweep_param, fixed_param, fixed_index, line_col):
        """
        Event handler for update to suplot sweep
        Updates the sweep cursor on the main plot in response to changes in the
        subplot

        Parameters
        ----------
        sweep_id : int
            The track of subplots to know which subplot cursor to edit.
        sweep_param : str
            The parameter over which the sweep subplot looks. Used to confirm
            that a cursor can be plotted
        fixed_param : str
            The static parameter and the parameter to place the line on.
        fixed_index : int
            index of on heatmap to place the line.
        line_col : QPen
            The plen color of the line.


        """
        # Check if display is possible on current axes
        if sweep_param not in self.axis_options.values() or fixed_param not in self.axis_options.values():
            return
        
        # get axis of fixed_param
        index = list(self.axis_options.values()).index(fixed_param)
        axis = list(self.axis_options.keys())[index]
    
        at_value = self.sweep_pixel_centre(axis, fixed_index)
    
        if self.sweep_lines.get(sweep_id, None) is not None:
            line = self.sweep_lines[sweep_id]
            
            # Update line data
            line.angle = (90 if axis == "x" else 0)
            line.pen = line_col
            line.hoverPen = line_col
            line.currentPen = line_col
            
            # refresh
            line.resetTransform()
            line.setRotation(line.angle)
            line.setPos(at_value)
            self.set_sweep_line_cursor(line)
            
    
        # Set up new line
        else:
            # Produce line
            if axis == "x":
                line = self.plot.addLine(
                    x=at_value, 
                    pen=line_col, 
                    movable=True
                    )
            else:
                line = self.plot.addLine(
                    y=at_value, 
                    pen=line_col, 
                    movable=True
                    )
                
                
            line.setZValue(1) # Move to top
            line.sigDragged.connect(self.moving_sweep)
            line.sigClicked.connect(self.activate_sweep_line)
            self.sweep_lines[sweep_id] = line # Track for update/delete
            line.sweep_id = sweep_id # give copy of id if needed
            self.set_sweep_line_cursor(line)
        
        self.set_sweep_line_index(line, fixed_index, emit=False)
    
    
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
        #check exists, then remove
        if self.sweep_lines.get(sweep_id, None) is None:
            return
        self.restore_sweep_line_hover_cursor(self.sweep_lines[sweep_id])
        self.restore_sweep_line_drag_cursor(self.sweep_lines[sweep_id])
        self.plot.removeItem(self.sweep_lines[sweep_id])
        self.sweep_lines.pop(sweep_id)
        
        
    @QtCore.pyqtSlot()
    def change_axis(self, key : str):
        
        # Rotate lines in case of duplciates
        options = self.axis_options
        if options["x"] == options["y"]:
            self.rotate = True
        else: # Otherwise delete them
            self.rotate = False
            
        super().change_axis(key)

    
    @QtCore.pyqtSlot()  
    def rotate_sweeps(self):
        """
        Event handler for changing assigned axes (is connected to self.end_wait
                                                  in self.refreshPlot)
        
        Rotates sweep cursors if the axis is flipped. Otherwise removes them

        Returns
        -------
        None.

        """
        if self.rotate is None: # Not from changing axis parameters
            return
        
        # remote lines as parameters have changed
        if not self.rotate:
            for key in self.sweep_lines.keys():
                self.remove_sweep(key)
            self.rotate = None
            return
            
        # Rotate lines as parameters switched
        for key, line in self.sweep_lines.items():
            line = self.sweep_lines[key]
            # Rotate
            pos = line.value()
            line.angle = 90 if line.angle == 0 else 0
            
            line.resetTransform()
            line.setRotation(line.angle)
            line.setPos(pos) # force line placement into correct spot
            self.set_sweep_line_cursor(line)
            
        self.rotate = None
    
    
    def sweep_axis_count(self, axis):
        if axis == "x":
            return self.dataGrid.shape[1]
        return self.dataGrid.shape[0]


    def sweep_pixel_centre(self, axis, index):
        """
        Return the plot coordinate at the centre of a heatmap pixel.

        """
        count = self.sweep_axis_count(axis)
        index = min(max(int(index), 0), count - 1)

        if axis == "x":
            return self.rect.x() + (index + 0.5) * self.rect.width() / count
        return self.rect.y() + (index + 0.5) * self.rect.height() / count


    def sweep_index_at_value(self, axis, value):
        """
        Return the heatmap pixel index containing a plot coordinate.

        """
        count = self.sweep_axis_count(axis)
        if axis == "x":
            start = self.rect.x()
            width = self.rect.width()
        else:
            start = self.rect.y()
            width = self.rect.height()

        if count <= 0 or width <= 0:
            return None

        index = int((value - start) / width * count)
        return min(max(index, 0), count - 1)


    def line_sweep_axis(self, line):
        return "x" if line.angle == 90 else "y"


    def sweep_line_cursor_shape(self, line):
        if self.line_sweep_axis(line) == "x":
            return QtCore.Qt.SizeHorCursor
        return QtCore.Qt.SizeVerCursor


    def set_sweep_line_cursor(self, line):
        if not getattr(line, "movable", False):
            self.restore_sweep_line_hover_cursor(line)
            self.restore_sweep_line_drag_cursor(line)
            line.unsetCursor()
            return

        line.setCursor(self.sweep_line_cursor_shape(line))
        self.install_sweep_line_hover_cursor(line)
        self.install_sweep_line_drag_cursor(line)
        self.update_sweep_line_hover_cursor(line)


    def install_sweep_line_drag_cursor(self, line):
        if getattr(line, "_qplot_sweep_drag_cursor_installed", False):
            return

        previous_mouse_drag_event = getattr(line, "mouseDragEvent", None)
        if previous_mouse_drag_event is None:
            return

        def mouse_drag_event(event):
            if self._sweep_line_drag_cursor_event_applies(line, event):
                self.set_sweep_line_drag_cursor(line)

            try:
                return previous_mouse_drag_event(event)
            finally:
                if event.isFinish():
                    self.restore_sweep_line_drag_cursor(line)

        line.mouseDragEvent = mouse_drag_event
        line._qplot_sweep_drag_cursor_installed = True


    def install_sweep_line_hover_cursor(self, line):
        if getattr(line, "_qplot_sweep_hover_cursor_installed", False):
            return

        previous_hover_event = getattr(line, "hoverEvent", None)
        if previous_hover_event is None:
            return

        def hover_event(event):
            previous_hover_event(event)
            if getattr(line, "mouseHovering", False):
                self.set_sweep_line_hover_cursor(line)
            else:
                self.restore_sweep_line_hover_cursor(line)

        line.hoverEvent = hover_event
        line._qplot_sweep_hover_cursor_installed = True


    def _sweep_line_drag_cursor_event_applies(self, line, event):
        if not getattr(line, "movable", False):
            return False

        button = getattr(event, "button", lambda: None)()
        return button == QtCore.Qt.LeftButton


    def set_sweep_line_drag_cursor(self, line):
        self.set_sweep_line_override_cursor(line, "drag")


    def restore_sweep_line_drag_cursor(self, line):
        self.restore_sweep_line_override_cursor(line, "drag")


    def set_sweep_line_hover_cursor(self, line):
        self.set_sweep_line_override_cursor(line, "hover")


    def restore_sweep_line_hover_cursor(self, line):
        self.restore_sweep_line_override_cursor(line, "hover")


    def set_sweep_line_override_cursor(self, line, reason):
        if qtw.QApplication.instance() is None:
            return

        active_attribute = f"_qplot_sweep_{reason}_cursor_override_active"
        shape_attribute = f"_qplot_sweep_{reason}_cursor_shape"
        cursor_shape = self.sweep_line_cursor_shape(line)
        cursor = QtGui.QCursor(cursor_shape)
        if getattr(line, active_attribute, False):
            if getattr(line, shape_attribute, None) != cursor_shape:
                qtw.QApplication.changeOverrideCursor(cursor)
                setattr(line, shape_attribute, cursor_shape)
            return

        qtw.QApplication.setOverrideCursor(cursor)
        setattr(line, active_attribute, True)
        setattr(line, shape_attribute, cursor_shape)


    def restore_sweep_line_override_cursor(self, line, reason):
        active_attribute = f"_qplot_sweep_{reason}_cursor_override_active"
        if not getattr(line, active_attribute, False):
            return

        if qtw.QApplication.instance() is not None:
            qtw.QApplication.restoreOverrideCursor()
        setattr(line, active_attribute, False)
        setattr(line, f"_qplot_sweep_{reason}_cursor_shape", None)


    def update_sweep_line_hover_cursor(self, line):
        if self.sweep_line_contains_global_cursor(line):
            self.set_sweep_line_hover_cursor(line)
        else:
            self.restore_sweep_line_hover_cursor(line)


    def sweep_line_contains_global_cursor(self, line):
        widget = self.__dict__.get("widget")
        if widget is None:
            return False

        try:
            cursor_pos = QtGui.QCursor.pos()
            view_pos = widget.mapFromGlobal(cursor_pos)
            scene_pos = widget.mapToScene(view_pos)
            return line.contains(line.mapFromScene(scene_pos))
        except (AttributeError, RuntimeError, TypeError):
            return False


    def activate_sweep_line(self, line, event=None):
        self.active_sweep_line_id = line.sweep_id
        if self.sweep_line_remove_requested(event):
            self.request_sweep_line_removal(line, event)
            if event is not None:
                event.accept()
            return

        self.set_sweep_line_hover_cursor(line)
        if event is not None:
            event.accept()


    def sweep_line_remove_requested(self, event):
        if event is None:
            return False

        button = getattr(event, "button", lambda: None)()
        double_clicked = getattr(event, "double", lambda: False)()
        return button == QtCore.Qt.LeftButton and double_clicked


    def request_sweep_line_removal(self, line, event=None):
        if self.sweep_line_remove_all_requested(event):
            sweep_ids = tuple(sorted(self.sweep_lines.keys()))
        else:
            sweep_ids = (line.sweep_id,)

        self.close_sweeps_requested.emit(self, sweep_ids)


    def sweep_line_remove_all_requested(self, event):
        if event is None:
            return False

        modifiers = getattr(event, "modifiers", lambda: QtCore.Qt.NoModifier)()
        return bool(modifiers & QtCore.Qt.ShiftModifier)


    def set_sweep_line_index(self, line, index, emit=True):
        axis = self.line_sweep_axis(line)
        count = self.sweep_axis_count(axis)
        index = min(max(int(index), 0), count - 1)

        line.setBounds((
            self.sweep_pixel_centre(axis, 0),
            self.sweep_pixel_centre(axis, count - 1)
            ))
        line.setPos(self.sweep_pixel_centre(axis, index))
        line.sweep_index = index
        self.active_sweep_line_id = line.sweep_id

        if emit:
            self.sweep_moved.emit(line.sweep_id, index)


    def _snap_sweep_lines_to_pixel_centres(self):
        for line in self.sweep_lines.values():
            axis = self.line_sweep_axis(line)
            index = getattr(line, "sweep_index", None)
            if index is None:
                index = self.sweep_index_at_value(axis, line.value())
            if index is not None:
                self.set_sweep_line_index(line, index, emit=False)


    def move_sweep_with_arrow_key(self, key):
        moves = {
            QtCore.Qt.Key_Left: ("x", -1),
            QtCore.Qt.Key_Right: ("x", 1),
            QtCore.Qt.Key_Down: ("y", -1),
            QtCore.Qt.Key_Up: ("y", 1),
            }
        if key not in moves:
            return

        axis, step = moves[key]
        line = self.sweep_line_for_keyboard_move(axis)
        if line is None:
            return

        index = getattr(line, "sweep_index", None)
        if index is None:
            index = self.sweep_index_at_value(axis, line.value())
        if index is None:
            return

        self.set_sweep_line_index(line, index + step)


    def sweep_line_index(self, line):
        index = getattr(line, "sweep_index", None)
        if index is not None:
            return index

        axis = self.line_sweep_axis(line)
        return self.sweep_index_at_value(axis, line.value())


    def sweep_line_for_keyboard_move(self, axis):
        matching_lines = [
            line for line in self.sweep_lines.values()
            if self.line_sweep_axis(line) == axis
            ]
        if not matching_lines:
            return None

        active_line = self.sweep_lines.get(getattr(self, "active_sweep_line_id", None))
        if active_line in matching_lines:
            return active_line

        return max(matching_lines, key=lambda line: line.sweep_id)


    def sweep_group_drag_requested(self):
        if qtw.QApplication.instance() is None:
            return False

        return bool(qtw.QApplication.keyboardModifiers() & QtCore.Qt.ShiftModifier)


    def move_sweep_group(self, dragged_line, dragged_index):
        """
        Move all same-orientation sweep lines by the dragged line's index delta.

        """
        axis = self.line_sweep_axis(dragged_line)
        previous_index = self.sweep_line_index(dragged_line)
        if previous_index is None or dragged_index is None:
            return

        requested_delta = dragged_index - previous_index
        group_lines = [
            line for line in self.sweep_lines.values()
            if self.line_sweep_axis(line) == axis
            ]
        if dragged_line not in group_lines:
            group_lines.append(dragged_line)

        indexed_lines = []
        for line in group_lines:
            index = self.sweep_line_index(line)
            if index is not None:
                indexed_lines.append((line, index))
        if not indexed_lines:
            return

        delta = self.bounded_sweep_group_delta(axis, indexed_lines, requested_delta)
        for line, index in indexed_lines:
            self.set_sweep_line_index(line, index + delta)

        self.active_sweep_line_id = dragged_line.sweep_id


    def bounded_sweep_group_delta(self, axis, indexed_lines, requested_delta):
        count = self.sweep_axis_count(axis)
        min_delta = max(-index for _line, index in indexed_lines)
        max_delta = min(count - 1 - index for _line, index in indexed_lines)
        return min(max(requested_delta, min_delta), max_delta)


    @QtCore.pyqtSlot(object)
    def moving_sweep(self, line):
        """
        Event handler for dragging sweep cursor.
        
        Uses line possition to find index of fixed parameter and sends to 
        signal to subplot window to move sweep scan to new location.

        Parameters
        ----------
        line : pyqtgraph.graphicsItems.InfiniteLine
            The line being dragged.

        """        
        pos = line.value()
        axis = self.line_sweep_axis(line)
        index = self.sweep_index_at_value(axis, pos)

        if index is not None:
            if self.sweep_group_drag_requested():
                self.move_sweep_group(line, index)
            else:
                self.set_sweep_line_index(line, index)
