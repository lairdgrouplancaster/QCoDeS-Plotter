from qplot.windows.plotWin import plotWidget
from qplot.windows._widgets import picker_1d
from qplot.tools.subplot import subplot1d

from PyQt5 import (
    QtWidgets as qtw,
    QtCore,
    )

import pyqtgraph as pg


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
            line.refresh()
        # self.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
        
###############################################################################
#Line and Subplots control
   
    def initAxes(self):
        super().initAxes()
        
        
        self.axes_dock.addWidget(qtw.QLabel("Line Control"))
        self.lines = {self.label : self.line}
        self.option_boxes = []
        self.box_count = 1
        
        
        self.lineScroll = qtw.QScrollArea()
        self.lineScroll.setWidgetResizable(True)
        self.lineScroll.setMinimumSize(1, 1)
        self.lineScroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.axes_dock.addWidget(self.lineScroll)
        
        self.scrollWidget = qtw.QWidget()
        self.lineScroll.setWidget(self.scrollWidget)
        
        self.box_layout = qtw.QVBoxLayout()
        self.box_layout.setContentsMargins(0, 0, 0, 0)
        self.scrollWidget.setLayout(self.box_layout)
        
        main_line = picker_1d(self, self.config, [self.label])
        main_line.option_box.setCurrentIndex(0)
        main_line.option_box.setDisabled(True)
        main_line.del_box.setDisabled(True)
        main_line.axis_side.setDisabled(True)
        main_line.color_box.setColor(self.config.theme.colors[0])
        main_line.color_box.selectedColor.connect(
            lambda col: self.set_color(col, self.line)
            )
        self.box_layout.addWidget(main_line)
        main_line.adjustSize()
        
        self.box_layout.addStretch()
        
        self.add_option_box(options=[])
        
        
    def _resize_scrollArea(self):
        self.scrollWidget.adjustSize()
        scrollWidth = (
            self.scrollWidget.sizeHint().width() +
            2 *  self.lineScroll.frameWidth() +
            self.lineScroll.verticalScrollBar().sizeHint().width()
            )
        self.lineScroll.setMinimumWidth(scrollWidth)
        
        
    def add_option_box(self, options = None):
        if options is not None:
            new_option = picker_1d(self, self.config, options)
        else:
            new_option = picker_1d(self, self.config, [item.label for item in self.mergable])
        
        new_option.itemSelected.connect(lambda label: self.add_line(label))
        new_option.closed.connect(self.remove_line)
        
        cols = self.config.theme.colors
        col_ind = self.box_count % len(cols)
        new_option.color_box.setColor(cols[col_ind])
        self.box_count += 1
        
        self.option_boxes.append(new_option)
        self.box_layout.insertWidget(self.box_layout.count() - 1, new_option)
        
        self._resize_scrollArea()
        
    
    
    def update_line_picker(self, wins = None):
        if wins:
            self.mergable = wins
        
        if self.option_boxes and self.mergable:
            box_texts = [box.option_box.currentText() for box in self.option_boxes]
            for box in self.option_boxes:
                if box.option_box.isEnabled():
                    self.option_boxes[-1].reset_box([item.label for item in self.mergable if item.label not in box_texts])
    
    
    @QtCore.pyqtSlot(str)
    def add_line(self, label):
        
        win = None
        
        for item in self.mergable:
            if item.label == label:
                win = item
                self.mergable.remove(item)
                break
        
        assert win is not None
        
        self.add_option_box()
        
        subplot = subplot1d(self, win)
        self.lines[label] = subplot
        
        for box in self.option_boxes:
            if label == box.option_box.currentText():
                
                box.color_box.selectedColor.connect(
                    subplot.set_color
                    )
                
                box.axis_side.currentTextChanged.connect(
                    subplot.set_side
                    )
                break
        
        assert box is not None
        
        subplot.set_color(box.color_box.color())
        subplot.set_side(box.axis_side.currentText().lower())
        
    
    @QtCore.pyqtSlot(str)
    def remove_line(self, label):
        
        for option in self.option_boxes:
            if option.option_box.currentText() == label:
                self.option_boxes.remove(option)
                break
        
        self.plot.removeItem(self.lines[label])
        self.update_line_picker()
        
        self._resize_scrollArea()
    
        