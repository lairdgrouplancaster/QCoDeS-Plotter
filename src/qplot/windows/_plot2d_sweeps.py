from itertools import count
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import numpy.typing as npt
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._subplots.subplot2d import sweeper

_SweepAxis = Literal["x", "y"]

if TYPE_CHECKING:
    from qplot.tools.heatmap_geometry import HeatmapGeometry

    class _Plot2DSweepBase(qtw.QMainWindow):
        _dataset_key: Any
        active_sweep_line_id: int | None
        axis_options: dict[str, str]
        close_sweeps_requested: Any
        dataGrid: npt.NDArray[Any]
        open_subplot: Any
        param: Any
        plot: Any
        rotate: bool | None
        sweep_lines: dict[int, Any]
        sweep_moved: Any

        def change_axis(self, key: str) -> None: ...

        def clear_marquee(self) -> None: ...

        def hide_hover_pixel_outline(self) -> None: ...

        def _heatmap_geometry(self) -> HeatmapGeometry | None: ...

        def _required_heatmap_geometry(self) -> HeatmapGeometry: ...

        def _reset_heatmap_hover(self) -> None: ...
else:
    class _Plot2DSweepBase:
        pass


_SWEEP_ID_COUNTER = count()


class Plot2DSweepMixin(_Plot2DSweepBase):
    """Sweep and cut interactions for 2D heatmap plot windows."""

    @staticmethod
    def _reserve_sweep_id() -> int:
        """Return an application-session-unique identity for a heatmap cut."""

        return next(_SWEEP_ID_COUNTER)

    def openSweep(self, side: str) -> None:
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
        z_index = self.__dict__.get("z_index")
        if not isinstance(z_index, list):
            return
        
        # Fetch axes names
        axes = self.axis_options
        
        # Get fixed and sweep parameter
        if side == "v":
            fixed_var = axes["x"]
            sweep_var = axes["y"]
            fixed_index = z_index[0]
            fixed_axis: _SweepAxis = "x"
        elif side == "h":
            fixed_var = axes["y"]
            sweep_var = axes["x"]
            fixed_index = z_index[1]
            fixed_axis = "y"
        else:
            raise KeyError(f"Invalid sweep side, {side=}, must be 'v' or 'h'.")

        fixed_value = float(fixed_index)
        geometry_getter = getattr(self, "_heatmap_geometry", None)
        if callable(geometry_getter) and geometry_getter() is not None:
            fixed_value = self.sweep_pixel_centre(fixed_axis, fixed_index)
            
        sweep_id = self._reserve_sweep_id()

        # Emit to Main window to open new window
        self.open_subplot.emit(
                sweeper,
                self._dataset_key,
                (
                sweep_id,
                sweep_var,
                fixed_var,
                fixed_value,
                self.param
                )
            )
        

    @QtCore.pyqtSlot(int, str, str, float, object)
    def update_sweep_line(
            self,
            sweep_id: int,
            sweep_param: str,
            fixed_param: str,
            fixed_value: float,
            line_col: Any,
            ) -> None:
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
        fixed_value : float
            Physical fixed-axis coordinate at which to place the line.
        line_col : QPen
            The plen color of the line.


        """
        if self._heatmap_geometry() is None:
            return
        # Check if display is possible on current axes
        if sweep_param not in self.axis_options.values() or fixed_param not in self.axis_options.values():
            return
        
        # get axis of fixed_param
        index = list(self.axis_options.values()).index(fixed_param)
        axis_name = list(self.axis_options.keys())[index]
        if axis_name not in ("x", "y"):
            return
        axis: _SweepAxis = "x" if axis_name == "x" else "y"

        fixed_index = self.sweep_index_at_value(axis, fixed_value, clamp=False)
        if fixed_index is None:
            line = self.sweep_lines.get(sweep_id)
            if line is not None:
                line.sweep_index = None
                set_visible = getattr(line, "setVisible", None)
                if callable(set_visible):
                    set_visible(False)
            return

        at_value = self.sweep_pixel_centre(axis, fixed_index)
    
        if self.sweep_lines.get(sweep_id, None) is not None:
            line = self.sweep_lines[sweep_id]
            set_visible = getattr(line, "setVisible", None)
            if callable(set_visible):
                set_visible(True)
            
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
    def remove_sweep(self, sweep_id: int) -> None:
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
    def change_axis(self, key: str) -> None:
        self._reset_heatmap_hover()
        if self.__dict__.get("marquee") is not None:
            self.clear_marquee()
        
        # Rotate lines in case of duplciates
        options = self.axis_options
        if options["x"] == options["y"]:
            self.rotate = True
        else: # Otherwise delete them
            self.rotate = False
            
        super().change_axis(key)

    
    @QtCore.pyqtSlot()  
    def rotate_sweeps(self) -> None:
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
            for key in list(self.sweep_lines.keys()):
                self.remove_sweep(key)
            self.__dict__["rotate"] = None
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
            
        self.__dict__["rotate"] = None
    
    
    def sweep_axis_count(self, axis: _SweepAxis) -> int:
        geometry = self._heatmap_geometry()
        if geometry is None:
            return 0
        return geometry.x.count if axis == "x" else geometry.y.count


    def sweep_pixel_centre(self, axis: _SweepAxis, index: int) -> float:
        """
        Return the plot coordinate at the centre of a heatmap pixel.

        """
        geometry = self._required_heatmap_geometry()
        axis_geometry = geometry.x if axis == "x" else geometry.y
        index = min(max(int(index), 0), axis_geometry.count - 1)
        return axis_geometry.centre(index)


    def sweep_index_at_value(
            self,
            axis: _SweepAxis,
            value: float,
            *,
            clamp: bool = True,
            ) -> int | None:
        """
        Return the heatmap pixel index containing a plot coordinate.

        """
        geometry = self._heatmap_geometry()
        if geometry is None:
            return None
        axis_geometry = geometry.x if axis == "x" else geometry.y
        return axis_geometry.index_at(value, clamp=clamp)


    def line_sweep_axis(self, line: Any) -> _SweepAxis:
        return "x" if line.angle == 90 else "y"


    def sweep_line_cursor_shape(self, line: Any) -> QtCore.Qt.CursorShape:
        if self.line_sweep_axis(line) == "x":
            return QtCore.Qt.CursorShape.SizeHorCursor
        return QtCore.Qt.CursorShape.SizeVerCursor


    def set_sweep_line_cursor(self, line: Any) -> None:
        if not getattr(line, "movable", False):
            self.restore_sweep_line_hover_cursor(line)
            self.restore_sweep_line_drag_cursor(line)
            line.unsetCursor()
            return

        line.setCursor(self.sweep_line_cursor_shape(line))
        self.install_sweep_line_hover_cursor(line)
        self.install_sweep_line_drag_cursor(line)
        self.update_sweep_line_hover_cursor(line)


    def install_sweep_line_drag_cursor(self, line: Any) -> None:
        if getattr(line, "_qplot_sweep_drag_cursor_installed", False):
            return

        previous_mouse_drag_event = getattr(line, "mouseDragEvent", None)
        if previous_mouse_drag_event is None:
            return

        def mouse_drag_event(event: Any) -> Any:
            if self._sweep_line_drag_cursor_event_applies(line, event):
                self.set_sweep_line_drag_cursor(line)

            try:
                return previous_mouse_drag_event(event)
            finally:
                if event.isFinish():
                    self.restore_sweep_line_drag_cursor(line)

        line.mouseDragEvent = mouse_drag_event
        line._qplot_sweep_drag_cursor_installed = True


    def install_sweep_line_hover_cursor(self, line: Any) -> None:
        if getattr(line, "_qplot_sweep_hover_cursor_installed", False):
            return

        previous_hover_event = getattr(line, "hoverEvent", None)
        if previous_hover_event is None:
            return

        def hover_event(event: Any) -> None:
            previous_hover_event(event)
            if getattr(line, "mouseHovering", False):
                self.set_sweep_line_hover_cursor(line)
            else:
                self.restore_sweep_line_hover_cursor(line)

        line.hoverEvent = hover_event
        line._qplot_sweep_hover_cursor_installed = True


    def _sweep_line_drag_cursor_event_applies(self, line: Any, event: Any) -> bool:
        if not getattr(line, "movable", False):
            return False

        button = getattr(event, "button", lambda: None)()
        return button == QtCore.Qt.MouseButton.LeftButton


    def set_sweep_line_drag_cursor(self, line: Any) -> None:
        self.set_sweep_line_override_cursor(line, "drag")


    def restore_sweep_line_drag_cursor(self, line: Any) -> None:
        self.restore_sweep_line_override_cursor(line, "drag")


    def set_sweep_line_hover_cursor(self, line: Any) -> None:
        self.set_sweep_line_override_cursor(line, "hover")


    def restore_sweep_line_hover_cursor(self, line: Any) -> None:
        self.restore_sweep_line_override_cursor(line, "hover")


    def set_sweep_line_override_cursor(self, line: Any, reason: str) -> None:
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


    def restore_sweep_line_override_cursor(self, line: Any, reason: str) -> None:
        active_attribute = f"_qplot_sweep_{reason}_cursor_override_active"
        if not getattr(line, active_attribute, False):
            return

        if qtw.QApplication.instance() is not None:
            qtw.QApplication.restoreOverrideCursor()
        setattr(line, active_attribute, False)
        setattr(line, f"_qplot_sweep_{reason}_cursor_shape", None)


    def update_sweep_line_hover_cursor(self, line: Any) -> None:
        if self.sweep_line_contains_global_cursor(line):
            self.set_sweep_line_hover_cursor(line)
        else:
            self.restore_sweep_line_hover_cursor(line)


    def sweep_line_contains_global_cursor(self, line: Any) -> bool:
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


    def activate_sweep_line(self, line: Any, event: Any = None) -> None:
        self.active_sweep_line_id = line.sweep_id
        if self.sweep_line_remove_requested(event):
            self.request_sweep_line_removal(line, event)
            if event is not None:
                event.accept()
            return

        self.set_sweep_line_hover_cursor(line)
        if event is not None:
            event.accept()


    def sweep_line_remove_requested(self, event: Any) -> bool:
        if event is None:
            return False

        button = getattr(event, "button", lambda: None)()
        double_clicked = getattr(event, "double", lambda: False)()
        return button == QtCore.Qt.MouseButton.LeftButton and double_clicked


    def request_sweep_line_removal(self, line: Any, event: Any = None) -> None:
        if self.sweep_line_remove_all_requested(event):
            sweep_ids = tuple(sorted(self.sweep_lines.keys()))
        else:
            sweep_ids = (line.sweep_id,)

        self.close_sweeps_requested.emit(self, sweep_ids)


    def sweep_line_remove_all_requested(self, event: Any) -> bool:
        if event is None:
            return False

        modifiers = getattr(event, "modifiers", lambda: QtCore.Qt.KeyboardModifier.NoModifier)()
        return bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)


    def set_sweep_line_index(self, line: Any, index: int, emit: bool = True) -> None:
        axis = self.line_sweep_axis(line)
        count = self.sweep_axis_count(axis)
        if count <= 0:
            return
        index = min(max(int(index), 0), count - 1)

        line.setBounds((
            self.sweep_pixel_centre(axis, 0),
            self.sweep_pixel_centre(axis, count - 1)
            ))
        line.setPos(self.sweep_pixel_centre(axis, index))
        line.sweep_index = index
        self.active_sweep_line_id = line.sweep_id

        if emit:
            self.sweep_moved.emit(line.sweep_id, float(line.value()))


    def _snap_sweep_lines_to_pixel_centres(self) -> None:
        if self._heatmap_geometry() is None:
            return
        for line in self.__dict__.get("sweep_lines", {}).values():
            axis = self.line_sweep_axis(line)
            previous_value = float(line.value())
            index = self.sweep_index_at_value(
                axis,
                previous_value,
                clamp=False,
                )
            set_visible = getattr(line, "setVisible", None)
            if index is None:
                line.sweep_index = None
                if callable(set_visible):
                    set_visible(False)
                continue

            if callable(set_visible):
                set_visible(True)
            snapped_value = self.sweep_pixel_centre(axis, index)
            self.set_sweep_line_index(
                line,
                index,
                emit=not np.isclose(snapped_value, previous_value),
                )


    def move_sweep_with_arrow_key(self, key: QtCore.Qt.Key) -> None:
        if self._heatmap_geometry() is None:
            return
        moves: dict[QtCore.Qt.Key, tuple[_SweepAxis, int]] = {
            QtCore.Qt.Key.Key_Left: ("x", -1),
            QtCore.Qt.Key.Key_Right: ("x", 1),
            QtCore.Qt.Key.Key_Down: ("y", -1),
            QtCore.Qt.Key.Key_Up: ("y", 1),
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


    def sweep_line_index(self, line: Any) -> int | None:
        index = getattr(line, "sweep_index", None)
        if index is not None:
            return index

        axis = self.line_sweep_axis(line)
        return self.sweep_index_at_value(axis, line.value())


    def sweep_line_for_keyboard_move(self, axis: _SweepAxis) -> Any | None:
        matching_lines = [
            line for line in self.sweep_lines.values()
            if self.line_sweep_axis(line) == axis
            ]
        if not matching_lines:
            return None

        active_line = (
            self.sweep_lines.get(self.active_sweep_line_id)
            if self.active_sweep_line_id is not None
            else None
            )
        if active_line in matching_lines:
            return active_line

        return max(matching_lines, key=lambda line: line.sweep_id)


    def sweep_group_drag_requested(self) -> bool:
        if qtw.QApplication.instance() is None:
            return False

        return bool(qtw.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)


    def move_sweep_group(self, dragged_line: Any, dragged_index: int | None) -> None:
        """
        Move all same-orientation sweep lines by the dragged line's index delta.

        """
        axis = self.line_sweep_axis(dragged_line)
        previous_index = self.sweep_line_index(dragged_line)
        if previous_index is None or dragged_index is None:
            return

        requested_delta = dragged_index - previous_index
        try:
            group_lines = [
                line for line in self.sweep_lines.values()
                if self.line_sweep_axis(line) == axis
                ]
        except (AttributeError, RuntimeError):
            # A drag can arrive while a plot is still being initialised or torn
            # down.  Treat the active cursor as a one-line group in that case.
            group_lines = []
        if dragged_line not in group_lines:
            group_lines.append(dragged_line)

        indexed_lines: list[tuple[Any, int]] = []
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


    def bounded_sweep_group_delta(
            self,
            axis: _SweepAxis,
            indexed_lines: list[tuple[Any, int]],
            requested_delta: int,
            ) -> int:
        count = self.sweep_axis_count(axis)
        min_delta = max(-index for _line, index in indexed_lines)
        max_delta = min(count - 1 - index for _line, index in indexed_lines)
        return min(max(requested_delta, min_delta), max_delta)


    @QtCore.pyqtSlot(object)
    def moving_sweep(self, line: Any) -> None:
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
