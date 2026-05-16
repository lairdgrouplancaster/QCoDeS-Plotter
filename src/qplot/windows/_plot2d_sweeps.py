from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui

from ._subplots.subplot2d import sweeper


class Plot2DSweepMixin:
    """Sweep and cut interactions for 2D heatmap plot windows."""

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
