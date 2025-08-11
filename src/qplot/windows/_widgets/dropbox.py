from PyQt5 import QtCore, QtWidgets as qtw
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt

class picker_1d(qtw.QWidget):
    """
    A Custom QWidget that houses widgets for controlling line in plot1d
    Contains 4 interactable widget:
        self.option_box: dropdown menu for user selection
        self.del_box: On click removes line from plot
        self.axis_side: Control which side the plot is attached to (left or right)
        self.color_box: dropdown menu for choosing line colour
    
    """
    itemSelected = QtCore.pyqtSignal([str])
    closed = QtCore.pyqtSignal([str])
    
    del_but_width = 15
    color_box_width = 75
    
    def __init__(self, main, cfg, items, *args, **kargs):
        super().__init__()
        
        layout = qtw.QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        #produce rows with context menu and context manager from dockWidget
        row_1 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        row_2 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        
        # Selection box
        self.option_box = expandingComboBox(*args, **kargs)
        self.option_box.setSizePolicy(qtw.QSizePolicy.Expanding, qtw.QSizePolicy.Fixed)
        self.reset_box(items)
        self.option_box.currentIndexChanged.connect(self.selectedOption)
        row_1.addWidget(self.option_box)
        
        # Removal box
        self.del_box = qtw.QPushButton("X")
        self.del_box.setFixedWidth(self.del_but_width)
        self.del_box.setToolTip("Remove Plot")
        self.del_box.setDisabled(True)
        self.del_box.clicked.connect(self.deleteBox)
        row_1.addWidget(self.del_box)
        
        
        row_2.addWidget(qtw.QLabel("Side: "))
        
        # Axis side picker
        self.axis_side = expandingComboBox()
        self.axis_side.addItems(["Left", "Right"])
        self.axis_side.setCurrentIndex(0)
        row_2.addWidget(self.axis_side)
        
        row_2.addStretch()
        row_2.addWidget(qtw.QLabel("Color: "))
        
        # Line colour picker
        self.color_box = colorBox(cfg)
        self.color_box.setFixedWidth(self.color_box_width)
        row_2.addWidget(self.color_box)
        
        layout.addLayout(row_1)
        layout.addLayout(row_2)
        
    
    def reset_box(self, items):
        """
        Resets the widget to default and sets selectable items

        Parameters
        ----------
        items : list[str]
            List of items that the user can select from

        """
        self.option_box.blockSignals(True)
        
        # Reset selection
        self.option_box.clear()
        if items:
            self.option_box.addItems(items)
        
        # Reset display
        self.option_box.setEditable(True)
        self.option_box.lineEdit().setReadOnly(True)
        self.option_box.lineEdit().setPlaceholderText("Add to Plot")
        self.option_box.setCurrentIndex(-1) # set to placeholder
        
        self.option_box.blockSignals(False)
        
        
    @QtCore.pyqtSlot(int)
    def selectedOption(self, index):
        """
        Event handler for selecting a option in self.option_box

        Parameters
        ----------
        Unused but required by slot

        """
        self.option_box.setDisabled(True)
        self.del_box.setEnabled(True)
        
        # Find text width
        font_metrics = self.option_box.view().fontMetrics()
        text_width = font_metrics.boundingRect(self.option_box.currentText()).width()
        
        # Set width with small pad, ScrollArea which is placed in struggles to
        # correctly fetch width when only 1 option
        self.option_box.setMinimumWidth(text_width + 10)
        
        # Emit to plot1d for further handling
        self.itemSelected.emit(self.option_box.currentText())
   
    @QtCore.pyqtSlot()
    def deleteBox(self):
        """
        Event handler for del_box pressed
        Removes widget.

        """
        # Removal
        self.setParent(None)
        self.deleteLater()
        
        # Emit to plot1d for further handling
        self.closed.emit(self.option_box.currentText())
    

    
class expandingComboBox(qtw.QComboBox):
    """
    A custom QComboBox which allows dropdown options to be larger than ComboBox
    
    """
    def showPopup(self):
        # Get width of largest item in options
        max_width = 0
        font_metrics = self.view().fontMetrics()
        for i in range(self.count()):
            text_width = font_metrics.boundingRect(self.itemText(i)).width()
            max_width = max(max_width, text_width)

        max_width += 5

        # Update width
        self.view().setMinimumWidth(max_width)

        # Display options
        super().showPopup()     
        
    def wheelEvent(self, event):
        """
        Prevents scrolling on box as it can cause accidental selection, by 
        prevent ignoring signal.

        Parameters
        ----------
        event : PyQt5.<something?>.QGraphicsSceneWheelEvent

        """
        event.ignore()  
        

class colorBox(qtw.QComboBox):
    """
    A Custom QComboBox which can display colour options and emit those colours
    through signals.
    
    """
    selectedColor = QtCore.pyqtSignal([QColor])

    def __init__(self, cfg, *args, **kargs):
        super().__init__(*args, **kargs)
        
        # Required to allow changes but prevent user typing
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        
        self.activated.connect(self._color_selected)
        
        # Get options from config.themes
        for col in cfg.theme.colors:
            self.addItem('', userData = col)
            self.setItemData(self.count()-1, col, Qt.BackgroundRole)
        self.addItem('Custom')
     
        
    def color(self):
        """
        Returns the currently select color

        Returns
        -------
        PyQt5.QtGui.QColor
            Returns the currently select color.

        """
        return self._currentColor
    
    
    def setColor(self, color):
        """
        Sets color to selected color

        Parameters
        ----------
        color : PyQt5.QtGui.QColor
            Color to display.

        """
        self._color_selected(color=color, emitSignal=False)

    
    @QtCore.pyqtSlot(int)
    def _color_selected(self, index = None, color = None, emitSignal = True):
        """
        Event handler for selecting an option

        Parameters
        ----------
        index : int, optional
            The index of the selection from the options.
        color : PyQt5.QtGui.QColor, optional
            Force override to a specific color. The default is None.
        emitSignal : bool, optional
            Whether to emit a signal to main. The default is True.

        """
        # Allow user to select a custom colour
        if index and self.itemText(index) == "Custom":
            self.setCurrentText("") 
            color = qtw.QColorDialog.getColor()

            # Check user did note exit out
            if not color.isValid():
                return
            
            self._currentColor = color
            
        # Force setting a color
        elif color:      
            self._currentColor = color
            
        # Select from preset colours
        else:
            self._currentColor = self.itemData(index)
            self.setCurrentIndex(self.findData(self._currentColor))
            
        # Update visual display of dropdown
        self.lineEdit().setStyleSheet("background-color: " + self._currentColor.name())
        # Emit signal to main for further handling
        if emitSignal:
            self.selectedColor.emit(self._currentColor)
        
        
    def wheelEvent(self, event):
        """
        Prevents scrolling on box as it can cause accidental selection, by 
        prevent ignoring signal.

        Parameters
        ----------
        event : PyQt5.<something?>.QGraphicsSceneWheelEvent

        """
        event.ignore()  
        