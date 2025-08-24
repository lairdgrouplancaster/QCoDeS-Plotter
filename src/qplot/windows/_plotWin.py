from typing import TYPE_CHECKING

from math import log10

import pyqtgraph as pg

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore 

from qcodes.dataset.sqlite.database import get_DB_location

from qplot.tools import (
    unpack_param,
    loader,
    )
from qplot.datahandling import load_param_data_from_db_prep
    
from ._subplots import custom_viewbox
from ._widgets import (
    expandingComboBox,
    QDock_context,
    operations_widget,
    )

if TYPE_CHECKING:
    import qplot
    import qcodes


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
    
    _label_width = 95 #About the size of 3 s.f. scientific
    
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
        print("Working, please wait")
        super().__init__()
        
        ### CORE VARIABLES
        self._dataset_holder = dataset_holder
        self._guid = guid
        self.param = param
        if not hasattr(self.param, "_complete"): # Add completed load track
            self.param._complete = False
        self.name = str(self)
        self.label = f"ID:{self.ds.run_id} {self.param.name}"
        self.monitor = QtCore.QTimer()
        self.threadPool = threadPool
        self.last_ds_len = self.ds.number_of_results
        self.config = config
        self.visible = show
        self.operations = {}
        
        ### WIDGETS
        self.layout = qtw.QVBoxLayout()
        
        self.widget = pg.GraphicsLayoutWidget()
        # Overwrite default viewbox to give more flexibility
        self.vb = custom_viewbox() # Mainly for linking secondary axis
        self.plot = self.widget.addPlot(viewBox=self.vb)
        self.vb.setParent(self.plot)
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
        
        
    def __str__(self):
        filenameStr = get_DB_location().split('\\')[-1]
        fstr = (f"{filenameStr} | " 
                f"run ID: {self.ds.run_id} | "
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
        if self._dataset_holder.get(self._guid, 0) == 0:
            print(f"KeyError: guid: {self._guid} not found. Producing new dataset.")
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
        
        actions = self.vbMenu.actions()
        for action in actions:
            if action.text() == "View All":
                action.setText("Autoscale")
                break
        
        x_action = actions[1]
        
        self.autoscaleSep = self.vbMenu.insertSeparator(x_action)
        
        # Create visibility
        toggleAction = qtw.QAction("View Operations", self, checkable=True)
        toggleAction.triggered.connect(self.oper_dock.setVisible)
        self.oper_dock.visibilityChanged.connect(toggleAction.setChecked)
        self.vbMenu.insertAction(x_action, toggleAction)
        self.vbMenu.insertSeparator(x_action)
        
        
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
         
        worker = loader(
            self.ds.cache, 
            self.param, 
            self.param_dict, 
            self.axis_options,
            read_data = not complete,
            operations = self.oper_widget.get_data()
            )
        
        # Callback
        worker.emitter.finished.connect(self.refreshPlot)
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
            return
    
        # get x, y values.
        mousePoint = self.plot.vb.mapSceneToView(pos)
        
        # Format text into a easy to read format
        x_txt = f"x = {self.formatNum(mousePoint.x())};"
        y_txt = f"y = {self.formatNum(mousePoint.y())}"
        
        # For 2d plots.
        if self.pos_labels.get("z", 0):
            
            y_txt += ";"
            
            image_data = self.image.image
            
            rect = self.rect
            
            if hasattr(rect, "x"): # Check plot has initalised
                
                # Get index for that heatmap 'pixel' as a percentage of width/height
                i = (mousePoint.x() - rect.x()) / rect.width()
                j = (mousePoint.y() - rect.y()) / rect.height()
                
                # Check index is within heatmap.
                if (i >= 0 and i <= 1) and (j >= 0 and j <= 1):
                    # Convert to true index
                    # Note that pyqtgraph indexes [column, row]
                    i = int(i * image_data.shape[1])
                    j = int(j * image_data.shape[0])
                    self.pos_labels["z"].setText(f"z = {self.formatNum(image_data[j, i])}")
                    
                    # Save z location for subplot use
                    self.z_index = [i, j]
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
    def refreshPlot(self, finished : bool = True):
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
                return
            
            print("loading new data")
            
            worker = self.worker
            
            # Update qcodes dataset variables if db read happened
            if worker.read_data:
                cache = self.ds.cache
                name = self.param.name
                
                cache._read_status[name] = worker.updated_read_status[name]
                cache._write_status[name] = worker.updated_write_status[name]
                cache._data[name] = worker.cache_data[name]
                
                ### Copied from qcodes functions
                data_not_read = all(
                    status is None or status == 0 for status in cache._write_status.values()
                )
                if not data_not_read:
                    self._live = False
                ###
            
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
            self.plot.setLabel(axis="bottom", text=f"{self.axis_param['x'].label} ({self.axis_param['x'].unit})")
            self.plot.setLabel(axis="left", text=f"{self.axis_param['y'].label} ({self.axis_param['y'].unit})")
                
        except AttributeError as err:
            # If worker starts too quickly, overwrites data and spits out error.
            # This should no longer be possible so making error soft error.
            print(type(err), err)
        
        finally: # Allow code to move on from wait_on_thread
            self.end_wait.emit()
        
        
    @QtCore.pyqtSlot(Exception)
    def err_raiser(self, err : Exception):
        # Worker cannot raise errors so much be done through event handlers
        print("WORKER ERROR:")
        raise err
        
        
    @QtCore.pyqtSlot(str)
    def worker_printer(self, fstr : str):
        # Worker print() often does not work, so done through event handlers
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
        