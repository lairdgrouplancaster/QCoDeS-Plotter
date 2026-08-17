import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6.QtGui import QColor

from .._plot_refresh import plot_refresh_required


def _subplot_axis_order(
        parent_options: dict[str, str],
        source_options: dict[str, str],
        *,
        source_is_cut: bool = False,
        shared_parameter: str | None = None,
        ) -> tuple[str, str] | None:
    """Map source data axes onto a host plot, or reject incompatible axes."""

    if shared_parameter:
        parent_axes = [
            axis for axis in ("x", "y")
            if parent_options.get(axis) == shared_parameter
        ]
        source_axes = [
            axis for axis in ("x", "y")
            if source_options.get(axis) == shared_parameter
        ]
        if len(parent_axes) != 1 or len(source_axes) != 1:
            return None

        parent_axis = parent_axes[0]
        source_axis = source_axes[0]
        # A cut's axis_options['y'] is its fixed setpoint selection, while its
        # axis_data['y'] remains the dependent cut values. It therefore cannot
        # act as the shared coordinate.
        if source_is_cut and source_axis != "x":
            return None
        return ("x", "y") if parent_axis == source_axis else ("y", "x")

    shared_x = parent_options.get("x")
    if not shared_x:
        return None
    if shared_x == source_options.get("x"):
        return "x", "y"
    if not source_is_cut and shared_x == source_options.get("y"):
        return "y", "x"

    return None


def _subplot_shared_parameter(parent) -> str | None:
    """Return the host plot's canonical 1D independent parameter name."""

    default_names = getattr(parent, "_default_plot_axis_names", None)
    if callable(default_names):
        names = default_names()
        if names is not None:
            return names[0]

    independent = tuple(getattr(getattr(parent, "param", None), "depends_on_", ()))
    if len(independent) == 1:
        return str(independent[0])
    return None


class subplot1d(pg.PlotDataItem):
    """
    Class for handling secondary line plots on plot1d
    """
    def __init__(self, parent, from_win, *args, **kargs):
        super().__init__(*args, **kargs)
        
        self.label = from_win.label
        self.param_dict = from_win.param_dict
        self.running = plot_refresh_required(from_win)
        
        self.parent = parent
        self.from_win = from_win
        self._source_consumer_registered = False
        self._source_interval_signal = None
        self._source_interval_slot = None

        self.choose_from: tuple[str, str] | None = None
        self._source_update_signal = getattr(from_win, "trace_updated", None)
        if self._source_update_signal is not None:
            self._source_update_signal.connect(self._source_trace_updated)
        self._source_compatibility_signal = getattr(
            from_win,
            "merge_compatibility_changed",
            None,
            )
        if self._source_compatibility_signal is not None:
            self._source_compatibility_signal.connect(
                self._source_compatibility_changed
                )

        self.refresh()
        
        self.side = "left"
        self.parent.plot.addItem(self)
        self._register_source_consumer()
            
            
    def refresh(self, *, source_ready: bool = False):
        """
        Fetches data from source window and updates view on parent window

        """
        parent = self.parent
        from_win = self.from_win

        self._disconnect_pending_update()

        # Update live state
        self.running = plot_refresh_required(from_win)
        
        # Get which data is on which axis
        parent_options = parent.axis_options
        from_win_options = from_win.axis_options

        self.choose_from = _subplot_axis_order(
            parent_options,
            from_win_options,
            source_is_cut=hasattr(from_win, "sweep_id"),
            shared_parameter=_subplot_shared_parameter(parent),
            )
        if self.choose_from is None:
            self.setData(x=[], y=[])
            return

        # Wait for data to finish
        if from_win.worker.running and not source_ready:
            if self._source_update_signal is None:
                from_win.end_wait.connect(self.call_update)
            return

        self.call_update()


    @QtCore.pyqtSlot()
    def _source_trace_updated(self):
        """Refresh immediately after a cut publishes new, ready-to-use data."""

        self.refresh(source_ready=True)


    @QtCore.pyqtSlot()
    def _source_compatibility_changed(self):
        """Clear old cut data until the changed axes publish replacement data."""

        self._disconnect_pending_update()
        self.choose_from = _subplot_axis_order(
            self.parent.axis_options,
            self.from_win.axis_options,
            source_is_cut=hasattr(self.from_win, "sweep_id"),
            shared_parameter=_subplot_shared_parameter(self.parent),
            )
        self.setData(x=[], y=[])


    def _disconnect_pending_update(self):
        try:
            self.from_win.end_wait.disconnect(self.call_update)
        except (TypeError, RuntimeError):  # Signal was absent or already gone.
            pass


    def _register_source_consumer(self):
        if self._source_consumer_registered:
            return

        source = self.from_win
        source._merged_trace_users = max(
            int(getattr(source, "_merged_trace_users", 0)),
            0,
            ) + 1
        self._source_consumer_registered = True

        if not getattr(source, "visible", True):
            parent_spinbox = getattr(self.parent, "spinBox", None)
            source_spinbox = getattr(source, "spinBox", None)
            if parent_spinbox is not None and source_spinbox is not None:
                source_spinbox.setValue(parent_spinbox.value())
                interval_signal = getattr(parent_spinbox, "valueChanged", None)
                if interval_signal is not None:
                    interval_slot = source_spinbox.setValue
                    interval_signal.connect(interval_slot)
                    self._source_interval_signal = interval_signal
                    self._source_interval_slot = interval_slot

        if (
                getattr(source, "_closed", False)
                and plot_refresh_required(source)
                and not source.monitor.isActive()
                ):
            source.monitorIntervalChanged(source.spinBox.value())


    def _release_source_consumer(self):
        if not self._source_consumer_registered:
            return

        source = self.from_win
        remaining = max(int(getattr(source, "_merged_trace_users", 1)) - 1, 0)
        source._merged_trace_users = remaining
        self._source_consumer_registered = False

        if self._source_interval_signal is not None:
            try:
                self._source_interval_signal.disconnect(self._source_interval_slot)
            except (TypeError, RuntimeError):  # Signal was absent or already gone.
                pass
            self._source_interval_signal = None
            self._source_interval_slot = None

        if remaining == 0 and not getattr(source, "visible", True):
            monitor = getattr(source, "monitor", None)
            if monitor is not None:
                monitor.stop()


    def disconnect_source_updates(self):
        """Release the persistent cut-update connection when a trace is removed."""

        if self._source_update_signal is not None:
            try:
                self._source_update_signal.disconnect(self._source_trace_updated)
            except (TypeError, RuntimeError):  # Signal was absent or already gone.
                pass
            self._source_update_signal = None
        if self._source_compatibility_signal is not None:
            try:
                self._source_compatibility_signal.disconnect(
                    self._source_compatibility_changed
                    )
            except (TypeError, RuntimeError):  # Signal was absent or already gone.
                pass
            self._source_compatibility_signal = None
        self._disconnect_pending_update()
        self._release_source_consumer()
    
    @QtCore.pyqtSlot()
    def call_update(self):
        """
        Event handler for self.refresh/from_win.worker finish
        Updates the subplot line data.

        """
        data = {}

        choose_from = self.choose_from
        if choose_from is None:
            self.setData(x=[], y=[])
            self._disconnect_pending_update()
            return

        # Assign data to correct axis
        for itr, axis in enumerate(["x", "y"]):
            data[axis] = self.from_win.axis_data[choose_from[itr]]
                    
        # Updates display
        self.setData(
            x=data["x"], 
            y=data["y"],
            )
        
        self._disconnect_pending_update()
    
    @QtCore.pyqtSlot(QColor)
    def set_color(self, col):
        """
        Event handler connect to qplot.windows._widgets.dropbox.picker_1d.color_box
        Updates the display color of line based on color_box selection

        Parameters
        ----------
        col : PyQt6.QtGui.QColor
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

    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        self._marquee_owner = None
        self._shift_pan_axis_constraint = True
        self._shift_pan_axis = None
        self._main_moved_axis = None
        self.setAcceptHoverEvents(True)


    def set_marquee_owner(self, owner):
        self._marquee_owner = owner


    def set_shift_pan_axis_constraint(self, enabled):
        """Enable Shift-drag axis locking for panning in this view box."""

        self._shift_pan_axis_constraint = bool(enabled)
        self._shift_pan_axis = None


    def _shift_pan_axis_for_event(self, ev):
        """Return the axis selected by a Shift-pan drag, if applicable."""

        if (
                not self._shift_pan_axis_constraint
                or self.state["mouseMode"] != self.PanMode
                or ev.button() not in {
                    QtCore.Qt.MouseButton.LeftButton,
                    QtCore.Qt.MouseButton.MiddleButton,
                    }
                ):
            return None

        if not ev.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
            self._shift_pan_axis = None
            return None

        if ev.isStart():
            self._shift_pan_axis = None

        if self._shift_pan_axis is None:
            drag = ev.pos() - ev.buttonDownPos(ev.button())
            if drag.x() == 0 and drag.y() == 0:
                return None
            self._shift_pan_axis = 0 if abs(drag.x()) >= abs(drag.y()) else 1

        return self._shift_pan_axis


    def _handle_marquee_mouse_drag(self, ev):
        owner = self._marquee_owner
        if owner is None or ev.button() != QtCore.Qt.MouseButton.LeftButton:
            return False

        if ev.isStart():
            mode = owner.marquee_drag_mode_at(ev.buttonDownScenePos())
            if mode is None and not ev.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier:
                return False

            owner.begin_marquee_drag(
                self.mapSceneToView(ev.buttonDownScenePos()),
                mode,
                )

        if not owner.is_marquee_dragging():
            return False

        self._update_marquee_cursor(ev.scenePos(), ev.modifiers())
        owner.drag_marquee_to(
            self.mapSceneToView(ev.scenePos()),
            ev.modifiers(),
            )
        if ev.isFinish():
            owner.finish_marquee_drag()
            self._update_marquee_cursor(ev.scenePos(), ev.modifiers())

        ev.accept()
        return True


    def _update_marquee_cursor(self, scene_pos, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        owner = self._marquee_owner
        if owner is None:
            self.unsetCursor()
            return

        cursor_shape = owner.marquee_cursor_shape_at(scene_pos, modifiers)
        if cursor_shape is None:
            self.unsetCursor()
        else:
            self.setCursor(cursor_shape)
    

    def hoverMoveEvent(self, ev):
        self._update_marquee_cursor(ev.scenePos(), ev.modifiers())
        super().hoverMoveEvent(ev)


    def hoverLeaveEvent(self, ev):
        owner = self._marquee_owner
        if owner is None or not owner.is_marquee_dragging():
            self.unsetCursor()
        super().hoverLeaveEvent(ev)


    def mouseDoubleClickEvent(self, ev):
        owner = self._marquee_owner
        if owner is not None and getattr(owner, "marquee", None) is not None:
            owner.clear_marquee()
            self.unsetCursor()
            ev.accept()
            return

        super().mouseDoubleClickEvent(ev)


    def mouseClickEvent(self, ev):
        owner = self._marquee_owner
        if (
                owner is not None
                and ev.button() == QtCore.Qt.MouseButton.RightButton
                and owner.open_marquee_context_menu(
                    ev.scenePos(),
                    self._mouse_event_global_pos(ev),
                    )
                ):
            ev.accept()
            return

        super().mouseClickEvent(ev)


    def _mouse_event_global_pos(self, ev):
        for attr_name in ("screenPos", "globalPos"):
            attr = getattr(ev, attr_name, None)
            if attr is None:
                continue
            pos = attr() if callable(attr) else attr
            if isinstance(pos, QtCore.QPointF):
                return pos.toPoint()
            if isinstance(pos, QtCore.QPoint):
                return pos

        return None


    def mouseDragEvent(self, ev, axis=None):
        if axis is None and self._handle_marquee_mouse_drag(ev):
            return

        constrained_axis = axis
        if axis is None:
            constrained_axis = self._shift_pan_axis_for_event(ev)

        super().mouseDragEvent(ev, axis=constrained_axis)

        if axis is None:
            self._main_moved_axis = constrained_axis
            try:
                self.main_moved.emit(ev)
            finally:
                self._main_moved_axis = None

        if ev.isFinish():
            self._shift_pan_axis = None
         
    def wheelEvent(self, ev, axis=None):
        super().wheelEvent(ev, axis=axis)
        
        if axis is None:
            self.main_moved.emit(ev)
       
    def autoRange(self, padding=None, items=None, item=None):
        super().autoRange(padding=padding, items=items, item=item)
        
        self.autoRange_triggered.emit()
