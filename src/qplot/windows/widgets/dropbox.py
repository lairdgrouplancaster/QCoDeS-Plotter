from PyQt5 import QtCore, QtWidgets as qtw
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt

class picker_1d(qtw.QWidget):
    itemSelected = QtCore.pyqtSignal([str])
    closed = QtCore.pyqtSignal([str])
    
    del_but_width = 15
    color_box_width = 75
    
    def __init__(self, cfg, items, *args, **kargs):
        super().__init__()
        
        layout = qtw.QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        row_1 = qtw.QHBoxLayout()
        row_2 = qtw.QHBoxLayout()
        
        self.option_box = expandingComboBox(*args, **kargs)
        self.option_box.setSizePolicy(qtw.QSizePolicy.Expanding, qtw.QSizePolicy.Fixed)
        self.reset_box(items)
        self.option_box.currentIndexChanged.connect(self.selectedOption)
        
        row_1.addWidget(self.option_box)
        
        
        self.del_box = qtw.QPushButton("X")
        self.del_box.setFixedWidth(self.del_but_width)
        self.del_box.setDisabled(True)
        self.del_box.clicked.connect(self.deleteBox)
        row_1.addWidget(self.del_box)
        
        self.color_box = colorBox(cfg)
        self.color_box.setFixedWidth(self.color_box_width)
        
        row_2.addStretch()
        row_2.addWidget(qtw.QLabel("Color: "))
        row_2.addWidget(self.color_box)
        
        layout.addLayout(row_1)
        layout.addLayout(row_2)
        
    
    def reset_box(self, items):
        self.option_box.blockSignals(True)
        
        self.option_box.clear()
        self.option_box.addItems(items)
        
        self.option_box.setEditable(True)
        self.option_box.lineEdit().setReadOnly(True)
        self.option_box.lineEdit().setPlaceholderText("Add to Plot")
        self.option_box.setCurrentIndex(-1)
        
        self.option_box.blockSignals(False)
        
        
    @QtCore.pyqtSlot(int)
    def selectedOption(self, index):
        self.option_box.setDisabled(True)
        self.del_box.setEnabled(True)
        
        # print(self.option_box.count())
        # if self.option_box.count() == 1:
        #     self.option_box.addItem(self.option_box.currentText())
        
        font_metrics = self.option_box.view().fontMetrics()
        text_width = font_metrics.boundingRect(self.option_box.currentText()).width()
        
        self.option_box.setMinimumWidth(text_width + 10)
        
        self.itemSelected.emit(self.option_box.currentText())
   
    @QtCore.pyqtSlot()
    def deleteBox(self):
        
        self.setParent(None)
        self.deleteLater()
        
        self.closed.emit(self.option_box.currentText())
    

    
class expandingComboBox(qtw.QComboBox):
    def showPopup(self):
        
        max_width = 0
        font_metrics = self.view().fontMetrics()
        for i in range(self.count()):
            text_width = font_metrics.boundingRect(self.itemText(i)).width()
            max_width = max(max_width, text_width)

        max_width += 5

        self.view().setMinimumWidth(max_width)

        super().showPopup()     
        
    def wheelEvent(self, event):
        event.ignore()  
        

class colorBox(qtw.QComboBox):
    selectedColor = QtCore.pyqtSignal([QColor])

    def __init__(self, cfg, *args, **kargs):
        super().__init__(*args, **kargs)
        
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        
        self.activated.connect(self._color_selected)
        
        for col in cfg.theme.colors:
            self.addItem('', userData = col)
            self.setItemData(self.count()-1, col, Qt.BackgroundRole)
        self.addItem('Custom')
     
        
    def color(self):
        return self._currentColor
    
    
    def setColor(self, color):
        self._color_selected(color=color, emitSignal=False)

    
    @QtCore.pyqtSlot(int)
    def _color_selected(self, index = None, color = None, emitSignal = True):
        if index and self.itemText(index) == "Custom":
            self.setCurrentText("") 
            color = qtw.QColorDialog.getColor()

            if color.alpha() == 255:
                return
            
            self._currentColor = color
            
        elif color:      
            self._currentColor = color
            
        else:
            self._currentColor = self.itemData(index)
            self.setCurrentIndex(self.findData(self._currentColor))
            
        
        self.lineEdit().setStyleSheet("background-color: " + self._currentColor.name())
        if emitSignal:
            self.selectedColor.emit(self._currentColor)
        
        
    def wheelEvent(self, event):
        event.ignore()  
        