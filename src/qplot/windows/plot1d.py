from qplot.windows.plotWin import plotWidget
from qplot.windows.widgets import picker_1d
from qplot.tools.subplot import subplot1d

from PyQt5 import (
    QtWidgets as qtw,
    QtCore,
    )


class plot1d(plotWidget):
    
    def __init__(self, 
                 *args,
                 **kargs
                 ):
        self.mergable = None
        self.line = None
        super().__init__(*args, **kargs)
        
        
    def initFrame(self):
        if self.df.empty:
            return
        
        self.line = self.plot.plot()
        
        self.refreshPlot()
        
        self.plot.setLabel(axis="bottom", text=f"{self.axis_param['x'].label} ({self.axis_param['x'].unit})")
        self.plot.setLabel(axis="left", text=f"{self.axis_param['y'].label} ({self.axis_param['y'].unit})")
        
        self.initalised = True
        print("Graph produced \n")
        
        
    def refreshPlot(self):
        self.line.setData(
            x=self.axis_data["x"], 
            y=self.axis_data["y"],
            )
        for line in list(self.lines.values())[1:]:
            # print(row)
            line.refresh()
        # self.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
        
###############################################################################
#Line control
   
    def initAxes(self):
        super().initAxes()
        
        self.toolbarAxes.addSeparator()
        
        self.toolbarAxes.addWidget(qtw.QLabel("Line Control"))
        self.lines = {self.label : self.line}
        self.option_boxes = []
        
        main_line = picker_1d(self.config, [self.label])
        main_line.option_box.setCurrentIndex(0)
        main_line.option_box.setDisabled(True)
        main_line.del_box.setDisabled(True)
        main_line.color_box.setColor(self.config.theme.colors[0])
        
        self.toolbarAxes.addWidget(main_line)
        self.add_option_box(options=[""])
    
    
    def add_option_box(self, options = None):
        if options:
            new_option = picker_1d(self.config, options)
        else:
            new_option = picker_1d(self.config, [item.label for item in self.mergable])
        
        new_option.itemSelected.connect(self.add_line)
        new_option.closed.connect(self.remove_line)
        
        self.option_boxes.append(new_option)
        self.toolbarAxes.addWidget(new_option)
    
    
    def update_line_picker(self, wins = None):
        if wins:
            self.mergable = wins
        
        for box in self.option_boxes:
            if box.option_box.isEnabled():
                box.reset_box([item.label for item in self.mergable])
    
    
    @QtCore.pyqtSlot(str)
    def add_line(self, label):
        for item in self.mergable:
            if item.label == label:
                win = item
                break
        
        subplot = subplot1d(self, win)
        self.lines[label] = subplot
        
        self.add_option_box()
        
    
    @QtCore.pyqtSlot(str)
    def remove_line(self, label):
        
        for option in self.option_boxes:
            if option.option_box.currentText() == label:
                self.option_boxes.remove(option)
                break
        
        self.plot.removeItem(self.lines[label])