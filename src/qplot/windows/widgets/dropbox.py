from PyQt5 import QtCore, QtWidgets as qtw
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt

class picker_1d(qtw.QWidget):
    itemSelected = QtCore.pyqtSignal([str])
    closed = QtCore.pyqtSignal([str])
    
    def __init__(self, cfg, items, *args, **kargs):
        super().__init__()
        
        layout = qtw.QGridLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        self.option_box = expandingComboBox(*args, **kargs)
        self.reset_box(items)
        self.option_box.currentIndexChanged.connect(self.selectedOption)
        
        layout.addWidget(self.option_box, 0, 0, 1, 4)
        
        self.del_box = qtw.QPushButton("X")
        self.del_box.setFixedWidth(15)
        self.del_box.setDisabled(True)
        self.del_box.clicked.connect(self.deleteBox)
        layout.addWidget(self.del_box, 0, 4, 1, 1)
        
        self.color_box = colorBox(cfg)
        self.color_box.setFixedWidth(75)
        
        layout.addWidget(qtw.QLabel("Color:"), 1, 1, 1, 2)
        layout.addWidget(self.color_box, 1, 3, 1, 3)
        
    
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
     
        
    def color(self):
        return self._currentColor
     
        
    def setColor(self, color):
        self._color_selected(self.findData(color), False)

    
    def _color_selected(self, index, emitSignal = True):
        # if a color is selected, emit the selectedColor signal      
        self._currentColor = self.itemData(index)
        if (emitSignal):
            self.selectedColor.emit(self._currentColor)
        
        # make sure that current color is displayed
        if (self._currentColor):
            self.setCurrentIndex(self.findData(self._currentColor))
            self.lineEdit().setStyleSheet("background-color: "+self._currentColor.name())
            