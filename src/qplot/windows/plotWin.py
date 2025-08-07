from math import log10

import pyqtgraph as pg

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore 

import qcodes
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.tools import unpack_param
from qplot.windows._widgets import (
    expandingComboBox,
    QDock_context,
    )
from qplot.tools.subplot import custom_viewbox


class plotWidget(qtw.QMainWindow):
    closed = QtCore.pyqtSignal([object])
    
    def __init__(self, 
                 dataset : qcodes.dataset.data_set.DataSet, 
                 param : qcodes.dataset.ParamSpec,
                 config,
                 threadPool : QtCore.QThreadPool,
                 refrate : float=None,
                 show : bool=True
                 ):
        print("Working, please wait")
        super().__init__()
        
        self.ds = dataset
        self.param = param
        self.name = str(self)
        self.label = f"ID:{self.ds.run_id} {self.param.name}"
        self.monitor = QtCore.QTimer()
        self.threadPool = threadPool
        self.initalised = False
        self.ds.cache.load_data_from_db() #create live cache
        self.last_ds_len = self.ds.number_of_results
        self.config = config
        
        self.layout = qtw.QVBoxLayout()
        
        self.widget = pg.GraphicsLayoutWidget()
        self.vb = custom_viewbox()
        self.plot = self.widget.addPlot(viewBox=self.vb)
        self.vb.setParent(self.plot)
        self.layout.addWidget(self.widget)
        
        self.initAxes()
        
        self.initRefresh(refrate)
        self.initFrame() #in plot1d, plot2d
        
        
        if show: #dont run non essential GUI functions if not displaying
            self.initLabels()
            self.initContextMenu()
            self.initMenu()
            
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
        
        if self.ds.running: #start refresh cycle if live
            self.monitor.start((int(self.spinBox.value() * 1000)))
        
    def __str__(self):
        filenameStr = get_DB_location().split('\\')[-1]
        fstr = (f"{filenameStr} | " 
                f"run ID: {self.ds.run_id} | "
                f"{self.param.name} ({self.param.label})"
                )
        return fstr

###############################################################################
#Other Methods     
    
    def initRefresh(self, refrate : float):
        self.toolbarRef = qtw.QToolBar("Refresh Timer")
        self.addToolBar(QtCore.Qt.TopToolBarArea, self.toolbarRef)
        
        if not self.ds.running:
            self.toolbarRef.hide()
        
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)

        self.toolbarRef.addWidget(qtw.QLabel("Refresh interval (s): "))
        self.toolbarRef.addWidget(self.spinBox)
    
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshWindow)
        
        if refrate > 0:
            self.spinBox.setValue(refrate)
        else:
            self.spinBox.setValue(5.0)
            
        self.toolbarRef.addSeparator()
        self.toolbarRef.addWidget(qtw.QLabel("On refresh:  "))
        self.toolbarRef.addWidget(qtw.QLabel("Re-scale"))
        
        self.rescale_refresh = qtw.QCheckBox()
        self.rescale_refresh.setChecked(True)
        self.toolbarRef.addWidget(self.rescale_refresh)
        
        
    def initLabels(self):
        self.toolbarCo_ord = qtw.QToolBar("Co-ordinates")
        self.addToolBar(QtCore.Qt.BottomToolBarArea, self.toolbarCo_ord)
        
        labelWidth = 95
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
        self.vbMenu = self.vb.menu
        
        actions = []
        for action in self.vbMenu.actions():
            actions.append(action)
            if action.text() == "View All":
                action.setText("Autoscale")
        
        self.autoscaleSep = self.vbMenu.insertSeparator(actions[1])
        
        
    def initAxes(self):
        indep_params = self.param.depends_on_
        
        self.param_dict = {self.param.name: self.param}
        
        for param in indep_params:
            param_spec = unpack_param(self.ds, param)
            self.param_dict[param_spec.name] = param_spec
        
        self.axes_dock = QDock_context("Line control", self)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.axes_dock)
        
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
        
        self.axis_dropdown = {"x": x_dropdown, "y": y_dropdown}
        
        if len(indep_params) == 1:
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
        
        for axis in ["x", "y"]:
            self.axis_dropdown[axis].currentIndexChanged.connect(
                                        lambda index, axis=axis: self.change_axis(axis)
                                        )
        sep = qtw.QFrame()
        sep.setFrameShape(qtw.QFrame.HLine)
        sep.setFrameShadow(qtw.QFrame.Sunken)
        
        self.axes_dock.addWidget(sep)
        
        if self.__class__.__name__ == "plot2d":
            self.axes_dock.layout.addStretch()
        
    
    def initMenu(self):
        menu = self.menuBar()
        
        main_menu = menu.addMenu("&View")
        
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(lambda: self.refreshWindow(force=True))
        main_menu.addAction(refreshAction)
        
        toolbar_menu = self.createPopupMenu()
        toolbar_menu.setTitle("Toolbars")
        main_menu.addMenu(toolbar_menu)
        
        
    @staticmethod
    def formatNum(num : float, sf : int=3) -> str:
        try:
            log = int(log10(abs(num)))
        except ValueError:
            return f"{0:.{sf}f}"
        
        if log >= sf or log < 0:
            return f"{num:.{sf}e}"
        else:
            return f"{num:.{sf - log}f}"
        
        
    def update_theme(self, config):
        self.config = config
        
        self.setStyleSheet(self.config.theme.main)
        self.config.theme.style_plotItem(self)
    
    
    #Note, this is an overwrite
    def createPopupMenu(self):
        menu = qtw.QMenu(self)
    
        # Safe collection of QToolBar and QDockWidget
        widgets = self.findChildren((qtw.QToolBar, qtw.QDockWidget))
    
        for widget in widgets:
            action = widget.toggleViewAction()
            if isinstance(action, qtw.QAction):
                menu.addAction(action)
    
        return menu
        
    
    def axis_options(self):
        return {k: v.currentText() for k, v in self.axis_dropdown.items()}
    
    
    def load_data(self, wait_on_thread=False):
        worker = self.loader(self.ds, self.param, self.param_dict, self.axis_options())
        
        # self.loader defined in plot<1/2>d.initRefresh()
        worker.emitter.finished.connect(self.refreshPlot)
        worker.emitter.errorOccurred.connect(self.err_raiser)
        worker.emitter.printer.connect(self.worker_printer)
        
        if wait_on_thread:     
            hold_up = QtCore.QEventLoop()
            worker.emitter.finished.connect(hold_up.quit)
            
        self.worker = worker
        self.threadPool.start(worker)
    
        if wait_on_thread:
            hold_up.exec()
            self.worker.emitter.finished.disconnect(hold_up.quit)
            
    
###############################################################################
#Events
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        self.monitor.stop()
        self.closed.emit(self) 
        del self


    @QtCore.pyqtSlot(object)
    def mouseMoved(self, pos):
        
        if not self.plot.sceneBoundingRect().contains(pos):
            return
    
        self.mousePoint = self.plot.vb.mapSceneToView(pos)
        
        x_txt = f"x = {self.formatNum(self.mousePoint.x())};"
        y_txt = f"y = {self.formatNum(self.mousePoint.y())}"
        
        if self.pos_labels.get("z", 0):
            
            y_txt += ";"
            
            image_data = self.image.image
            
            rect = self.rect
            
            i = (self.mousePoint.x() - rect.x()) / rect.width()
            j = (self.mousePoint.y() - rect.y()) / rect.height()
            
            if (i >= 0 and i <= 1) and (j >= 0 and j <= 1):
                i = int(i * image_data.shape[0])
                j = int(j * image_data.shape[1])
                self.pos_labels["z"].setText(f"z = {self.formatNum(image_data[i, j])}")

        self.pos_labels["x"].setText(x_txt)
        self.pos_labels["y"].setText(y_txt)
        
            
    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000)) #convert to seconds
            
            
    @QtCore.pyqtSlot()
    def refreshWindow(self, force : bool = False, wait_on_thread : bool = False):
        self.monitor.stop()
        retry = False
        
        print("Trying refresh")

        try:
            #Plot has started
            if not self.initalised:
                print("not Init")
                self.initFrame() #defined in children classes
                retry = True
                return
            
            if self.ds.number_of_results != self.last_ds_len or force:
                print("Attempting reload")
                if self.worker.running:
                    if not force: #restart loading process in event of force
                        print("Loaded, quitting")
                        return
                    
                print("Loading")
                self.load_data(wait_on_thread=wait_on_thread)

        finally: #Ran after return
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
    def refreshPlot(self, finished):
        print("Refreshing")
        if self.worker.df.empty or not finished:
            return
        
        #set data to be called by plot<1/2>d.refreshPlot()
        self.depvarData = self.worker.depvarData
        self.axis_data = {
            "x": self.worker.axis_data["x"].copy(), 
            "y": self.worker.axis_data["y"].copy()
            }
        self.axis_param = {
            "x": self.worker.axis_param["x"], 
            "y": self.worker.axis_param["y"]
            }
        
        
    @QtCore.pyqtSlot(Exception)
    def err_raiser(self, err : Exception):
        print("WORKER ERROR:")
        raise err
        
        
    @QtCore.pyqtSlot(str)
    def worker_printer(self, fstr : str):
        print(fstr)
    
    
    @QtCore.pyqtSlot()
    def change_axis(self, key):
        duplicates = [k for k, v in self.axis_dropdown.items() 
                          if self.axis_dropdown[key].currentText() == v.currentText()
                          and k != key
                     ]
        if len(duplicates) == 1:
            self.axis_dropdown[duplicates[0]].blockSignals(True)
            
            self.axis_dropdown[duplicates[0]].setCurrentIndex(
                self.axis_dropdown[duplicates[0]].findText(self.axis_param[key].name)
                )
            
            self.axis_dropdown[duplicates[0]].blockSignals(False)
            
        elif len(duplicates) > 1:
            raise ValueError("Too many duplicates in axis assertion.\nThis should not be possible?")
        
        self.refreshWindow(force=True, wait_on_thread=True)
        
        self.plot.setLabel(axis="bottom", text=f"{self.axis_param['x'].label} ({self.axis_param['x'].unit})")
        self.plot.setLabel(axis="left", text=f"{self.axis_param['y'].label} ({self.axis_param['y'].unit})")
        
        self.plot.enableAutoRange(True)
        if hasattr(self, "scaleColorbar"):
            self.scaleColorbar()
        