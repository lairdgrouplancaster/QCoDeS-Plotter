from PyQt5 import (
    QtCore,
    QtWidgets as qtw
    )

from qplot.windows._plotWin import plotWidget
from qplot.windows._widgets import (
    picker_1d,
    expandingComboBox,
    )


class sweeper(plotWidget):
    """
    A plotWidget which displays a 1d sweep on an 2d plot.
    Produced throught he context menu of the plotItem in plot2d.
    
    Produces a cursor on its source plot2d to display the location of the sweep.
    Both plots become linked, any changes to the cursor or sweep will update
    their counterpart.
    """
    sweep_moved = QtCore.pyqtSignal([int, str, str, int, object])
    remove_sweep = QtCore.pyqtSignal([int])
    
    def __init__(self,
                 guid : str, # Had to handle seperately to *args
                 sweep_id : int,
                 sweep_indep : str,
                 fixed_indep : str, 
                 fixed_index : int,
                 *args, 
                 **kargs
                 ):
        self.sweep_id = sweep_id
        self.sweep_indep = sweep_indep
        self.fixed_indep = fixed_indep
        self.fixed_index = fixed_index
        
        self.line = None
        
        super().__init__(guid, *args, **kargs)
        
        
    def initAxes(self):
        """
        Adds to left toolbar to allow for sweep parameter control

        """
        # Got back to default before line picker
        super().initAxes()
        
        # Set correct display on axis picker
        self.axis_dropdown["x"].blockSignals(True)
        self.axis_dropdown["x"].setCurrentIndex(
            self.axis_dropdown["x"].findText(self.sweep_indep)
            )
        self.axis_dropdown["x"].blockSignals(False)
        
        # Disable y axis box, for display only
        self.axis_dropdown["y"].blockSignals(True)
        self.axis_dropdown["y"].setEditable(True)
        self.axis_dropdown["y"].lineEdit().setReadOnly(True)
        self.axis_dropdown["y"].setDisabled(True)
        self.axis_dropdown["y"].setCurrentText(self.param.name)
        
        # add line control
        main_line = picker_1d(self, self.config, [self.label])
        main_line.option_box.setCurrentIndex(0)
        main_line.option_box.setDisabled(True)
        main_line.del_box.setDisabled(True)
        main_line.axis_side.setDisabled(True)
        main_line.color_box.setColor(self.config.theme.colors[0])
        main_line.color_box.selectedColor.connect(
            lambda col: self.line.setPen(col)
            )
        main_line.color_box.selectedColor.connect( # emit update to main
            lambda _: self.update_sweep()
            )
        self.axes_dock.addWidget(main_line)
        main_line.adjustSize()
        
        # Add picker for changing sweep location using x axis options since
        # y param is not in that
        self.picker = fixed_var_picker(
            self, 
            [self.axis_dropdown["x"].itemText(i) for i in range(self.axis_dropdown["x"].count())],
            )
        self.axes_dock.addWidget(self.picker)
        
        # Set up picker options
        self.picker.option_box.setCurrentIndex(
            self.picker.option_box.findText(self.fixed_indep)
            )
        self.picker.option_box.currentIndexChanged.connect(self.change_fixed_param)
        self.picker.slider.valueChanged.connect(self.change_index)
        self.picker.slider.blockSignals(True) # Prevent use while loading
        
        # Push all widgets to top
        self.axes_dock.layout.addStretch()
        
        
    def initFrame(self):
        """
        Sets up the initial plot and starting data.
        
        Note, is copy of plot1d.initFrame

        """
        
        self.line = self.plot.plot()
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        
        print("Graph produced \n")
        
    
    @QtCore.pyqtSlot(bool)
    def refreshPlot(self, finished : bool = True):
        """
        Event handler for worker callback
        Fetches values from data from worker in super().refreshPlot
        
        Updates display (see self.update_sweep) and slider as needed

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.

        """
        super().refreshPlot(finished)
        
        ### NOTE. self.axis_data["y"] is set to fixed param in super().refreshPlot,
        ###       then updated to y axis value (indep param) in self.update_sweep()
        self.fixed_indep_data = self.axis_data["y"]
        
        # Get correct row and param for y data
        self.axis_param["y"] = self.param
        
        # Set-up fixed axis picker and slide
        # Match range to index of fixed param data
        self.picker.slider.setRange(0, len(self.fixed_indep_data) - 1)
        
        # Refresh plot
        # blank text_box means slider signal blocked, during axis switch
        if not self.picker.text_box.text():
            self.picker.slider.setValue(self.fixed_index) 
            self.picker.text_box.setText(
                self.formatNum(self.fixed_indep_data[self.fixed_index])
                )
            
            self.update_sweep()
            self.picker.slider.blockSignals(False)
            
        else:
            self.update_sweep()
        
        self.worker.running = False
        
        
    @property
    def axis_options(self) -> dict:
        """
        Alter axis_options for correct data fetch from worker

        Returns
        -------
        dict
            The required axes data.

        """
        return {"x": self.axis_dropdown["x"].currentText(), "y": self.picker.option_box.currentText()}
        
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Post close admin, emits to 2d main plot  to remove sweep cursor

        Parameters
        ----------
        unused byt reauired by slot
        
        """
        super().closeEvent(event)
        self.remove_sweep.emit(self.sweep_id)
        
###############################################################################
# Events/Slots
    
    def update_sweep(self, emit = True):
        """
        Refresh 1d plot when there is a change in parameter or value
        
        Parameters
        ----------
        emit : bool, optional
            Whether to emit a signal to parent 2d plot. The default is True.

        """
        # Get correct row for y data
        self.axis_data["y"] = self.dataGrid[:, self.fixed_index]
        
        # update line
        self.line.setData(
            x=self.axis_data["x"], 
            y=self.axis_data["y"],
            )
        
        if emit:
            # Tell source graph to update scan line on source graph
            self.sweep_moved.emit(
                self.sweep_id,
                *self.axis_options.values(),
                self.fixed_index,
                self.line.opts['pen']
                )


    @QtCore.pyqtSlot(int)
    def change_index(self, index):
        """
        Event handler for picker.slider changing value
        Changes index value of the fixed parameter and refreshes plot.

        Parameters
        ----------
        index : int
            Value slider was changed to.

        """
        # Update display box
        self.picker.text_box.setText(
            self.formatNum(self.fixed_indep_data[index])
            )
        
        # Update plot
        self.fixed_index = index
        self.update_sweep()
        
        self.plot.setLabel(axis="bottom", text=f"{self.axis_param['x'].label} ({self.axis_param['x'].unit})")
        self.plot.setLabel(axis="left", text=f"{self.axis_param['y'].label} ({self.axis_param['y'].unit})")
            
    
    @QtCore.pyqtSlot(int)
    def change_fixed_param(self, index):
        """
        Event handler for fixed parameter dropdown selector. (picker.option_box)
        Updates the parameter on the x axis of the sweep and resets fixed 
        parameter index to 0.
        If the parameter changed to is the current sweep parameter, switches 
        them.

        Parameters
        ----------
        Unused but required by slot

        """
        # Update Slider
        self.picker.slider.blockSignals(True)
        self.picker.slider.setValue(0)
        self.fixed_index = 0
        self.picker.text_box.setText("") # Let refreshPlot know signal is blocked
        
        if self.picker.option_box.currentText() == self.axis_dropdown["x"].currentText():
            self.axis_dropdown["x"].blockSignals(True)
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(self.fixed_indep)
                )
            self.axis_dropdown["x"].blockSignals(False)
            
        # NOTE, same as self.change_axis() from here
            
            # Switch data
            temp_y_data = self.worker.axis_data["y"]
            temp_y_param = self.worker.axis_param["y"]
            self.worker.dataGrid = self.worker.dataGrid.transpose()
            
            # Switch worker data to track changes
            self.worker.axis_data["y"] = self.worker.axis_data["x"]
            self.worker.axis_data["x"] = temp_y_data
            
            self.worker.axis_param["y"] = self.worker.axis_param["x"]
            self.worker.axis_param["x"] = temp_y_param
        
            self.sweep_indep, self.fixed_indep = self.axis_options.values()
        
            self.refreshPlot() # Refresh without new data
            
        else:
            self.sweep_indep, self.fixed_indep = self.axis_options.values()
            
            # Get new data
            self.refreshWindow(force=True) 
            
    
    @QtCore.pyqtSlot()
    def change_axis(self, key : str):
        """
        Event handler for x axis dropdown selector.
        Updates the parameter on the x axis of the sweep and resets fixed 
        parameter index to 0.
        If the parameter changed to is the current fixed parameter, switches 
        them.
        

        Parameters
        ----------
        key : str
            The key of which box to change. Will be x in all cases but required
            by definition in parent

        """
        # Update Slider
        self.picker.slider.blockSignals(True)
        self.picker.slider.setValue(0)
        self.fixed_index = 0
        self.picker.text_box.setText("") # Let refreshPlot know signal is blocked
        
        if self.axis_dropdown["x"].currentText() == self.picker.option_box.currentText():
            self.picker.option_box.blockSignals(True)
            self.picker.option_box.setCurrentIndex(
                self.picker.option_box.findText(self.sweep_indep)
                )
            self.picker.option_box.blockSignals(False)
            
        # NOTE, same as self.change_fixed_param() from here
            
            # Switch data
            temp_y_data = self.worker.axis_data["y"]
            temp_y_param = self.worker.axis_param["y"]
            self.worker.dataGrid = self.worker.dataGrid.transpose()
            
            # Switch worker data to track changes
            self.worker.axis_data["y"] = self.worker.axis_data["x"]
            self.worker.axis_data["x"] = temp_y_data
            
            self.worker.axis_param["y"] = self.worker.axis_param["x"]
            self.worker.axis_param["x"] = temp_y_param
        
            self.sweep_indep, self.fixed_indep = self.axis_options.values()
        
            self.refreshPlot() # Refresh without new data
            
        else:
            self.sweep_indep, self.fixed_indep = self.axis_options.values()
            
            # Get new data
            self.refreshWindow(force=True) 
            
            
    @QtCore.pyqtSlot(int, int)
    def update_sweep_line(self, sweep_id, index):
        """
        Event handler for moving sweep cursor on source plot.

        Parameters
        ----------
        sweep_id : int
            The sweep id of the line moved. Confirms that this is the intened
            plot to adjust
        index : int
            The index that the indep variable was set to.

        """
        if sweep_id != self.sweep_id:
            return
        
        self.fixed_index = index
        
        self.picker.slider.blockSignals(True)
        self.picker.slider.setValue(index)
        self.picker.slider.blockSignals(False)
        self.picker.text_box.setText(
            self.formatNum(self.fixed_indep_data[index])
            )
        
        self.update_sweep(emit = False)

    
class fixed_var_picker(qtw.QWidget):
    """
    A custom QWidget which contains other widgets to interact with and control
    the static/fixed parameter of the heat map while viewing the 1d sweep.
    
    Contains:
        self.option_box: Changing the fixed parameter
        self.slider: change which value the plot is looking at
        self.text_box: visual display of current sweep value
        
    Note, uses custom HBoxLayout to set up context menu within dock widget.
    See qplot.windows._widgets.toolbar
    """
    
    
    def __init__(self, main, items):
        super().__init__()
        
        layout = qtw.QVBoxLayout(self)
        
        # Set up layouts with customised context menus
        row_1 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        row_2 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        
        row_1.addWidget(qtw.QLabel("Fixed Varaible: "))
        
        # Create box to change paramater
        self.option_box = expandingComboBox()
        self.option_box.addItems(items)
        row_1.addWidget(self.option_box)
        
        # Switches fixed parameter
        self.slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setTickPosition(qtw.QSlider.TickPosition.TicksBelow)
        row_2.addWidget(self.slider)
        
        # Update user to change
        self.text_box = qtw.QLineEdit()
        self.text_box.setReadOnly(True)
        self.text_box.setMaximumWidth(main._label_width)
        row_2.addWidget(self.text_box)
        
        layout.addLayout(row_1)
        layout.addLayout(row_2)
        