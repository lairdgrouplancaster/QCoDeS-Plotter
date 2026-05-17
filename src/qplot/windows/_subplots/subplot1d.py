import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6.QtGui import QColor


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
        
        # Get which data is on which axis
        parent_options = parent.axis_options
        from_win_options = from_win.axis_options

        # for subplot, must share 1 axis parameter name, so check if flipped
        if parent_options["x"] == from_win_options["x"] or parent_options["y"] == from_win_options["y"]:
            self.choose_from = ["x", "y"]
        else:
            self.choose_from = ["y", "x"]
            
        # Wait for data to finish
        if from_win.worker.running:
            from_win.end_wait.connect(self.call_update)
        else:
            self.call_update()
            
    
    @QtCore.pyqtSlot()
    def call_update(self):
        """
        Event handler for self.refresh/from_win.worker finish
        Updates the subplot line data.

        """
        data = {}
    
        # Assign data to correct axis
        for itr, axis in enumerate(["x", "y"]):
            data[axis] = self.from_win.axis_data[self.choose_from[itr]]
                    
        # Updates display
        self.setData(
            x=data["x"], 
            y=data["y"],
            )
        
        try:
            self.from_win.end_wait.disconnect(self.call_update)
        except TypeError: # Type error if not connected
            pass
    
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
        self.setAcceptHoverEvents(True)


    def set_marquee_owner(self, owner):
        self._marquee_owner = owner


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
