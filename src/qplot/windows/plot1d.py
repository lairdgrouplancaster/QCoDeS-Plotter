from qplot.windows.plotWin import plotWidget
from qplot.windows.widgets import picker_1d

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
        
        self.plot.setLabel(axis="bottom", text=f"{self.xaxis_param.label} ({self.xaxis_param.unit})")
        self.plot.setLabel(axis="left", text=f"{self.yaxis_param.label} ({self.yaxis_param.unit})")
        
        self.initalised = True
        print("Graph produced \n")
        
        
    def refreshPlot(self):
        self.line.setData(
            x=self.xaxis_data, 
            y=self.yaxis_data,
            )
        # self.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
        
###############################################################################
#Line control
   
    def initAxes(self):
        super().initAxes()
        
        self.toolbarAxes.addSeparator()
        
        self.toolbarAxes.addWidget(qtw.QLabel("Line Control"))
        self.lines = {"main" : self.line}
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
        
        subplot = self.plot.plot()
        self.lines[label] = [win, subplot]
        
        
        