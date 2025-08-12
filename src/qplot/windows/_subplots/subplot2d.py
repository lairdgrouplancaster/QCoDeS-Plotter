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
    
    
    def __init__(self, sweep_indep, fixed_indep, fixed_index, *args, **kargs):
        self.sweep_indep = sweep_indep
        self.fixed_indep = fixed_indep
        self.fixed_index = fixed_index
        
        self.line = None
        
        super().__init__(*args, **kargs)
        
        
    def initAxes(self):
        # Got back to default before line picker
        super().initAxes()
        
        # Set correct display on axis picker
        self.axis_dropdown["x"].blockSignals(True)
        self.axis_dropdown["x"].setCurrentIndex(
            self.axis_dropdown["x"].findText(self.sweep_indep)
            )
        self.axis_dropdown["x"].blockSignals(False)
        
        self.axis_dropdown["y"].blockSignals(True)
        self.axis_dropdown["y"].setEditable(True)
        self.axis_dropdown["y"].lineEdit().setReadOnly(True)
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
        self.axes_dock.addWidget(main_line)
        main_line.adjustSize()
        
        # Add picker for changing sweep location
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
        super().refreshPlot(finished)
        
        # Get correct row and param for y data
        self.axis_param["y"] = self.param
        
        # Set-up fixed axis picker and slide
        # Match range to index of x data
        self.picker.slider.setRange(0, len(self.axis_data["x"]) - 1)
        
        # Refresh plot
        # blank text_box means slider signal blocked, during axis switch
        if not self.picker.text_box.text():
            self.picker.slider.blockSignals(False)
            
            self.picker.slider.setValue(self.fixed_index) # Calls update_sweep
            self.picker.text_box.setText(
                self.formatNum(self.axis_data["x"][self.fixed_index])
                )
            
        else:
            self.update_sweep()
        
        
        
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
        
###############################################################################
# Events/Slots
    
    def update_sweep(self):
        # Get correct row for y data
        self.axis_data["y"] = self.dataGrid[:, self.fixed_index]
        
        # update line
        self.line.setData(
            x=self.axis_data["x"], 
            y=self.axis_data["y"],
            )


    @QtCore.pyqtSlot(int)
    def change_index(self, index):
        # Update display box
        self.picker.text_box.setText(
            self.formatNum(self.axis_data["x"][index])
            )
        
        # Update plot
        self.fixed_index = index
        self.update_sweep()
        
    
    @QtCore.pyqtSlot(int)
    def change_fixed_param(self, index):
        if self.picker.option_box.currentText() == self.axis_dropdown["x"].currentText():
            self.axis_dropdown["x"].blockSignals(True)
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(self.fixed_indep)
                )
            self.axis_dropdown["x"].blockSignals(True)
            
            # Switch data
            self.axis_data["x"] = self.worker.axis_data["y"]
            self.dataGrid = self.dataGrid.transpose()
            
            # Switch worker data to track changes
            self.worker.axis_data["y"] = self.worker.axis_data["x"]
            self.worker.axis_data["x"] = self.axis_data["x"]
        
            # Update slider
            self.picker.slider.setValue(0) # Also emits refresh signal
            self.picker.slider.setRange(0, len(self.axis_data["x"]) - 1)
            
        else:
            
            self.picker.slider.blockSignals(True)
            self.picker.text_box.setText("") # Let refreshPlot know signal is blocked
            
            # Get new data
            self.refreshWindow(force=True) # wait_on_thread else param data is not updated
            
    
    
    
    
    
    
    
    
class fixed_var_picker(qtw.QWidget):
    
    def __init__(self, main, items):
        super().__init__()
        
        layout = qtw.QVBoxLayout(self)
        
        # Set up layouts with customised context menus
        row_1 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        row_2 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        
        row_1.addWidget(qtw.QLabel("Fixed Varaible: "))
        
        self.option_box = expandingComboBox()
        self.option_box.addItems(items)
        row_1.addWidget(self.option_box)
        
        self.slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setTickPosition(qtw.QSlider.TickPosition.TicksBelow)
        row_2.addWidget(self.slider)
        
        self.text_box = qtw.QLineEdit()
        self.text_box.setReadOnly(True)
        self.text_box.setMaximumWidth(95)
        row_2.addWidget(self.text_box)
        
        layout.addLayout(row_1)
        layout.addLayout(row_2)
        