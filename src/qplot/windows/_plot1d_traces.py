from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

import pyqtgraph as pg

from ._widgets import picker_1d
from ._subplots import subplot1d


class Plot1DTraceMixin:
    """Trace controls and secondary-axis handling for 1D plot windows."""

    def _register_main_line(self):
        """
        Keeps the main pyqtgraph line in the trace registry.

        """
        if hasattr(self, "lines"):
            self.lines[self.label] = self.line


    def initAxes(self):
        """
        Adds to the base axis toolbar (left) to allow adding and removing 
        secondary lines along with changing color.

        """
        super().initAxes()
        
        self.axes_dock.addWidget(qtw.QLabel("Line Control"))
        
        # Store all line data and boxes for later use
        self.lines = {}
        self._register_main_line()
        self.option_boxes = []
        self.box_count = 1
        
        # Produce scrollable widget to allow viewing of as many lines as needed
        self.lineScroll = qtw.QScrollArea()
        self.lineScroll.setWidgetResizable(True)
        self.lineScroll.setMinimumSize(1, 1)
        self.lineScroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.axes_dock.addWidget(self.lineScroll)
        
        # QScrollArea can only take 1 widget. That widget holds the layout.
        self.scrollWidget = qtw.QWidget()
        self.lineScroll.setWidget(self.scrollWidget)
        
        self.box_layout = qtw.QVBoxLayout()
        self.box_layout.setContentsMargins(0, 0, 0, 0)
        self.scrollWidget.setLayout(self.box_layout)
        
        # Main line controller
        main_line = picker_1d(self, self.config, [self.label])
        main_line.option_box.setCurrentIndex(0)
        main_line.option_box.setDisabled(True)
        main_line.del_box.setDisabled(True)
        main_line.axis_side.setDisabled(True)
        main_line.color_box.setColor(self.config.theme.colors[0])
        main_line.color_box.selectedColor.connect(
            lambda col: self.line.setPen(col)
            )
        self.box_layout.addWidget(main_line)
        main_line.adjustSize()
        
        # Force to top
        self.box_layout.addStretch()
        
        # Add empty box for user to use
        self.add_option_box(options=[])
        
        
    def _resize_scrollArea(self):
        """
        Updates the width of the dock widget to match the width of the largest
        row in the Scroll area so all data is visible

        Note. Prevents user from making dock widget any smaller.
        Adding self.lineScroll.setMinimumWidth(1) should fix this but my attempts
        have failed.
        """
        self.scrollWidget.adjustSize()
        # Get scrollArea width
        scrollWidth = (
            self.scrollWidget.sizeHint().width() +
            2 *  self.lineScroll.frameWidth() +
            self.lineScroll.verticalScrollBar().sizeHint().width()
            )
        self.lineScroll.setMinimumWidth(scrollWidth)
        
        
    def add_option_box(self, options = None):
        """
        Produces a new box for user to add another line to the plot.
        Boxes are made from a QWidget, see qplot.windows._widgets.dropbox.picker_1d
        for how they are produced.

        Parameters
        ----------
        options : list[str], optional
            List of item to add to the dropdown menu to pick from. 
            The default is None, which adds self.mergable or all valid windows
            which can be added.

        """
        if options is not None:
            new_option = picker_1d(self, self.config, options)
        else:
            new_option = picker_1d(self, self.config, [item.label for item in self.mergable])
        
        # Connect Slots
        new_option.itemSelected.connect(lambda label: self.add_line(label))
        new_option.closed.connect(self.remove_line)
        
        # Adjust apperance
        cols = self.config.theme.colors
        col_ind = self.box_count % len(cols)
        new_option.color_box.setColor(cols[col_ind])
        self.box_count += 1
        
        # Add box to tracking array and then to last possition in ScrollWidget
        self.option_boxes.append(new_option)
        self.box_layout.insertWidget(self.box_layout.count() - 1, new_option)
        
        # Resize after adding box. This func is also ran after removing a box
        # which is the main reason for it
        self._resize_scrollArea()
        
    
    def update_line_picker(self, wins = None):
        """
        Refreshes the available options in the box dropdown menus.

        Parameters
        ----------
        wins : list[plotWidget], optional
            Updates internal save of plots which can be added.

        """
        if wins:
            self.mergable = wins
        
        # Only add options which are not already being plotted
        if self.option_boxes and self.mergable:
            box_texts = [box.option_box.currentText() for box in self.option_boxes]
            for box in self.option_boxes:
                if box.option_box.isEnabled():
                    self.option_boxes[-1].reset_box([item.label for item in self.mergable if item.label not in box_texts])


    def refresh_secondary_lines(self):
        """
        Refreshes added trace lines and restarts hidden live-trace monitors.

        """
        for line in list(self.lines.values())[1:]:
            line.refresh()
            from_win = line.from_win
            if (
                not from_win.visible
                and from_win.ds.running
                and not from_win.monitor.isActive()
                ):
                from_win.spinBox.setValue(self.spinBox.value())
                from_win.monitor.start(int(self.spinBox.value() * 1000))
                self.spinBox.valueChanged.connect(line.from_win.spinBox.setValue)
    
    
    @QtCore.pyqtSlot(str)
    def add_line(self, label):
        """
        Produces a secondary plot based on user selection in dropdown menus
        
        See both subplot1d and custom_viewbox in:
            qplot.tools.subplots
        for setup and other functions

        Parameters
        ----------
        label : str
            The label of the chosen plot.

        Returns
        -------
        None.

        """
        
        win = None
        
        # Find selected window from open windows.
        for item in self.mergable:
            if item.label == label:
                win = item
                self.mergable.remove(item)
                break
        
        # Dedug line
        assert win is not None
        
        # Initialise right axis if not already done. 
        if not self.right_vb:
            #Create viewbox for right axis and add viewbox to main plot widget
            self.right_vb = pg.ViewBox()
            self.right_vb.setDefaultPadding(0)
            self.plot.scene().addItem(self.right_vb)
            
            self.plot.getAxis('right').linkToView(self.right_vb)
            self.right_vb.setXLink(self.plot)
            
            #connect pan/scale signals
            self.updateViews(None)
            self.vb.main_moved.connect(self.updateViews) # main_moved in .tools.subplots
            
            # Connect bottom left autoscale button to right axis
            self.plot.autoBtn.clicked.connect(
                lambda: self.right_vb.enableAutoRange() if self.plot.autoBtn.mode == 'auto'
                        else self.right_vb.disableAutoRange()
                )
            self.vb.autoRange_triggered.connect(self.right_vb.autoRange)
            
        # Produce new box to allow another selection
        self.add_option_box()
        
        # Create and track new line
        self.make_ds.emit(win._guid)
        subplot = subplot1d(self, win)
        self.lines[label] = subplot
        
        self.plot.getAxis('right').setStyle(showValues=True)
        
        # Connect box options to line
        for box in self.option_boxes:
            if label == box.option_box.currentText():
                
                box.color_box.selectedColor.connect(
                    subplot.set_color
                    )
                
                box.axis_side.currentTextChanged.connect(
                    subplot.set_side
                    )
                break
        
        # debug line
        assert box is not None
        
        # Set display
        subplot.set_color(box.color_box.color())
        subplot.set_side(box.axis_side.currentText().lower())
        
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        # Stopped lines as needed
        for line in list(self.lines.values())[1:]:
            self.remove_dataset.emit(line.from_win._guid)
            if not line.from_win.visible:
                line.from_win.monitor.stop()
                
            
        super().closeEvent(event)
        
    
    @QtCore.pyqtSlot(str)
    def remove_line(self, label):
        """
        Deletes line connect to box widget.

        Parameters
        ----------
        label : str
            The label of the chosen plot.
            
        """
        # Find box and remove box
        for option in self.option_boxes:
            if option.option_box.currentText() == label:
                side = option.axis_side.currentText()
                self.option_boxes.remove(option)
                break
        
        # Remove line from viewbox
        line = self.lines[label]
        self.lines.pop(label)
        # Fetch correct viewbox to remove from
        vb = self.plot if side.lower() == "left" else self.right_vb
        vb.removeItem(line)

        if not any(
                getattr(line, "side", "left") == "right"
                for line in self.lines.values()
                ):
            self.plot.getAxis('right').setStyle(showValues=False)
        
        # Remove track of window
        self.remove_dataset.emit(line.from_win._guid)
        # Stop refresh monitor for line if needed
        if not line.from_win.visible:
            line.from_win.monitor.stop()
        
        # Update box options
        self.get_mergables.emit()
        # Resize dock widget
        self._resize_scrollArea()
    
    
    @QtCore.pyqtSlot(object)
    def updateViews(self, ev):
        """
        When moving main viewbox move/scale right viewbox but the same
        relative amount.

        Parameters
        ----------
        ev : PyQt5.<something?>
            
        """
        self.right_vb.setGeometry(self.vb.sceneBoundingRect())
        
        # Find which event 
        if ev.__class__.__name__ == "QGraphicsSceneWheelEvent":
            self.right_vb.wheelEvent(ev)
        elif ev.__class__.__name__ == "MouseDragEvent":
            self.right_vb.mouseDragEvent(ev)

        # Prevents lines from moving outside the axes.
        self.right_vb.setGeometry(self.vb.sceneBoundingRect())
