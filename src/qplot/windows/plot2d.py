from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui

import pyqtgraph as pg

import numpy as np

from ._plotWin import plotWidget
from ._subplots.subplot2d import sweeper


_ENGINEERING_PREFIXES = {
    -24: "y",
    -21: "z",
    -18: "a",
    -15: "f",
    -12: "p",
    -9: "n",
    -6: "u",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
    12: "T",
    15: "P",
    18: "E",
    21: "Z",
    24: "Y",
}


def _trim_decimal_places(value, decimal_places=3):
    """
    Format a float with up to decimal_places, trimming trailing zeros.

    """
    text = f"{value:.{decimal_places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _format_engineering_tick(value, decimal_places=3):
    """
    Format an axis tick using compact engineering notation.

    """
    if not np.isfinite(value):
        return ""

    if value == 0:
        return "0"

    exponent = int(np.floor(np.log10(abs(value)) / 3) * 3)
    exponent = max(min(exponent, 24), -24)
    scaled = value / 10**exponent
    rounded_scaled = round(scaled, decimal_places)
    if abs(rounded_scaled) >= 1000 and exponent < 24:
        exponent += 3
        scaled = value / 10**exponent

    prefix = _ENGINEERING_PREFIXES[exponent]

    return f"{_trim_decimal_places(scaled, decimal_places)}{prefix}"


def _engineering_tick_strings(values, scale=1.0, spacing=None):
    """
    Return engineering-notation labels for pyqtgraph AxisItem ticks.

    """
    return [_format_engineering_tick(value * scale) for value in values]


class plot2d(plotWidget):
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
        h_sweep = qtw.QAction("Plot Horizontal Cut", self)
        self.register_shortcut(h_sweep, "Ctrl+Shift+H", "Plot horizontal cut")
        h_sweep.triggered.connect(lambda _: self.openSweep("h"))
        self.vbMenu.insertAction(sep, h_sweep)
        
        v_sweep = qtw.QAction("Plot Vertical Cut", self)
        self.register_shortcut(v_sweep, "Ctrl+Shift+V", "Plot vertical cut")
        v_sweep.triggered.connect(lambda _: self.openSweep("v"))
        self.vbMenu.insertAction(sep, v_sweep)
        
        # Link finish update with check for rotation of sweep cursor
        self.end_wait.connect(self.rotate_sweeps)
        self.vbMenu.insertSeparator(h_sweep)

        self._init_colorbar_context_menu()

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
        if not super().refreshPlot(finished, worker=worker):
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
                colorMap=self.config.get("user_preference.bar_colour"),
                label=f"{self.param.label} ({self.param.unit})",
                rounding=(np.nanmax(self.dataGrid) - np.nanmin(self.dataGrid))/1e5 #Add 10,000 colours
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
        self._snap_sweep_lines_to_pixel_centres()
            
        # Allow new worker to be produced
        self.worker.running = False


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


    @QtCore.pyqtSlot(bool)
    def scaleColorbar(self, event = None):
        """
        Sets colorbar range to match heatmap value range, giving effect of
        rescaling color bar

        Parameters
        ----------
        Unused but required by slot

        """
        levels = self._data_colorbar_levels()
        if levels is None:
            return

        self._colorbar_manual_levels = None
        self._set_colorbar_levels(*levels)


    def _data_colorbar_levels(self):
        """
        Return finite min/max levels from the current heatmap data.

        """
        data = getattr(self, "dataGrid", None)
        if data is None:
            return None

        vmin, vmax = np.nanmin(data), np.nanmax(data)
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            return None

        return float(vmin), float(vmax)


    def _set_colorbar_levels(self, vmin, vmax):
        """
        Apply levels to the colorbar and mirror them in the menu fields.

        """
        bar = self.__dict__.get("bar")
        if bar is not None:
            bar.setLevels((vmin, vmax))

        self._sync_colorbar_level_fields(vmin, vmax)


    def _set_colorbar_tick_formatter(self):
        """
        Use engineering notation for colorbar tick labels.

        """
        bar = self.__dict__.get("bar")
        axis = getattr(bar, "axis", None)
        if axis is None:
            return

        axis.tickStrings = _engineering_tick_strings
        axis.setWidth(70)
        axis.setStyle(tickTextWidth=60)
        axis.picture = None
        axis.update()


    def _current_colorbar_levels(self):
        """
        Return the currently displayed colorbar levels for menu synchronisation.

        """
        bar = self.__dict__.get("bar")
        if bar is not None:
            return bar.levels()

        manual_levels = getattr(self, "_colorbar_manual_levels", None)
        if manual_levels is not None:
            return manual_levels

        return self._data_colorbar_levels()


    def _init_colorbar_context_menu(self):
        """
        Add manual/auto color scale controls to the plot context menu.

        """
        self.colorbar_menu = qtw.QMenu("Color scale", self.vbMenu)
        controls = qtw.QWidget()
        layout = qtw.QGridLayout(controls)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(4)
        layout.setVerticalSpacing(4)

        self.colorbar_manual_radio = qtw.QRadioButton("Manual")
        self.colorbar_auto_radio = qtw.QRadioButton("Auto")
        self.colorbar_min_text = qtw.QLineEdit()
        self.colorbar_max_text = qtw.QLineEdit()

        validator = QtGui.QDoubleValidator(self)
        self.colorbar_min_text.setValidator(validator)
        self.colorbar_max_text.setValidator(validator)
        for line_edit in (self.colorbar_min_text, self.colorbar_max_text):
            line_edit.setMinimumWidth(80)

        self.colorbar_button_group = qtw.QButtonGroup(self)
        self.colorbar_button_group.addButton(self.colorbar_manual_radio)
        self.colorbar_button_group.addButton(self.colorbar_auto_radio)
        self.colorbar_button_group.setExclusive(True)

        layout.addWidget(self.colorbar_manual_radio, 0, 0)
        layout.addWidget(self.colorbar_min_text, 0, 1)
        layout.addWidget(self.colorbar_max_text, 0, 2)
        layout.addWidget(self.colorbar_auto_radio, 1, 0)

        controls_action = qtw.QWidgetAction(self.colorbar_menu)
        controls_action.setDefaultWidget(controls)
        self.colorbar_menu.addAction(controls_action)

        self.colorbar_menu.aboutToShow.connect(self._sync_colorbar_menu)
        self.colorbar_manual_radio.clicked.connect(self._apply_colorbar_manual_fields)
        self.colorbar_min_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_max_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_auto_radio.clicked.connect(self.setColorbarAuto)

        self._insert_colorbar_menu()
        self._sync_colorbar_menu()


    def _insert_colorbar_menu(self):
        """
        Place the color scale menu next to pyqtgraph's X/Y axis menus.

        """
        actions = self.vbMenu.actions()
        insert_before = None

        for index, action in enumerate(actions):
            if action.text().replace("&", "") == "Y axis":
                if index + 1 < len(actions):
                    insert_before = actions[index + 1]
                break

        if insert_before is None:
            self.vbMenu.addMenu(self.colorbar_menu)
        else:
            self.vbMenu.insertMenu(insert_before, self.colorbar_menu)


    def _sync_colorbar_menu(self):
        """
        Update color scale menu controls from the current colorbar state.

        """
        levels = self._current_colorbar_levels()
        if levels is not None:
            self._sync_colorbar_level_fields(*levels)

        manual = getattr(self, "_colorbar_manual_levels", None) is not None
        for widget in (self.colorbar_manual_radio, self.colorbar_auto_radio):
            widget.blockSignals(True)

        self.colorbar_manual_radio.setChecked(manual)
        self.colorbar_auto_radio.setChecked(not manual)

        for widget in (self.colorbar_manual_radio, self.colorbar_auto_radio):
            widget.blockSignals(False)


    def _sync_colorbar_level_fields(self, vmin, vmax):
        """
        Mirror colorbar levels into the context menu text fields.

        """
        if "colorbar_min_text" not in self.__dict__:
            return

        for widget, value in (
                (self.colorbar_min_text, vmin),
                (self.colorbar_max_text, vmax),
                ):
            widget.blockSignals(True)
            widget.setText(f"{value:.6g}")
            widget.blockSignals(False)


    @QtCore.pyqtSlot()
    def _apply_colorbar_manual_fields(self):
        """
        Apply color scale levels entered in the context menu.

        """
        try:
            vmin = float(self.colorbar_min_text.text())
            vmax = float(self.colorbar_max_text.text())
        except ValueError:
            self.show_status("Invalid color scale range.", 5000)
            self._sync_colorbar_menu()
            return

        self.setColorbarManualRange(vmin, vmax)


    def setColorbarManualRange(self, vmin, vmax):
        """
        Set a persistent manual color scale range.

        """
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            self.show_status("Color scale minimum must be below maximum.", 5000)
            self._sync_colorbar_menu()
            return False

        self._colorbar_manual_levels = (float(vmin), float(vmax))

        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(False)

        self._set_colorbar_levels(*self._colorbar_manual_levels)
        if "colorbar_manual_radio" in self.__dict__:
            self.colorbar_manual_radio.setChecked(True)
        return True


    @QtCore.pyqtSlot()
    def setColorbarAuto(self):
        """
        Return the color scale to automatic data-range scaling.

        """
        self._colorbar_manual_levels = None
        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(True)
        self.scaleColorbar()


    @QtCore.pyqtSlot(bool)
    def _colorbar_auto_refresh_changed(self, enabled):
        """
        Keep colorbar manual state in sync with the refresh toolbar checkbox.

        """
        if enabled:
            self._colorbar_manual_levels = None
            self.scaleColorbar()

###############################################################################
# Subplot control

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


    def activate_sweep_line(self, line, event=None):
        self.active_sweep_line_id = line.sweep_id
        if event is not None:
            event.accept()


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
            self.set_sweep_line_index(line, index)
