from math import floor, isfinite, log10
from os import path
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.qcodes_cache import set_parameter_complete
from qplot.tools import (
    unpack_param,
)

from . import _plot_axis_scaling
from ._commands import (
    command_spec,
    command_with_status,
    create_action,
    toolbar_toggle_command_spec,
)
from ._dataset_handle import DatasetKey, TraceKey
from ._dragdrop import (
    preview_drop_is_compatible,
    run_preview_payload_from_mime,
)
from ._help import add_help_menu
from ._plot_axis_scaling import (
    PlotAxisScalingMixin,
    _PowerScaledAxisItem,
)
from ._plot_export import PlotExportMixin
from ._plot_feedback import PlotWindowFeedbackMixin
from ._plot_marquee import PlotMarqueeMixin
from ._plot_refresh import PlotRefreshMixin
from ._plot_state import PlotStateOverlay
from ._preferences import (
    MOUSE_MODE_KEY,
    PreferencesDialog,
    create_preferences_action,
)
from ._subplots import custom_viewbox
from ._widgets import (
    QDock_context,
    expandingComboBox,
    operations_widget,
)
from ._window_controls import (
    add_standard_window_controls,
    main_window_for,
)

if TYPE_CHECKING:
    import qcodes

    import qplot


_axis_scale_power_text = _plot_axis_scaling._axis_scale_power_text

_A4_LANDSCAPE_PLOT_AREA_SIZE = QtCore.QSize(1123, 794)
_A4_PORTRAIT_PLOT_AREA_SIZE = QtCore.QSize(794, 1123)
_POWERPOINT_STANDARD_PLOT_AREA_SIZE = QtCore.QSize(960, 720)
_POWERPOINT_WIDESCREEN_PLOT_AREA_SIZE = QtCore.QSize(1280, 720)
_SQUARE_PLOT_AREA_SIZE = QtCore.QSize(850, 850)
_PLOT_AREA_RESIZE_PRESETS = (
    (
        "A4 Landscape",
        "resizePlotAreaA4LandscapeAction",
        _A4_LANDSCAPE_PLOT_AREA_SIZE,
        ),
    (
        "A4 Portrait",
        "resizePlotAreaA4PortraitAction",
        _A4_PORTRAIT_PLOT_AREA_SIZE,
        ),
    (
        "PowerPoint Standard",
        "resizePlotAreaPowerPointStandardAction",
        _POWERPOINT_STANDARD_PLOT_AREA_SIZE,
        ),
    (
        "PowerPoint Widescreen",
        "resizePlotAreaPowerPointWidescreenAction",
        _POWERPOINT_WIDESCREEN_PLOT_AREA_SIZE,
        ),
    (
        "Square",
        "resizePlotAreaSquareAction",
        _SQUARE_PLOT_AREA_SIZE,
        ),
    )


def _plot_area_size_icon(size):
    """
    Builds a small aspect-ratio preview icon for plot-area resize presets.

    """
    pixmap = QtGui.QPixmap(28, 18)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)

    bounds = QtCore.QSizeF(22, 14)
    aspect = size.width() / size.height()
    bounds_aspect = bounds.width() / bounds.height()
    if aspect >= bounds_aspect:
        width = bounds.width()
        height = width / aspect
    else:
        height = bounds.height()
        width = height * aspect

    rect = QtCore.QRectF(
        (pixmap.width() - width) / 2,
        (pixmap.height() - height) / 2,
        width,
        height,
        )

    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor(70, 70, 70), 1))
    painter.setBrush(QtGui.QColor(120, 150, 200, 70))
    painter.drawRect(rect)
    painter.end()

    return QtGui.QIcon(pixmap)


class plotWidget(
    PlotWindowFeedbackMixin,
    PlotAxisScalingMixin,
    PlotMarqueeMixin,
    PlotExportMixin,
    PlotRefreshMixin,
    qtw.QMainWindow,
    ):
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
    make_ds = QtCore.pyqtSignal([object])
    previewTraceDropRequested = QtCore.pyqtSignal(object, object, str)
    trace_updated = QtCore.pyqtSignal()
    
    _label_width = 95 #About the size of 3 s.f. scientific
    def __init__(self, 
                 dataset_key: DatasetKey,
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
        dataset_key : DatasetKey
            The database-aware identity of the dataset to plot.
        param : qcodes.dataset.ParamSpec
            Which parameter within dataset to plot.
        config : qplot.configuration.config.config
            Holds configuration data, mainly theme and window size.
        threadPool : PyQt6.QtCore.QThreadPool
            A pool of threads for the refresh worker to be placed in.
        dataset_holder : dict[DatasetKey, DatasetHandle]
            Shared map of dataset identities to open-dataset ownership handles.
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
        self._dataset_key = dataset_key
        self._guid = dataset_key.guid
        self.param = param
        if not hasattr(self.param, "_complete"): # Add completed load track
            set_parameter_complete(self.param, False)
        self.name = str(self)
        self.label = f"ID:{self.ds.run_id} {self.param.name}"
        self._trace_key = TraceKey(self._dataset_key, self.param.name)
        self.monitor = QtCore.QTimer()
        self.threadPool = threadPool
        self.last_ds_len = self.ds.number_of_results
        self.config = config
        self.visible = show
        self._closed = False
        self._merged_trace_users = 0
        self.operations = {}
        self._last_error_text = None
        self.show_status("Working, please wait", 0)
        
        ### WIDGETS
        self._window_layout = qtw.QVBoxLayout()
        
        self.widget = pg.GraphicsLayoutWidget()
        self.plot_state_overlay = PlotStateOverlay(self.widget)
        self._install_preview_drop_target()
        # Overwrite default viewbox to give more flexibility
        self.vb = custom_viewbox() # Mainly for linking secondary axis
        self.vb.setDefaultPadding(0)
        self.apply_mouse_mode_preference()
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
        self._window_layout.addWidget(self.widget)
        
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
    
            initial_width = int(sizeFrac * screenrect.width())
            initial_height = int(sizeFrac * screenrect.height())
            self.resize(initial_width, initial_height)
            
            w = qtw.QFrame()
            w.setLayout(self._window_layout)
            self.setCentralWidget(w)
        
        #start refresh cycle if live
        if self.ds.running:
            self.monitorIntervalChanged(self.spinBox.value())


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
            QtCore.QEvent.Type.DragEnter,
            QtCore.QEvent.Type.DragMove,
            QtCore.QEvent.Type.Drop,
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

        if event.type() == QtCore.QEvent.Type.Drop:
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
            source_identity = payload["guid"]
            if payload.get("database_path"):
                source_identity = DatasetKey(
                    payload["database_path"],
                    payload["guid"],
                )
            self.previewTraceDropRequested.emit(
                self,
                source_identity,
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



    def __str__(self):
        filenameStr = path.basename(self._dataset_key.database_path)
        fstr = (f"{filenameStr} | " 
                f"Run ID: {self.ds.run_id} | "
                f"{self.param.name} ({self.param.label})"
                )
        return fstr

    
    @property
    def ds(self):
        """
        Returns the window's dataset from the dictionary of stored datasets

        Returns
        -------
        qcodes.dataset.data_set.dataset

        """
        # Check dataset exists, produce new one if needed.
        handle = self._dataset_holder.get(self._dataset_key)
        if handle is None:
            self.show_status(f"Dataset {self._guid} not found. Reloading...", 5000)
            self.make_ds.emit(self._dataset_key)
            handle = self._dataset_holder[self._dataset_key]
        
        # Check a deletion timer is not active and stop
        else:
            handle.cancel_delete_timer()
            
        return handle.dataset
        
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
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, self.toolbarRef)
        
        if not self.ds.running:
            self.toolbarRef.hide()
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setRange(0.0, 86_400.0)
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(3)

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
        self.addToolBar(QtCore.Qt.ToolBarArea.BottomToolBarArea, self.toolbarCo_ord)
        
        labelWidth = self._label_width #About the size of 3 s.f. scientific
        self.pos_labels = {}

        posLabelIndex = qtw.QLabel("")
        posLabelIndex.setMinimumWidth(45)
        self.toolbarCo_ord.addWidget(posLabelIndex)
        self.pos_labels["index"] = posLabelIndex
        
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
        self._connect_mouse_mode_menu_to_preferences()
        self._remove_scene_export_context_menu()
        if getattr(self.plot, "ctrlMenu", None) is not None:
            self.plot.ctrlMenu.setTitle("Options")
            self.plot.ctrlMenu.menuAction().setText("Options")

        self.exportPlotAction = create_action("plot.export", self)
        self.register_shortcut(
            self.exportPlotAction,
            command_spec("plot.export"),
            )
        self.exportPlotAction.triggered.connect(self.open_export_dialog)

        self.savePlotPdfAction = QtGui.QAction("Save Plot as &PDF...", self)
        self.savePlotPdfAction.setObjectName("savePlotPdfAction")
        self.savePlotPdfAction.setStatusTip("Save the visible plot area as a PDF")
        self.savePlotPdfAction.triggered.connect(self.save_plot_pdf)

        self.copyPlotImageAction = create_action("plot.copy_image", self)
        self.register_shortcut(
            self.copyPlotImageAction,
            command_spec("plot.copy_image"),
            )
        self.copyPlotImageAction.triggered.connect(self.copy_plot_image)

        contextAction = create_action(
            "context.show",
            self,
            status_tip="Show plot context menu",
            )
        self.register_shortcut(
            contextAction,
            command_with_status("context.show", "Show plot context menu"),
            )
        contextAction.triggered.connect(self.open_context_menu)
        
        actions = self.vbMenu.actions()
        for action in actions:
            if action.text() == "View All":
                self.register_shortcut(action, command_spec("plot.autoscale"))
                action.setText(command_spec("plot.autoscale").text)
                break
        
        x_action = actions[1]
        
        self.autoscaleSep = self.vbMenu.insertSeparator(x_action)

        self.vbMenu.insertAction(x_action, self.savePlotPdfAction)
        self.vbMenu.insertSeparator(x_action)
        self.vbMenu.insertAction(x_action, self.copyPlotImageAction)
        self.vbMenu.insertSeparator(x_action)
        
        # Create visibility
        toggleAction = create_action(
            "plot.toggle_operations",
            self,
            checkable=True,
            )
        self.register_shortcut(toggleAction, command_spec("plot.toggle_operations"))
        toggleAction.triggered.connect(self.oper_dock.setVisible)
        self.oper_dock.visibilityChanged.connect(toggleAction.setChecked)
        self.vbMenu.insertAction(x_action, toggleAction)
        self.vbMenu.insertSeparator(x_action)

        self._init_axis_scale_dialogs()


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
        self.vbMenu.exec(self.widget.mapToGlobal(self.widget.rect().center()))


    def _add_plot_area_resize_menu(self, window_menu):
        """
        Adds fixed plot-area resize actions to the Window menu.

        """
        resize_menu = qtw.QMenu("&Resize", self)
        resize_menu.setObjectName("plotAreaResizeMenu")

        for label, object_name, size in _PLOT_AREA_RESIZE_PRESETS:
            action = QtGui.QAction(
                _plot_area_size_icon(size),
                f"{label} ({size.width()} x {size.height()} px)",
                self,
                )
            action.setObjectName(object_name)
            action.setStatusTip(
                f"Resize the copied plot area to {size.width()} x {size.height()} px"
                )
            action.triggered.connect(
                lambda _checked=False, size=size: self.resize_plot_area(
                    size.width(),
                    size.height(),
                    )
                )
            resize_menu.addAction(action)

        resize_menu.addSeparator()
        custom_action = QtGui.QAction("&Custom...", self)
        custom_action.setObjectName("resizePlotAreaCustomAction")
        custom_action.setStatusTip("Resize the copied plot area to a custom pixel size")
        custom_action.triggered.connect(self.open_custom_plot_area_size_dialog)
        resize_menu.addAction(custom_action)

        insert_before = next(
            (
                action for action in window_menu.actions()
                if action.text().replace("&", "") == "Minimize"
                ),
            None,
            )
        if insert_before is None:
            window_menu.addMenu(resize_menu)
            return resize_menu

        window_menu.insertMenu(insert_before, resize_menu)
        window_menu.insertSeparator(insert_before)
        return resize_menu


    @QtCore.pyqtSlot()
    def open_custom_plot_area_size_dialog(self):
        """
        Opens a dialog for resizing the copied plot area to an exact pixel size.

        """
        current_size = self._current_plot_area_size()

        dialog = qtw.QDialog(self)
        dialog.setWindowTitle("Custom Plot Area Size")

        form = qtw.QFormLayout(dialog)
        width_spin = qtw.QSpinBox(dialog)
        width_spin.setRange(1, 20_000)
        width_spin.setSuffix(" px")
        width_spin.setValue(max(1, current_size.width()))

        height_spin = qtw.QSpinBox(dialog)
        height_spin.setRange(1, 20_000)
        height_spin.setSuffix(" px")
        height_spin.setValue(max(1, current_size.height()))

        form.addRow("&Width:", width_spin)
        form.addRow("&Height:", height_spin)

        buttons = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Ok
            | qtw.QDialogButtonBox.StandardButton.Cancel,
            dialog,
            )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() == qtw.QDialog.DialogCode.Accepted:
            self.resize_plot_area(width_spin.value(), height_spin.value())


    def _current_plot_area_size(self):
        """
        Returns the current copied plot-area size.

        """
        widget = getattr(self, "widget", None)
        if widget is None:
            return QtCore.QSize(1, 1)
        return widget.size()


    def resize_plot_area(self, width, height):
        """
        Resizes the window so the copied plot area has the requested size.

        """
        target = QtCore.QSize(int(width), int(height))
        if target.width() < 1 or target.height() < 1:
            self.show_status("Plot area size must be at least 1 x 1 px.", 5000)
            return False

        widget = getattr(self, "widget", None)
        if widget is None:
            self.show_status("No plot area available to resize.", 5000)
            return False

        if self.isMaximized() or self.isFullScreen():
            self.showNormal()

        self._resize_window_for_plot_area(target)
        actual = self._current_plot_area_size()
        if actual == target:
            self.show_status(
                f"Plot area resized to {actual.width()} x {actual.height()} px.",
                3000,
                )
            return True

        self.show_status(
            "Plot area resized to "
            f"{actual.width()} x {actual.height()} px "
            f"(requested {target.width()} x {target.height()} px).",
            5000,
            )
        return False


    def _resize_window_for_plot_area(self, target):
        """
        Applies the top-level resize, refining after layouts have updated.

        """
        for _attempt in range(3):
            current = self._current_plot_area_size()
            delta = target - current
            if delta.isNull():
                return

            next_size = self.size() + delta
            next_size.setWidth(max(1, next_size.width()))
            next_size.setHeight(max(1, next_size.height()))
            self.resize(next_size)

            app = qtw.QApplication.instance()
            if app is not None:
                app.processEvents(
                    QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                    )
        
        
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
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self.axes_dock)
        
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
        # Do not call the overridable ``axis_options`` property while the base
        # controls are still being constructed. Cut windows add their fixed-
        # axis picker only after this method returns.
        self._axis_selection = {
            axis: dropdown.currentText()
            for axis, dropdown in self.axis_dropdown.items()
            }
            
        # Produce seperations line as QDockWidget as none inbuilt
        sep = qtw.QFrame()
        sep.setFrameShape(qtw.QFrame.Shape.HLine)
        sep.setFrameShadow(qtw.QFrame.Shadow.Sunken)
        
        self.axes_dock.addWidget(sep)
        
        if getattr(self, "operation_kind", None) == "plot2d":
            self.axes_dock.content_layout.addStretch()
        
    
    def initOperations(self):
        """
        Produces a right toolbar for viewing operations to perform during 
        refresh
        
        see ._widgets.operations for setup
            and
            qplot.tools.plot_tools for functions

        """
        self.oper_dock = QDock_context("Operations", self)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.oper_dock)
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

        save_plot_pdf_action = getattr(self, "savePlotPdfAction", None)
        if save_plot_pdf_action is not None:
            file_menu.addAction(save_plot_pdf_action)

        if export_plot_action is not None or save_plot_pdf_action is not None:
            file_menu.addSeparator()

        copy_plot_image_action = getattr(self, "copyPlotImageAction", None)
        if copy_plot_image_action is not None:
            edit_menu = menu.addMenu("&Edit")
            edit_menu.addAction(copy_plot_image_action)

        close_all_plots_action = create_action(
            "plots.close_all",
            self,
            status_tip="Close all open plot windows",
            )
        close_all_plots_action.triggered.connect(self.request_close_all_plots)
        file_menu.addAction(close_all_plots_action)

        closeAction = create_action(
            "window.close",
            self,
            status_tip="Close this plot window",
            )
        closeAction.triggered.connect(self.close)
        file_menu.addAction(closeAction)

        quitAction = create_action("app.quit", self)
        quitAction.triggered.connect(self.request_application_quit)
        file_menu.addAction(quitAction)

        window_menu = add_standard_window_controls(self)
        self._add_plot_area_resize_menu(window_menu)

        options_menu = menu.addMenu("&Options")
        options_menu.addAction(
            create_preferences_action(self, self.show_preferences_dialog)
            )

        mouse_mode_action = getattr(self, "mouseModeAction", None)
        if mouse_mode_action is not None:
            self.vbMenu.removeAction(mouse_mode_action)
            options_menu.addSeparator()
            options_menu.addAction(mouse_mode_action)
        
        main_menu = menu.addMenu("&View")
        
        refreshAction = create_action("window.refresh", self)
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
        if not isfinite(num):
            if num != num:
                return "nan"
            return "-inf" if num < 0 else "inf"

        try: # Get number of leading/following zeros
            exponent = floor(log10(abs(num)))
        except ValueError:
            return f"{0:.{sf}f}"

        precision = max(sf - 1, 0)
        if exponent >= sf or exponent < 0:
            return f"{num:.{precision}e}"

        formatted = f"{num:.{max(sf - exponent - 1, 0)}f}"
        rounded_exponent = floor(log10(abs(float(formatted))))
        if rounded_exponent != exponent:
            return f"{num:.{precision}e}"
        return formatted


    def _set_cursor_index_label(self, text: str) -> None:
        """
        Update the cursor index label when the toolbar includes one.

        """
        label = self.pos_labels.get("index")
        if label is not None:
            label.setText(text)


    def _cursor_1d_x_data(self):
        """
        Return the X data used to derive the 1d cursor array index.

        """
        line = self.__dict__.get("line")
        if line is not None and hasattr(line, "getData"):
            data = line.getData()
            if data is not None and data[0] is not None:
                return data[0]

        return self.__dict__.get("axis_data", {}).get("x")


    def _nearest_1d_array_index(self, x_value: float) -> int | None:
        """
        Return the zero-based data index nearest to a cursor X coordinate.

        """
        x_data = self._cursor_1d_x_data()
        if x_data is None:
            return None

        nearest_index = None
        nearest_distance = None
        for index, value in enumerate(x_data):
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue

            if not isfinite(numeric_value):
                continue

            distance = abs(numeric_value - x_value)
            if nearest_distance is None or distance < nearest_distance:
                nearest_index = index
                nearest_distance = distance

        return nearest_index
        
        
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


    def apply_current_settings(self):
        """
        Applies config-backed settings handled directly by plot windows.

        """
        self.update_theme(self.config)
        self.apply_mouse_mode_preference()


    def _configured_mouse_mode(self):
        try:
            mode = self.config.get(MOUSE_MODE_KEY)
        except KeyError:
            mode = "pan"

        if mode not in {"pan", "rect"}:
            return "pan"
        return mode


    def apply_mouse_mode_preference(self):
        """
        Applies the configured pyqtgraph mouse mode to this plot view.

        """
        if hasattr(self, "vb"):
            self.vb.setLeftButtonAction(self._configured_mouse_mode())


    def change_mouse_mode(self, mode):
        """
        Persists a mouse mode change made from a plot-window menu.

        """
        if mode not in {"pan", "rect"}:
            return False

        main_window = main_window_for(self)
        target_config = getattr(main_window, "config", self.config)
        if target_config is not self.config:
            self.config = target_config

        try:
            current_mode = target_config.get(MOUSE_MODE_KEY)
        except KeyError:
            current_mode = None

        if current_mode != mode:
            target_config.update(MOUSE_MODE_KEY, mode)

        if (
                main_window is not None
                and main_window is not self
                and hasattr(main_window, "apply_current_settings")
                ):
            main_window.apply_current_settings()
        else:
            self.apply_mouse_mode_preference()
        return True


    def _connect_mouse_mode_menu_to_preferences(self):
        menu = self.mouseModeAction.menu() if self.mouseModeAction is not None else None
        if menu is None:
            return

        for action, mode in zip(
                getattr(menu, "mouseModes", ()),
                ("pan", "rect"),
                strict=False,
                ):
            action.triggered.connect(
                lambda _checked=False, mode=mode: self.change_mouse_mode(mode)
                )


    def show_preferences_dialog(self):
        """
        Opens the shared preferences dialog from a plot window.

        """
        main_window = main_window_for(self)
        owner = main_window if main_window is not None else self
        config = getattr(owner, "config", self.config)

        if config is not self.config:
            self.config = config

        dialog = PreferencesDialog(config, self)
        if hasattr(owner, "apply_current_settings"):
            dialog.preferencesApplied.connect(owner.apply_current_settings)
        else:
            dialog.preferencesApplied.connect(self.apply_current_settings)
        dialog.preferencesApplied.connect(
            lambda: self.show_status("Preferences saved.", 3000)
            )
        dialog.exec()


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
                quit_application = getattr(window, "quit_application", None)
                if callable(quit_application):
                    quit_application()
                else:
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
        menu : PyQt6.QtWidgets.QMenu
            Context menu to be displayed.

        """
        menu = qtw.QMenu(self)
    
        # Fetching QToolBar and QDockWidget
        widgets = self.findChildren((qtw.QToolBar, qtw.QDockWidget))
    
        # Set actions
        for widget in widgets:
            action = widget.toggleViewAction()
            if isinstance(action, QtGui.QAction):
                command = toolbar_toggle_command_spec(widget.windowTitle())
                if command is not None:
                    self.register_shortcut(
                        action,
                        command,
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
    
    
###############################################################################
#Events

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape and self.__dict__.get("marquee") is not None:
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
        if self.__dict__.get("_merged_trace_users", 0) <= 0:
            self.monitor.stop()
        self.visible = False
        self._closed = True
        self.closed.emit(self)

        if (
                self.__dict__.get("_merged_trace_users", 0) > 0
                and self.ds.running
                and not self.monitor.isActive()
                ):
            self.monitorIntervalChanged(self.spinBox.value())


    @QtCore.pyqtSlot(object)
    def mouseMoved(self, pos):
        """
        Handles event for moving mouse over plot widget. Updates labels defined
        in self.initLabels().

        Parameters
        ----------
        pos : PyQt6.<something?>
            The cursor position object.

        """
        # Ignore if not in plot widget
        if not self.plot.sceneBoundingRect().contains(pos):
            self._set_cursor_index_label("")
            if hasattr(self, "hide_hover_pixel_outline"):
                self.hide_hover_pixel_outline()
            z_label = self.pos_labels.get("z")
            if z_label is not None:
                z_label.setText("z =")
            return
    
        # get x, y values.
        mousePoint = self.plot.vb.mapSceneToView(pos)
        
        # Format text into a easy to read format
        index_txt = ""
        x_txt = f"x = {self.formatNum(mousePoint.x())};"
        y_txt = f"y = {self.formatNum(mousePoint.y())}"
        
        # For 2d plots.
        if self.pos_labels.get("z", 0):
            y_txt += ";"
            sample_at = getattr(self, "heatmap_sample_at", None)
            sample = (
                sample_at(mousePoint.x(), mousePoint.y())
                if callable(sample_at)
                else None
                )
            if sample is not None:
                i, j, x, y, z = sample
                index_txt = f"[{i},{j}]"
                x_txt = f"x = {self.formatNum(x)};"
                y_txt = f"y = {self.formatNum(y)};"
                self.pos_labels["z"].setText(f"z = {self.formatNum(z)}")
                if hasattr(self, "show_hover_pixel_outline"):
                    self.show_hover_pixel_outline(i, j)
                else:
                    self.z_index = [i, j]
            else:
                self.pos_labels["z"].setText("z =")
                if hasattr(self, "hide_hover_pixel_outline"):
                    self.hide_hover_pixel_outline()
                else:
                    self.z_index = None
        else:
            index = self._nearest_1d_array_index(mousePoint.x())
            if index is not None:
                index_txt = f"[{index}]"

        # Update text
        self._set_cursor_index_label(index_txt)
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
            self.monitor.start(max(1, round(interval * 1000)))
            
            
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
        previous = getattr(self, "_axis_selection", self.axis_options)
        duplicates = [k for k, v in self.axis_dropdown.items()
                          if self.axis_dropdown[key].currentText() == v.currentText()
                          and k != key
                     ]
        
        # If both boxes show the same value, switch second box to original value
        if len(duplicates) == 1:
            self.axis_dropdown[duplicates[0]].blockSignals(True)
            
            self.axis_dropdown[duplicates[0]].setCurrentIndex(
                self.axis_dropdown[duplicates[0]].findText(previous[key])
                )
            
            self.axis_dropdown[duplicates[0]].blockSignals(False)
            
        self._axis_selection = self.axis_options
        self.refreshWindow(force=True)
