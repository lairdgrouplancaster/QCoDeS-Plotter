from typing import TYPE_CHECKING

from math import isclose, isfinite, log10
from os import path
from time import perf_counter

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui
from PyQt5.QtGui import QKeySequence

import pyqtgraph as pg
from pyqtgraph.graphicsItems.ViewBox import axisCtrlTemplate_generic

from qcodes.dataset.sqlite.database import get_DB_location

from qplot.tools import (
    unpack_param,
    loader,
    )
from qplot.datahandling import load_param_data_from_db_prep
from qplot.datahandling.qcodes_cache import (
    cache_has_no_written_data,
    set_parameter_complete,
    update_cache_parameter_data,
    )
    
from ._subplots import custom_viewbox
from ._widgets import (
    expandingComboBox,
    QDock_context,
    operations_widget,
    )
from ._shortcuts import standard_key_sequences
from ._help import add_help_menu
from ._dragdrop import (
    preview_drop_is_compatible,
    run_preview_payload_from_mime,
    )
from ._window_controls import (
    add_confirmation_options,
    add_standard_window_controls,
    )
from qplot.diagnostics import log_exception, log_user_error

if TYPE_CHECKING:
    import qplot
    import qcodes


def _axis_scale_power_text(scale):
    """
    Return a compact HTML power-of-ten label for an axis display scale.

    """
    if not isfinite(scale) or scale <= 0 or isclose(scale, 1.0):
        return ""

    exponent = round(log10(scale))
    if isclose(scale, 10**exponent, rel_tol=1e-9, abs_tol=0.0):
        return f"10<sup>{exponent}</sup>"

    return f"{scale:g}"


class _PowerScaledAxisItem(pg.AxisItem):
    """
    Display pyqtgraph's auto SI scaling as powers of ten in the axis unit.

    """

    def labelString(self) -> str:
        if self.autoSIPrefix and not isclose(self.autoSIPrefixScale, 1.0):
            unit_scale = 1.0 / self.autoSIPrefixScale
        else:
            unit_scale = 1.0

        scale_text = _axis_scale_power_text(unit_scale)
        if self.labelUnits == "":
            units = f"({scale_text})" if scale_text else ""
        elif scale_text:
            units = f"({scale_text} {self.labelUnits})"
        else:
            units = f"({self.labelUnitPrefix}{self.labelUnits})"

        text = f"{self.labelText} {units}"
        style = ";".join([f"{k}: {self.labelStyle[k]}" for k in self.labelStyle])
        return f"<span style='{style}'>{text}</span>"


class plotWidget(qtw.QMainWindow):
    """
    Base class for plot1d and plot2d.
    Controls common setup and functions for both windows.
    
    
    Refresh overview:
    > Refresh monitor set at Main window time or 5s if none.
    > On monitor timeout, calls self.refreshWindow() to check if refresh is needed
    > Then produce worker for thread in self.load_data(). And queues to available
      thread in self.threadPool.
      See qplot.tools.worker.loader for more detail.
    > Worker loads from SQL database inside worker and handles data to usable
      form. See qplot.datahandling.LoadFromDB for more detail.
    > On worker finish, worker callback to plot which calls self.refreshPlot().
      plotWidget.refreshPlot() fetches data from worker, plot<1/2>d.refreshPlot()
      then inherits, handles data, and renders as needed.  
    """
    
    closed = QtCore.pyqtSignal([object])
    end_wait = QtCore.pyqtSignal()
    make_ds = QtCore.pyqtSignal([str])
    previewTraceDropRequested = QtCore.pyqtSignal(object, str, str)
    
    _label_width = 95 #About the size of 3 s.f. scientific
    _toggle_shortcuts = {
        "Refresh Timer": "Ctrl+Alt+R",
        "Co-ordinates": "Ctrl+Alt+C",
        "Line control": "Ctrl+Alt+A",
        "Operations": "Ctrl+Alt+O",
    }
    
    def __init__(self, 
                 guid : str, 
                 param : "qcodes.dataset.ParamSpec",
                 config : "qplot.configuration.config.config",
                 threadPool : "QtCore.QThreadPool",
                 dataset_holder : dict,
                 refrate : float=None,
                 show : bool=True
                 ):
        """
        Initialises window and sets up all required widgets. Also calls functions
        for static plotting and checks for live plotting.

        Parameters
        ----------
        guid : str
            The guid of dataset which contains the data to be plotted.
        param : qcodes.dataset.ParamSpec
            Which parameter within dataset to plot.
        config : qplot.configuration.config.config
            Holds configuration data, mainly theme and window size.
        threadPool : PyQt5.QtCore.QThreadPool
            A pool of threads for the refresh worker to be placed in.
        refrate : float, optional
            Default value for the refresh timer. The default is None, which 
            corresponds to a 5.0s refresh time.
        show : bool, optional
            Whether to display the window or not. The default is True.
            When false reduces produced widgets to reduce workload.

        """
        super().__init__()
        
        ### CORE VARIABLES
        self._dataset_holder = dataset_holder
        self._guid = guid
        self.param = param
        if not hasattr(self.param, "_complete"): # Add completed load track
            set_parameter_complete(self.param, False)
        self.name = str(self)
        self.label = f"ID:{self.ds.run_id} {self.param.name}"
        self.monitor = QtCore.QTimer()
        self.threadPool = threadPool
        self.last_ds_len = self.ds.number_of_results
        self.config = config
        self.visible = show
        self.operations = {}
        self._last_error_text = None
        self.show_status("Working, please wait", 0)
        
        ### WIDGETS
        self.layout = qtw.QVBoxLayout()
        
        self.widget = pg.GraphicsLayoutWidget()
        self._install_preview_drop_target()
        # Overwrite default viewbox to give more flexibility
        self.vb = custom_viewbox() # Mainly for linking secondary axis
        self.vb.setDefaultPadding(0)
        self.plot = self.widget.addPlot(
            viewBox=self.vb,
            axisItems={
                "bottom": _PowerScaledAxisItem("bottom"),
                "left": _PowerScaledAxisItem("left"),
                },
            )
        self.vb.setParent(self.plot)
        self.vb.set_marquee_owner(self)
        self._init_marquee()
        self.layout.addWidget(self.widget)
        
        ### CORE INIT FUNCTIONS
        self.initAxes()
        self.initOperations()
        self.initRefresh(refrate)
        self.initFrame() # See plot1d, plot2d
        
        if self.visible: #dont run non essential GUI functions if not displaying
            self.initLabels()
            self.initContextMenu()
            self.initMenu()
            
            ### FORMATING
            self.setWindowTitle(str(self))
            
            self.plot.showAxis("right")
            self.plot.showAxis("top")
            
            self.plot.getAxis('top').setStyle(showValues=False)
            self.plot.getAxis('right').setStyle(showValues=False)
            
            screenrect = qtw.QApplication.primaryScreen().availableGeometry()
            sizeFrac = self.config.get("GUI.plot_frame_fraction")
    
            self.width = int(sizeFrac * screenrect.width())
            self.height = int(sizeFrac * screenrect.height())
            self.resize(self.width, self.height)
            
            w = qtw.QFrame()
            w.setLayout(self.layout)
            self.setCentralWidget(w)
        
        #start refresh cycle if live
        if self.ds.running: 
            self.monitor.start((int(self.spinBox.value() * 1000)))


    def _install_preview_drop_target(self):
        self.setAcceptDrops(True)
        self.widget.setAcceptDrops(True)
        self.widget.installEventFilter(self)

        viewport = self.widget.viewport() if hasattr(self.widget, "viewport") else None
        if viewport is not None:
            viewport.setAcceptDrops(True)
            viewport.installEventFilter(self)


    def _set_param_axis_label(self, axis, param):
        self.plot.setLabel(axis=axis, text=param.label, units=param.unit)


    def _set_param_axis_labels(self):
        self._set_param_axis_label("bottom", self.axis_param["x"])
        self._set_param_axis_label("left", self.axis_param["y"])


    def eventFilter(self, source, event):
        if event.type() in (
            QtCore.QEvent.DragEnter,
            QtCore.QEvent.DragMove,
            QtCore.QEvent.Drop,
            ):
            if self._handle_preview_drag_drop(event):
                return True

        return super().eventFilter(source, event)


    def dragEnterEvent(self, event):
        if self._handle_preview_drag_drop(event):
            return
        super().dragEnterEvent(event)


    def dragMoveEvent(self, event):
        if self._handle_preview_drag_drop(event):
            return
        super().dragMoveEvent(event)


    def dropEvent(self, event):
        if self._handle_preview_drag_drop(event):
            return
        super().dropEvent(event)


    def _handle_preview_drag_drop(self, event):
        payload = run_preview_payload_from_mime(event.mimeData())
        if payload is None:
            return False

        if not self.accepts_preview_trace_drop(payload):
            event.ignore()
            return True

        if event.type() == QtCore.QEvent.Drop:
            event.setDropAction(QtCore.Qt.CopyAction)
            event.accept()
            self.previewTraceDropRequested.emit(
                self,
                payload["guid"],
                payload["parameter"]
                )
            return True

        event.acceptProposedAction()
        return True


    def accepts_preview_trace_drop(self, payload):
        if not hasattr(self, "option_boxes"):
            return False

        return preview_drop_is_compatible(
            getattr(self.param, "depends_on_", ()),
            payload
            )


    def _init_marquee(self):
        """
        Create the reusable marquee graphics shown after Alt-dragging.

        """
        self.marquee = None
        self._marquee_drag_state = None

        self.marquee_highlight = qtw.QGraphicsRectItem()
        highlight_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 135))
        highlight_pen.setWidthF(3)
        highlight_pen.setCosmetic(True)
        self.marquee_highlight.setPen(highlight_pen)
        self.marquee_highlight.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self.marquee_highlight.setZValue(18)
        self.marquee_highlight.hide()
        self.marquee_highlight.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.plot.addItem(self.marquee_highlight)

        self.marquee_outline = qtw.QGraphicsRectItem()
        pen = QtGui.QPen(QtGui.QColor(65, 65, 65, 220))
        pen.setWidthF(1.2)
        pen.setCosmetic(True)
        pen.setStyle(QtCore.Qt.DashLine)
        self.marquee_outline.setPen(pen)
        self.marquee_outline.setBrush(QtGui.QBrush(QtGui.QColor(40, 40, 40, 24)))
        self.marquee_outline.setZValue(19)
        self.marquee_outline.hide()
        self.marquee_outline.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.plot.addItem(self.marquee_outline)

        self.marquee_handles = pg.ScatterPlotItem(
            symbol="s",
            size=6,
            pen=pg.mkPen((20, 20, 20, 230), width=1, cosmetic=True),
            brush=pg.mkBrush(245, 245, 245, 230),
            )
        self.marquee_handles.setZValue(20)
        self.marquee_handles.hide()
        self.marquee_handles.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.plot.addItem(self.marquee_handles)


    def is_marquee_dragging(self):
        return self._marquee_drag_state is not None


    def current_marquee_drag_mode(self):
        if self._marquee_drag_state is None:
            return None

        return self._marquee_drag_state["mode"]


    def begin_marquee_drag(self, start, mode=None):
        """
        Start creating or resizing a marquee in plot coordinates.

        """
        start = QtCore.QPointF(start)
        if mode is None:
            mode = "new"

        handle_offset = QtCore.QPointF()
        if mode != "new" and self.marquee is not None:
            handle_point = self._marquee_handle_points_for_rect(self.marquee)[mode]
            handle_offset = start - handle_point

        self._marquee_drag_state = {
            "anchor": QtCore.QPointF(start),
            "handle_offset": QtCore.QPointF(handle_offset),
            "mode": mode,
            "rect": QtCore.QRectF(self.marquee) if self.marquee is not None else None,
            }

        if mode == "new":
            self.set_marquee_rect(QtCore.QRectF(start, start))


    def drag_marquee_to(self, point, modifiers=QtCore.Qt.NoModifier):
        """
        Update the marquee during an active drag.

        """
        point = QtCore.QPointF(point)
        state = self._marquee_drag_state
        if state is None:
            return

        if state["mode"] == "new" or state["rect"] is None:
            rect = QtCore.QRectF(state["anchor"], point)
        else:
            rect = QtCore.QRectF(state["rect"])
            offset = state.get("handle_offset", QtCore.QPointF())
            point = QtCore.QPointF(
                point.x() - offset.x(),
                point.y() - offset.y(),
                )
            self._resize_marquee_rect(rect, state["mode"], point, modifiers)

        self.set_marquee_rect(rect)


    def finish_marquee_drag(self):
        self._marquee_drag_state = None


    def marquee_contains_scene_pos(self, scene_pos):
        """
        Return whether a scene position is inside the current marquee.

        """
        if self.marquee is None:
            return False

        point = self.plot.vb.mapSceneToView(scene_pos)
        return self.marquee.normalized().contains(point)


    def open_marquee_context_menu(self, scene_pos, global_pos=None):
        """
        Open the marquee context menu when a right-click lands inside it.

        """
        if not self.marquee_contains_scene_pos(scene_pos):
            return False

        menu = self._new_marquee_context_menu()
        if global_pos is None:
            global_pos = QtGui.QCursor.pos()
        elif isinstance(global_pos, QtCore.QPointF):
            global_pos = global_pos.toPoint()

        menu.exec_(global_pos)
        return True


    def _new_marquee_context_menu(self):
        menu = qtw.QMenu()
        if hasattr(menu, "setToolTipsVisible"):
            menu.setToolTipsVisible(True)

        self._add_marquee_context_action(
            menu,
            "Zoom",
            lambda: self.zoom_marquee("xy"),
            )
        self._add_marquee_context_action(
            menu,
            "Zoom X",
            lambda: self.zoom_marquee("x"),
            )
        self._add_marquee_context_action(
            menu,
            "Zoom Y",
            lambda: self.zoom_marquee("y"),
            )
        self._add_marquee_color_context_action(menu)
        self._add_marquee_stats_context_action(menu)
        return menu


    def _add_marquee_context_action(self, menu, text, callback):
        action = menu.addAction(text)
        action.triggered.connect(
            lambda _checked=False, callback=callback: self._execute_marquee_action(callback)
            )
        return action


    def _add_marquee_color_context_action(self, menu):
        return None


    def _add_marquee_stats_context_action(self, menu):
        stats_text = self._marquee_stats_text()
        action = self._add_marquee_context_action(
            menu,
            "Stats...",
            lambda stats_text=stats_text: self.show_marquee_stats_dialog(stats_text),
            )
        if stats_text is None:
            action.setEnabled(False)
            action.setToolTip("No data points inside the marquee.")
        else:
            action.setToolTip("Show statistics for the marquee selection.")
            action.setStatusTip("Show statistics for the marquee selection.")
        return action


    def _execute_marquee_action(self, callback):
        if callback():
            self.clear_marquee()


    def zoom_marquee(self, axes):
        rect = self.marquee.normalized() if self.marquee is not None else None
        if rect is None:
            return False

        if "x" in axes:
            self.vb.setXRange(rect.left(), rect.right(), padding=0)
        if "y" in axes:
            self.vb.setYRange(rect.top(), rect.bottom(), padding=0)
        return True


    def _marquee_stats_text(self):
        return None


    def show_marquee_stats_dialog(self, stats_text=None):
        if stats_text is None:
            stats_text = self._marquee_stats_text()
        if stats_text is None:
            return False

        dialog = self._new_marquee_stats_dialog(stats_text)
        self._marquee_stats_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True


    def _new_marquee_stats_dialog(self, stats_text):
        dialog = qtw.QDialog(self)
        dialog.setWindowTitle("Marquee stats")

        layout = qtw.QVBoxLayout(dialog)
        stats_view = qtw.QPlainTextEdit(stats_text)
        stats_view.setReadOnly(True)
        stats_view.setMinimumWidth(280)
        stats_view.setMinimumHeight(170)
        layout.addWidget(stats_view)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.Close)
        copy_button = buttons.addButton("Copy", qtw.QDialogButtonBox.ActionRole)

        def copy_stats(_checked=False):
            self.copy_marquee_stats_to_clipboard(stats_text)

        copy_button.clicked.connect(copy_stats)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)

        return dialog


    def _format_marquee_stats_text(self, size_text, values, rect=None):
        if rect is None and self.__dict__.get("marquee") is not None:
            rect = self.marquee.normalized()

        lines = [size_text]
        if rect is not None:
            lines.extend((
                f"X range: {self.formatNum(rect.left())} to {self.formatNum(rect.right())}",
                f"Y range: {self.formatNum(rect.top())} to {self.formatNum(rect.bottom())}",
                ))

        lines.extend((
            f"Average: {self.formatNum(float(values.mean()))}",
            f"Standard deviation: {self.formatNum(float(values.std()))}",
            f"Max: {self.formatNum(float(values.max()))}",
            f"Min: {self.formatNum(float(values.min()))}",
            ))

        return "\n".join(lines)


    def copy_marquee_stats_to_clipboard(self, stats_text=None):
        if stats_text is None:
            stats_text = self._marquee_stats_text()
        if stats_text is None:
            return False

        clipboard = qtw.QApplication.clipboard()
        if clipboard is None:
            return False

        clipboard.setText(stats_text)
        return True


    def _resize_marquee_rect(self, rect, handle, point, modifiers=QtCore.Qt.NoModifier):
        symmetric = bool(modifiers & QtCore.Qt.AltModifier)
        asymmetric = bool(modifiers & QtCore.Qt.ShiftModifier) and not symmetric
        original = QtCore.QRectF(rect)
        anchor = self._marquee_handle_points_for_rect(original)[handle]

        if "w" in handle:
            rect.setLeft(point.x())
        if "e" in handle:
            rect.setRight(point.x())
        if "s" in handle:
            rect.setTop(point.y())
        if "n" in handle:
            rect.setBottom(point.y())

        dx = point.x() - anchor.x()
        dy = point.y() - anchor.y()

        if ("w" in handle or "e" in handle) and (symmetric or asymmetric):
            offset = -dx if symmetric else dx if asymmetric else None
            if "w" in handle:
                rect.setRight(original.right() + offset)
            else:
                rect.setLeft(original.left() + offset)

        if ("n" in handle or "s" in handle) and (symmetric or asymmetric):
            offset = -dy if symmetric else dy if asymmetric else None
            if "s" in handle:
                rect.setBottom(original.bottom() + offset)
            else:
                rect.setTop(original.top() + offset)

        if asymmetric:
            self._snap_translated_marquee_rect(rect, original, handle)


    def _snap_translated_marquee_rect(self, rect, original, handle):
        snapped = self._snap_marquee_rect(QtCore.QRectF(rect).normalized())
        if snapped is None:
            return

        adjusted = QtCore.QRectF(snapped)
        if "w" in handle or "e" in handle:
            width = original.width()
            if "w" in handle:
                adjusted.setLeft(snapped.left())
                adjusted.setRight(snapped.left() + width)
            else:
                adjusted.setRight(snapped.right())
                adjusted.setLeft(snapped.right() - width)

        if "n" in handle or "s" in handle:
            height = original.height()
            if "s" in handle:
                adjusted.setTop(snapped.top())
                adjusted.setBottom(snapped.top() + height)
            else:
                adjusted.setBottom(snapped.bottom())
                adjusted.setTop(snapped.bottom() - height)

        rect.setRect(adjusted.left(), adjusted.top(), adjusted.width(), adjusted.height())


    def set_marquee_rect(self, rect):
        """
        Snap, store, and draw the marquee rectangle.

        """
        rect = self._snap_marquee_rect(rect.normalized())
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            self.clear_marquee()
            return

        self.marquee = QtCore.QRectF(rect)
        self.marquee_highlight.setRect(rect)
        self.marquee_outline.setRect(rect)
        self.marquee_highlight.show()
        self.marquee_outline.show()
        self._update_marquee_handles()


    def clear_marquee(self):
        self.marquee = None
        self.marquee_highlight.hide()
        self.marquee_outline.hide()
        self.marquee_handles.hide()


    def _snap_marquee_rect(self, rect):
        return rect


    def marquee_drag_mode_at(self, scene_pos):
        """
        Return the resize handle under a scene position, if there is one.

        """
        if self.marquee is None or not self.marquee_handles.isVisible():
            return None

        threshold = 8
        for handle, point in self._marquee_handle_points().items():
            handle_scene_pos = self.plot.vb.mapViewToScene(point)
            distance = (
                (handle_scene_pos.x() - scene_pos.x()) ** 2
                + (handle_scene_pos.y() - scene_pos.y()) ** 2
                )
            if distance <= threshold ** 2:
                return handle

        return None


    def marquee_cursor_shape_at(self, scene_pos, modifiers=QtCore.Qt.NoModifier):
        mode = self.current_marquee_drag_mode()
        if mode == "new":
            return QtCore.Qt.CrossCursor
        if mode is not None:
            return self._marquee_cursor_shape_for_handle(mode)

        handle = self.marquee_drag_mode_at(scene_pos)
        if handle is not None:
            return self._marquee_cursor_shape_for_handle(handle)
        if modifiers & QtCore.Qt.AltModifier:
            return QtCore.Qt.CrossCursor

        return None


    def _marquee_cursor_shape_for_handle(self, handle):
        if handle in ("e", "w"):
            return QtCore.Qt.SizeHorCursor
        if handle in ("n", "s"):
            return QtCore.Qt.SizeVerCursor
        if handle in ("nw", "se"):
            return QtCore.Qt.SizeFDiagCursor
        if handle in ("ne", "sw"):
            return QtCore.Qt.SizeBDiagCursor

        return QtCore.Qt.CrossCursor


    def _update_marquee_handles(self):
        points = list(self._marquee_handle_points().values())
        self.marquee_handles.setData(
            [point.x() for point in points],
            [point.y() for point in points],
            )
        self.marquee_handles.show()


    def _marquee_handle_points(self):
        return self._marquee_handle_points_for_rect(self.marquee)


    def _marquee_handle_points_for_rect(self, rect):
        centre_x = rect.center().x()
        centre_y = rect.center().y()
        return {
            "nw": QtCore.QPointF(rect.left(), rect.bottom()),
            "n": QtCore.QPointF(centre_x, rect.bottom()),
            "ne": QtCore.QPointF(rect.right(), rect.bottom()),
            "e": QtCore.QPointF(rect.right(), centre_y),
            "se": QtCore.QPointF(rect.right(), rect.top()),
            "s": QtCore.QPointF(centre_x, rect.top()),
            "sw": QtCore.QPointF(rect.left(), rect.top()),
            "w": QtCore.QPointF(rect.left(), centre_y),
            }
        
        
    def __str__(self):
        filenameStr = path.basename(get_DB_location())
        fstr = (f"{filenameStr} | " 
                f"Run ID: {self.ds.run_id} | "
                f"{self.param.name} ({self.param.label})"
                )
        return fstr

    
    def show_status(self, message : str, timeout : int = 5000):
        """
        Shows a short message in the plot window status bar.

        """
        if getattr(self, "visible", False):
            self.statusBar().showMessage(message, timeout)


    def show_error(self, title : str, message : str, details : str = None):
        """
        Shows an error both in the status bar and, for visible windows, in a
        message box.

        """
        log_user_error(title, message, details, __name__)
        self.show_status(message, 10000)

        if not self.visible:
            return

        box = qtw.QMessageBox(qtw.QMessageBox.Warning, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec_()


    def register_shortcut(self, action, shortcut, status_tip : str = None):
        """
        Registers a QAction shortcut on the plot window.

        """
        if isinstance(shortcut, (list, tuple)):
            action.setShortcuts(shortcut)
            shortcut_text = shortcut[0].toString(QKeySequence.NativeText)
        else:
            action.setShortcut(shortcut)
            shortcut_text = QKeySequence(shortcut).toString(QKeySequence.NativeText)
        action.setShortcutContext(QtCore.Qt.WindowShortcut)
        if hasattr(action, "setShortcutVisibleInContextMenu"):
            action.setShortcutVisibleInContextMenu(True)
        if status_tip:
            action.setStatusTip(status_tip)
            action.setToolTip(f"{status_tip} ({shortcut_text})")
        if action not in self.actions():
            self.addAction(action)


    @property
    def ds(self):
        """
        Returns the window's dataset from the dictionary of stored datasets

        Returns
        -------
        qcodes.dataset.data_set.dataset

        """
        # Check dataset exists, produce new one if needed.
        if self._dataset_holder.get(self._guid, 0) == 0:
            self.show_status(f"Dataset {self._guid} not found. Reloading...", 5000)
            self.make_ds.emit(self._guid)
        
        # Check a deletion timer is not active and stop
        elif self._dataset_holder[self._guid]["del_timer"] is not None:
            self._dataset_holder[self._guid]["del_timer"].stop() # Stop delete timer
            self._dataset_holder[self._guid]["del_timer"] = None
            
        return self._dataset_holder[self._guid]["dataset"]
        
###############################################################################
# Init functions   
    
    def initRefresh(self, refrate : float):
        """
        Sets up refresh logic and widgets. Along with top toolbar

        Parameters
        ----------
        refrate : float
            Default value for the refresh timer.

        """
        self.toolbarRef = qtw.QToolBar("Refresh Timer")
        self.addToolBar(QtCore.Qt.TopToolBarArea, self.toolbarRef)
        
        if not self.ds.running:
            self.toolbarRef.hide()
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)

        self.toolbarRef.addWidget(qtw.QLabel("Refresh interval (s): "))
        self.toolbarRef.addWidget(self.spinBox)
        
        if refrate is not None and refrate > 0:
            self.spinBox.setValue(refrate)
        else:
            self.spinBox.setValue(self.config.get("user_preference.default_refresh_rate"))
            
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshWindow)
            
        
    def initLabels(self):
        """
        Sets up bottom toolbar which displays cursor point.

        """
        self.toolbarCo_ord = qtw.QToolBar("Co-ordinates")
        self.addToolBar(QtCore.Qt.BottomToolBarArea, self.toolbarCo_ord)
        
        labelWidth = self._label_width #About the size of 3 s.f. scientific
        self.pos_labels = {}
        
        posLabelx = qtw.QLabel("x= ")
        posLabelx.setMinimumWidth(labelWidth)
        self.toolbarCo_ord.addWidget(posLabelx)
        self.pos_labels["x"] = posLabelx
        
        posLabely = qtw.QLabel("y= ")
        posLabely.setMinimumWidth(labelWidth)
        self.toolbarCo_ord.addWidget(posLabely)
        self.pos_labels["y"] = posLabely
        
        self.toolbarCo_ord.addWidget(qtw.QLabel("  "))
        
        self.plot.scene().sigMouseMoved.connect(self.mouseMoved)
    
    
    def initContextMenu(self):
        """
        Adjusts the default plot context menu.

        """
        self.vbMenu = self.vb.menu
        self.mouseModeAction = self._context_menu_action("Mouse Mode")
        self._remove_scene_export_context_menu()
        if getattr(self.plot, "ctrlMenu", None) is not None:
            self.plot.ctrlMenu.setTitle("Options")
            self.plot.ctrlMenu.menuAction().setText("Options")

        self.exportPlotAction = qtw.QAction("&Export Plot...", self)
        self.register_shortcut(
            self.exportPlotAction,
            "Ctrl+E",
            "Open the plot export dialog",
            )
        self.exportPlotAction.triggered.connect(self.open_export_dialog)

        contextAction = qtw.QAction("Show Context Menu", self)
        self.register_shortcut(contextAction, "Shift+F10", "Show plot context menu")
        contextAction.triggered.connect(self.open_context_menu)
        
        actions = self.vbMenu.actions()
        for action in actions:
            if action.text() == "View All":
                action.setText("Autoscale")
                self.register_shortcut(action, "Ctrl+0", "Autoscale plot")
                break
        
        x_action = actions[1]
        
        self.autoscaleSep = self.vbMenu.insertSeparator(x_action)
        
        # Create visibility
        toggleAction = qtw.QAction("View Operations", self, checkable=True)
        self.register_shortcut(toggleAction, "Ctrl+Shift+O", "Toggle operations panel")
        toggleAction.triggered.connect(self.oper_dock.setVisible)
        self.oper_dock.visibilityChanged.connect(toggleAction.setChecked)
        self.vbMenu.insertAction(x_action, toggleAction)
        self.vbMenu.insertSeparator(x_action)

        self._init_axis_scale_dialogs()


    def _init_axis_scale_dialogs(self):
        """
        Move pyqtgraph's X/Y axis scaling controls into double-click dialogs.

        """
        self._axis_scale_controls = {}
        self._axis_scale_dialogs = {}

        for axis, menu_text in (("x", "X axis"), ("y", "Y axis")):
            action = self._context_menu_action(menu_text)
            if action is None or action.menu() is None:
                continue

            self.vbMenu.removeAction(action)

        self._install_axis_scale_double_click_handlers()


    def _menu_control_widget(self, menu):
        """
        Returns the embedded control widget from a QWidgetAction menu.

        """
        for action in menu.actions():
            if isinstance(action, qtw.QWidgetAction):
                return action.defaultWidget()
        return None


    def _install_axis_scale_double_click_handlers(self):
        """
        Open the relevant axis scale dialog when an axis is double-clicked.

        """
        for axis, side in (("x", "bottom"), ("y", "left")):
            axis_item = self.plot.getAxis(side)
            if axis_item is None:
                continue

            previous_handler = getattr(axis_item, "mouseDoubleClickEvent", None)

            def mouse_double_click(event, axis=axis, previous_handler=previous_handler):
                if event.button() == QtCore.Qt.LeftButton:
                    self.open_axis_scale_dialog(axis)
                    event.accept()
                    return

                if previous_handler is not None:
                    previous_handler(event)

            axis_item.mouseDoubleClickEvent = mouse_double_click


    def _axis_scale_dialog_title(self, axis):
        return f"{axis.upper()} axis scaling"


    def _axis_scale_axis_number(self, axis):
        return 0 if axis == "x" else 1


    def _axis_scale_axis_constant(self, axis):
        return pg.ViewBox.XAxis if axis == "x" else pg.ViewBox.YAxis


    def _new_axis_scale_controls(self, axis):
        """
        Build a fresh copy of pyqtgraph's axis scaling controls for a dialog.

        """
        widget = qtw.QWidget()
        ui = axisCtrlTemplate_generic.Ui_Form()
        ui.setupUi(widget)
        self._axis_scale_controls[axis] = ui

        ui.mouseCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_mouse_toggled(axis, checked)
            )
        ui.manualRadio.clicked.connect(
            lambda _checked=False, axis=axis: self._axis_scale_manual_clicked(axis)
            )
        ui.minText.editingFinished.connect(
            lambda axis=axis: self._axis_scale_range_text_changed(axis)
            )
        ui.maxText.editingFinished.connect(
            lambda axis=axis: self._axis_scale_range_text_changed(axis)
            )
        ui.autoRadio.clicked.connect(
            lambda _checked=False, axis=axis: self._axis_scale_auto_clicked(axis)
            )
        ui.autoPercentSpin.valueChanged.connect(
            lambda value, axis=axis: self._axis_scale_auto_spin_changed(axis, value)
            )
        ui.linkCombo.currentIndexChanged.connect(
            lambda _index, axis=axis: self._axis_scale_link_changed(axis)
            )
        ui.autoPanCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_auto_pan_toggled(axis, checked)
            )
        ui.visibleOnlyCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_visible_only_toggled(axis, checked)
            )
        ui.invertCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_invert_toggled(axis, checked)
            )

        return widget


    def _sync_axis_scale_controls(self, axis):
        """
        Update a dialog's controls from the current view state.

        """
        ui = self._axis_scale_controls.get(axis)
        if ui is None:
            return

        axis_number = self._axis_scale_axis_number(axis)
        state = self.vb.getState(copy=False)

        for widget in (
                ui.minText,
                ui.maxText,
                ui.manualRadio,
                ui.autoRadio,
                ui.autoPercentSpin,
                ui.linkCombo,
                ui.autoPanCheck,
                ui.visibleOnlyCheck,
                ui.invertCheck,
                ui.mouseCheck,
                ):
            widget.blockSignals(True)

        try:
            target_range = state["targetRange"][axis_number]
            ui.minText.setText("%0.5g" % target_range[0])
            ui.maxText.setText("%0.5g" % target_range[1])

            auto_range = state["autoRange"][axis_number]
            ui.autoRadio.setChecked(auto_range is not False)
            ui.manualRadio.setChecked(auto_range is False)
            if auto_range is not False and auto_range is not True:
                ui.autoPercentSpin.setValue(int(auto_range * 100))

            ui.mouseCheck.setChecked(state["mouseEnabled"][axis_number])
            ui.autoPanCheck.setChecked(state["autoPan"][axis_number])
            ui.visibleOnlyCheck.setChecked(state["autoVisibleOnly"][axis_number])
            ui.invertCheck.setChecked(state.get(axis + "Inverted", False))
            self._sync_axis_scale_link_combo(axis)
        finally:
            for widget in (
                    ui.minText,
                    ui.maxText,
                    ui.manualRadio,
                    ui.autoRadio,
                    ui.autoPercentSpin,
                    ui.linkCombo,
                    ui.autoPanCheck,
                    ui.visibleOnlyCheck,
                    ui.invertCheck,
                    ui.mouseCheck,
                    ):
                widget.blockSignals(False)


    def _sync_axis_scale_link_combo(self, axis):
        """
        Mirror pyqtgraph's available linked views into the dialog link combo.

        """
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        source_combo = self.vbMenu.ctrl[axis_number].linkCombo
        current = self.vb.getState(copy=False)["linkedViews"][axis_number] or ""

        ui.linkCombo.clear()
        for index in range(source_combo.count()):
            ui.linkCombo.addItem(source_combo.itemText(index))

        index = ui.linkCombo.findText(current)
        ui.linkCombo.setCurrentIndex(max(index, 0))


    def _axis_scale_mouse_toggled(self, axis, checked):
        if axis == "x":
            self.vb.setMouseEnabled(x=checked)
        else:
            self.vb.setMouseEnabled(y=checked)


    def _axis_scale_manual_clicked(self, axis):
        self.vb.enableAutoRange(self._axis_scale_axis_constant(axis), False)


    def _axis_scale_range_text_changed(self, axis):
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        values = list(self.vb.viewRange()[axis_number])
        for index, text in enumerate((ui.minText.text(), ui.maxText.text())):
            try:
                values[index] = float(text)
            except ValueError:
                pass

        ui.manualRadio.setChecked(True)
        if axis == "x":
            self.vb.setXRange(*values, padding=0)
        else:
            self.vb.setYRange(*values, padding=0)


    def _axis_scale_auto_clicked(self, axis):
        ui = self._axis_scale_controls[axis]
        self.vb.enableAutoRange(
            self._axis_scale_axis_constant(axis),
            ui.autoPercentSpin.value() * 0.01,
            )


    def _axis_scale_auto_spin_changed(self, axis, value):
        ui = self._axis_scale_controls[axis]
        ui.autoRadio.setChecked(True)
        self.vb.enableAutoRange(self._axis_scale_axis_constant(axis), value * 0.01)


    def _axis_scale_link_changed(self, axis):
        ui = self._axis_scale_controls[axis]
        if axis == "x":
            self.vb.setXLink(str(ui.linkCombo.currentText()))
        else:
            self.vb.setYLink(str(ui.linkCombo.currentText()))


    def _axis_scale_auto_pan_toggled(self, axis, checked):
        if axis == "x":
            self.vb.setAutoPan(x=checked)
        else:
            self.vb.setAutoPan(y=checked)


    def _axis_scale_visible_only_toggled(self, axis, checked):
        if axis == "x":
            self.vb.setAutoVisible(x=checked)
        else:
            self.vb.setAutoVisible(y=checked)


    def _axis_scale_invert_toggled(self, axis, checked):
        if axis == "x":
            self.vb.invertX(checked)
        else:
            self.vb.invertY(checked)


    @QtCore.pyqtSlot(str)
    def open_axis_scale_dialog(self, axis):
        """
        Opens the scaling dialog for the requested axis.

        """
        if hasattr(self.vb, "updateViewLists"):
            self.vb.updateViewLists()

        if hasattr(self.vbMenu, "updateState"):
            self.vbMenu.updateState()

        dialog = self._axis_scale_dialogs.get(axis)
        if dialog is None:
            dialog = qtw.QDialog(self)
            dialog.setWindowTitle(self._axis_scale_dialog_title(axis))
            layout = qtw.QVBoxLayout(dialog)
            layout.addWidget(self._new_axis_scale_controls(axis))

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)

            self._axis_scale_dialogs[axis] = dialog

        self._sync_axis_scale_controls(axis)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()


    def _remove_scene_export_context_menu(self):
        """
        Removes pyqtgraph's scene-level export action from right-click menus.

        """
        scene = self.widget.scene()
        context_menu = getattr(scene, "contextMenu", None)
        if context_menu is None:
            return

        scene.contextMenu = [
            action for action in context_menu
            if action.text().replace("&", "") != "Export..."
            ]


    def _context_menu_action(self, text):
        """
        Returns a pyqtgraph context-menu action by display text.

        """
        for action in self.vbMenu.actions():
            if action.text().replace("&", "") == text:
                return action
        return None


    @QtCore.pyqtSlot()
    def open_context_menu(self):
        """
        Opens the plot context menu from the keyboard.

        """
        self.vbMenu.exec_(self.widget.mapToGlobal(self.widget.rect().center()))


    @QtCore.pyqtSlot()
    def open_export_dialog(self):
        """
        Opens pyqtgraph's export dialog for this plot.

        """
        scene = self.widget.scene()
        scene.contextMenuItem = self.plot
        scene.showExportDialog()
        
        
    def initAxes(self):
        """
        Sets up left toolbar.
        Sets up which axis parameters are placed on for both 1d, 2d and more.
        
        Refresh fetches the text of the dropdown menu to deciede which data to
        fetch

        """
        indep_params = self.param.depends_on_
        
        self.param_dict = {self.param.name: self.param}
        
        for param in indep_params:
            param_spec = unpack_param(self.ds, param)
            self.param_dict[param_spec.name] = param_spec
        
        # Use of QDockWidget over QToolbar to allow proper widget placement
        self.axes_dock = QDock_context("Line control", self)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.axes_dock)
        
        # Widget production
        x_layout = self.axes_dock.addLayout()
        x_layout.addWidget(qtw.QLabel("x axis: "))
        x_dropdown = expandingComboBox()
        x_dropdown.addItems(indep_params)
        x_layout.addWidget(x_dropdown)
        
        y_layout = self.axes_dock.addLayout()
        y_layout.addWidget(qtw.QLabel("y axis: "))
        y_dropdown = expandingComboBox()
        y_dropdown.addItems(indep_params)
        y_layout.addWidget(y_dropdown)
        
        # Store for later use
        self.axis_dropdown = {"x": x_dropdown, "y": y_dropdown}
        
        # Add options to menus and place correct axes using dataset.depends_on_. 
        # This was set to match plottr
        if len(indep_params) == 1: # 1d plot
            self.axis_dropdown["y"].addItems([self.param.name])
            self.axis_dropdown["x"].addItems([self.param.name])
            
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(indep_params[0])
                )
            self.axis_dropdown["y"].setCurrentIndex(
                self.axis_dropdown["y"].findText(self.param.name)
                )
        else:
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(indep_params[1])
                )
            self.axis_dropdown["y"].setCurrentIndex(
                self.axis_dropdown["y"].findText(indep_params[0])
                )
        
        # Connect slots.
        for axis in ["x", "y"]:
            self.axis_dropdown[axis].currentIndexChanged.connect(
                                        lambda index, axis=axis: self.change_axis(axis)
                                        )
            
        # Produce seperations line as QDockWidget as none inbuilt
        sep = qtw.QFrame()
        sep.setFrameShape(qtw.QFrame.HLine)
        sep.setFrameShadow(qtw.QFrame.Sunken)
        
        self.axes_dock.addWidget(sep)
        
        if self.__class__.__name__ == "plot2d":
            self.axes_dock.layout.addStretch()
        
    
    def initOperations(self):
        """
        Produces a right toolbar for viewing operations to perform during 
        refresh
        
        see ._widgets.operations for setup
            and
            qplot.tools.plot_tools for functions

        """
        self.oper_dock = QDock_context("Operations", self)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.oper_dock)
        self.oper_dock.setVisible(False)# Large window so toggle off by default
        
        self.oper_widget = operations_widget(self)
        self.oper_widget.apply_but.clicked.connect(lambda: self.refreshWindow(force=True))
        self.oper_dock.addWidget(self.oper_widget)
        
    
    def initMenu(self):
        """
        Produces top menu bar.
        Allows toggle of toolbars and force refresh.

        """
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")

        export_plot_action = getattr(self, "exportPlotAction", None)
        if export_plot_action is not None:
            file_menu.addAction(export_plot_action)
            file_menu.addSeparator()

        close_all_plots_action = qtw.QAction("Close All &Plot Windows", self)
        close_all_plots_action.setShortcut("Ctrl+Shift+W")
        close_all_plots_action.setShortcutContext(QtCore.Qt.WindowShortcut)
        close_all_plots_action.setStatusTip("Close all open plot windows")
        close_all_plots_action.triggered.connect(self.request_close_all_plots)
        file_menu.addAction(close_all_plots_action)

        closeAction = qtw.QAction("&Close Window", self)
        closeAction.setShortcuts(
            standard_key_sequences(QKeySequence.Close, ["Ctrl+W"])
            )
        closeAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        closeAction.setStatusTip("Close this plot window")
        closeAction.triggered.connect(self.close)
        file_menu.addAction(closeAction)

        quitAction = qtw.QAction("&Quit qPlot", self)
        quitAction.setShortcuts(
            standard_key_sequences(QKeySequence.Quit, ["Ctrl+Q"])
            )
        quitAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        quitAction.setStatusTip("Quit qPlot")
        quitAction.triggered.connect(self.request_application_quit)
        file_menu.addAction(quitAction)

        add_standard_window_controls(self)

        options_menu = menu.addMenu("&Options")
        mouse_mode_action = getattr(self, "mouseModeAction", None)
        if mouse_mode_action is not None:
            self.vbMenu.removeAction(mouse_mode_action)
            options_menu.addAction(mouse_mode_action)
            options_menu.addSeparator()
        add_confirmation_options(self, options_menu)
        
        main_menu = menu.addMenu("&View")
        
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(lambda: self.refreshWindow(force=True))
        if hasattr(self, "get_mergables"): # Force refresh 1d line options
            refreshAction.triggered.connect(lambda: self.get_mergables.emit())
        main_menu.addAction(refreshAction)
        
        toolbar_menu = self.createPopupMenu()
        toolbar_menu.setTitle("Toolbars")
        main_menu.addMenu(toolbar_menu)
        add_help_menu(self)
    
###############################################################################
#Other Methods  
        
    @staticmethod
    def formatNum(num : float, sf : int=3) -> str:
        """
        Formats cursor point value to clean str display for user.

        Parameters
        ----------
        num : float
            Value at cursor point.
        sf : int, optional
            Number of significant figures to display. The default is 3.
            If this is changed, recomand increase labelWidth in initLables.

        Returns
        -------
        str
            Formated string for display.

        """
        try: # Get number of leading/following zeros
            log = int(log10(abs(num)))
        except ValueError:
            return f"{0:.{sf}f}"
        
        if log >= sf or log < 0:
            return f"{num:.{sf}e}"
        else:
            return f"{num:.{sf - log}f}"
        
        
    def update_theme(self, config):
        """
        Updates theme of window to match main.

        Parameters
        ----------
        config : qplot.config
            Updated config file.

        """
        self.config = config
        
        self.setStyleSheet(self.config.theme.main)
        self.config.theme.style_plotItem(self)


    @staticmethod
    def request_application_quit():
        """
        Closes the main window first so its normal quit handling still applies.

        """
        app = qtw.QApplication.instance()
        if app is None:
            return

        for window in app.topLevelWidgets():
            if window.__class__.__name__ == "MainWindow":
                window.close()
                return

        app.closeAllWindows()


    @staticmethod
    def request_close_all_plots():
        """
        Closes all plot windows through the main window.

        """
        app = qtw.QApplication.instance()
        if app is None:
            return

        for window in app.topLevelWidgets():
            if hasattr(window, "closeAll"):
                window.closeAll()
                return
    
    
    #Note, this is an overwrite of core QMainWindow function
    def createPopupMenu(self) -> "qtw.QMenu":
        """
        Produces a pop-up/context menu.
        Displays all toolbars/dockwidgets to allow for toggle on/off

        Returns
        -------
        menu : PyQt5.QtWidgets.QMenu
            Context menu to be displayed.

        """
        menu = qtw.QMenu(self)
    
        # Fetching QToolBar and QDockWidget
        widgets = self.findChildren((qtw.QToolBar, qtw.QDockWidget))
    
        # Set actions
        for widget in widgets:
            action = widget.toggleViewAction()
            if isinstance(action, qtw.QAction):
                shortcut = self._toggle_shortcuts.get(widget.windowTitle())
                if shortcut:
                    self.register_shortcut(
                        action,
                        shortcut,
                        f"Toggle {widget.windowTitle()}"
                        )
                menu.addAction(action)
    
        return menu
        
    @property
    def axis_options(self) -> dict:
        """
        Returns the currently selected axis in the axis dropdown boxes

        Returns
        -------
        dict{str: str}
            Dictionary in form {axis_name: parameter_name}.

        """
        return {k: v.currentText() for k, v in self.axis_dropdown.items()}
    
    
    def load_data(self, wait_on_thread : bool=False):
        """
        Produces a worker for loading/refreshing the dataset. 
        Then adds the worker to the threadPool queue to work.
        
        Can use wait_on_thread=True to force main thread to wait for callback.
        Recommend to avoid where possible, as effects all windows.

        Parameters
        ----------
        wait_on_thread : bool, optional
            If true uses an QEventLoop to stop main code from running until 
            worker has finished its task. The default is False.

        """
        complete = load_param_data_from_db_prep(self.ds.cache, self.param)
        if complete:
            self.show_status(f"Processing cached data for {self.param.name}...", 0)
        else:
            self.show_status(f"Loading data for {self.param.name}...", 0)
         
        worker = loader(
            self.ds.cache, 
            self.param, 
            self.param_dict, 
            self.axis_options,
            read_data = not complete,
            operations = self.oper_widget.get_data()
            )
        worker.started_at = perf_counter()
        
        # Callback
        worker.emitter.finished.connect(
            lambda finished, worker=worker: self.refreshPlot(finished, worker=worker)
            )
        # Error event handling
        worker.emitter.errorOccurred.connect(self.err_raiser)
        worker.emitter.printer.connect(self.worker_printer)
        
        if wait_on_thread: # Force freeze main thread
            hold_up = QtCore.QEventLoop()
            self.end_wait.connect(hold_up.quit) # Release main thread event
            
        # Run worker
        self.worker = worker
        self.threadPool.start(worker)
    
        if wait_on_thread:
            hold_up.exec() # The actual place the code waits for self.end_wait.emit
            self.end_wait.disconnect(hold_up.quit)
            
###############################################################################
#Events

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape and self.__dict__.get("marquee") is not None:
            self.clear_marquee()
            event.accept()
            return

        super().keyPressEvent(event)

    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Handles close admin on close event. 
        Sends signal, closed, to Main window to further handle event.

        Parameters
        ----------
        Unused but required by slot.

        """
        self.monitor.stop()
        self.visible = False
        self.closed.emit(self) 
        del self # Pretty much pointless but its here anyway.


    @QtCore.pyqtSlot(object)
    def mouseMoved(self, pos):
        """
        Handles event for moving mouse over plot widget. Updates labels defined
        in self.initLabels().

        Parameters
        ----------
        pos : PyQt5.<something?>
            The cursor position object.

        """
        # Ignore if not in plot widget
        if not self.plot.sceneBoundingRect().contains(pos):
            if hasattr(self, "hide_hover_pixel_outline"):
                self.hide_hover_pixel_outline()
            return
    
        # get x, y values.
        mousePoint = self.plot.vb.mapSceneToView(pos)
        
        # Format text into a easy to read format
        x_txt = f"x = {self.formatNum(mousePoint.x())};"
        y_txt = f"y = {self.formatNum(mousePoint.y())}"
        
        # For 2d plots.
        if self.pos_labels.get("z", 0):
            
            y_txt += ";"
            
            rect = self.rect
            
            if hasattr(rect, "x"): # Check plot has initalised
                
                # Get index for that heatmap 'pixel' as a percentage of width/height
                i = (mousePoint.x() - rect.x()) / rect.width()
                j = (mousePoint.y() - rect.y()) / rect.height()
                
                # Check index is within heatmap.
                if (i >= 0 and i <= 1) and (j >= 0 and j <= 1):
                    # Convert to true index
                    # Note that pyqtgraph indexes [column, row]
                    i = min(self.dataGrid.shape[1] - 1, int(i * self.dataGrid.shape[1]))
                    j = min(self.dataGrid.shape[0] - 1, int(j * self.dataGrid.shape[0]))
                    x = rect.x() + (i + 0.5) * rect.width() / self.dataGrid.shape[1]
                    y = rect.y() + (j + 0.5) * rect.height() / self.dataGrid.shape[0]
                    x_txt = f"x = {self.formatNum(x)};"
                    y_txt = f"y = {self.formatNum(y)};"
                    self.pos_labels["z"].setText(f"z = {self.formatNum(self.dataGrid[j, i])}")
                    
                    # Save z location for subplot use
                    if hasattr(self, "show_hover_pixel_outline"):
                        self.show_hover_pixel_outline(i, j)
                    else:
                        self.z_index = [i, j]
                else:
                    if hasattr(self, "hide_hover_pixel_outline"):
                        self.hide_hover_pixel_outline()
                    else:
                        self.z_index = None

        # Update text
        self.pos_labels["x"].setText(x_txt)
        self.pos_labels["y"].setText(y_txt)
        
            
    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        """
        Handles event for self.spinBox value change.
        Updates refresh timer.

        Parameters
        ----------
        interval : float
            Time in seconds to change refresh timer to.

        """
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000)) #convert to seconds
            
            
    @QtCore.pyqtSlot()
    def refreshWindow(self, force : bool = False):
        """
        Event handler for monitor timeout and other refresh sources.
        
        Check whether refresh should be done and attempts to refresh plot.

        Parameters
        ----------
        force : bool, optional
            Forces a refresh regarless of checks. The default is False.

        """
        self.monitor.stop()
        retry = False

        try:
            # Plot has started, worker first defined in initFrame
            if not hasattr(self, "worker"):
                self.initFrame() #defined in children classes
                retry = True
                return
            
            # Check if new data has been added to the dataset
            if self.ds.number_of_results != self.last_ds_len or force:
                if self.worker.running: # No need to run if already updating
                    if not force:
                        return
                    
                # The actual refresh line
                self.load_data()

        finally: #Ran after return or otherwise
        
            # number_of_results Uses SQL check so can be used regardless of loader progress
            self.last_ds_len = self.ds.number_of_results 

            #restart monitor
            if self.ds.running or retry:
                self.monitorIntervalChanged(self.spinBox.value())
               
            #restard monitor if any subplots are live
            elif hasattr(self, "lines") and self.lines:
                for subplot in list(self.lines.values())[1:]:
                    if subplot.running:
                        self.monitorIntervalChanged(self.spinBox.value())
                        break


    @QtCore.pyqtSlot(bool)
    def refreshPlot(self, finished : bool = True, worker=None):
        """
        Produces a shallow copy of data produced by worker.
        This is inhertited by plot<1/2>d to actually use the loaded data.

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.

        """
        try:
            if not finished: # error in worker
                if worker is not None:
                    worker.running = False
                return False
            
            if worker is None:
                worker = self.worker

            if worker is not self.worker:
                worker.running = False
                return False
            
            # Update qcodes dataset variables if db read happened
            if worker.read_data:
                cache = self.ds.cache
                name = self.param.name
                
                update_cache_parameter_data(
                    cache,
                    name,
                    worker.updated_read_status,
                    worker.updated_write_status,
                    worker.cache_data,
                    )
                
                if not cache_has_no_written_data(cache):
                    self._live = False
            
            #set data to be called by plot<1/2>d.refreshPlot()
            self.axis_data = {
                "x": worker.axis_data["x"], 
                "y": worker.axis_data["y"]
                }
            self.axis_param = {
                "x": worker.axis_param["x"], 
                "y": worker.axis_param["y"]
                }
            
            # For 2d plots
            if hasattr(worker, "dataGrid"):        
                self.dataGrid = worker.dataGrid
                
            # I didnt want to make this a dedicated callback for the few times 
            # it is used, as the performace hit is neglible
            # Update text
            self._set_param_axis_labels()
            elapsed = perf_counter() - worker.started_at

            self.show_status(
                f"Loaded {self.ds.number_of_results:,} points for {self.param.name} "
                f"in {elapsed:.2f} seconds",
                5000
                )
            return True
                
        except AttributeError as err:
            # If worker starts too quickly, overwrites data and spits out error.
            # This should no longer be possible so making error soft error.
            self.show_status(f"Refresh skipped: {err}", 10000)
        
        finally: # Allow code to move on from wait_on_thread
            self.end_wait.emit()
        
        
    @QtCore.pyqtSlot(Exception)
    def err_raiser(self, err : Exception):
        message = f"{type(err).__name__}: {err}"
        log_exception("Plot worker error", err, __name__)
        self.show_status(f"Worker error: {message}", 10000)

        if message == self._last_error_text:
            return

        self._last_error_text = message
        self.show_error("Plot Error", "A plot worker failed.", message)
        
        
    @QtCore.pyqtSlot(str)
    def worker_printer(self, fstr : str):
        # Worker print() often does not work, so done through event handlers
        self.show_status(fstr, 5000)
        print(fstr)
    
    
    def add_or_remove_operations(self, key : str, func : callable = None):
        """
        Adds a callable function to be passed to the operations for the worker

        Parameters
        ----------
        key : str
            A key to track the function.
        func : callable, optional
            Function to be added to the tracker. If None is passed instead of a
            callable, the key is instead removed from the tracker.

        """
        # Remove item if func is none
        if func is None and self.operations.get(key, 0) != 0:
            self.operations.pop(key)
        else: # otherwise add to list
            self.operations[key] = func
        
        # Force update
        self.refreshWindow(force=True)
    
    
    @QtCore.pyqtSlot()
    def change_axis(self, key : str):
        """
        Event handler for axis dropdown menu selection change.
        Switches the axes based on user selection and calls a forced refresh.

        Parameters
        ----------
        key : str
            The axis label (x or y) which has been changed.

        Raises
        ------
        ValueError
            Error catch for rare cases where dropdown menus fail to correctly
            update.

        """
        duplicates = [k for k, v in self.axis_dropdown.items() 
                          if self.axis_dropdown[key].currentText() == v.currentText()
                          and k != key
                     ]
        
        # If both boxes show the same value, switch second box to original value
        if len(duplicates) == 1:
            self.axis_dropdown[duplicates[0]].blockSignals(True)
            
            # Fetch axis parameter from self.axis_param["<x/y>"]
            self.axis_dropdown[duplicates[0]].setCurrentIndex(
                self.axis_dropdown[duplicates[0]].findText(self.axis_param[key].name)
                )
            
            self.axis_dropdown[duplicates[0]].blockSignals(False)
            
            # Flip worker data to match change
            temp_y_data = self.worker.axis_data["y"]
            temp_y_param = self.worker.axis_param["y"]
            
            self.worker.axis_data["y"] = self.worker.axis_data["x"]
            self.worker.axis_data["x"] = temp_y_data
            
            self.worker.axis_param["y"] = self.worker.axis_param["x"]
            self.worker.axis_param["x"] = temp_y_param
            
            if hasattr(self.worker, "dataGrid"):
                self.worker.dataGrid = self.worker.dataGrid.transpose()
                
            # Refresh without loading new dataset data
            self.refreshPlot()
            
        else:
            # get new data
            self.refreshWindow(force=True)
