from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._subplots import subplot1d
from ._widgets import picker_1d

if TYPE_CHECKING:
    class _Plot1DTraceBase(qtw.QMainWindow):
        axes_dock: Any
        axis_options: dict[str, str]
        box_count: int
        box_layout: qtw.QVBoxLayout
        config: Any
        get_mergables: Any
        label: str
        line: Any
        lineScroll: qtw.QScrollArea
        lines: dict[str, Any]
        make_ds: Any
        mergable: Any
        option_boxes: list[Any]
        plot: Any
        remove_dataset: Any
        right_vb: Any
        scrollWidget: qtw.QWidget
        spinBox: Any
        vb: Any

        def initAxes(self) -> None: ...

        def closeEvent(self, event: object) -> None: ...
else:
    class _Plot1DTraceBase:
        pass


class Plot1DTraceMixin(_Plot1DTraceBase):
    @dataclass
    class _TraceStyle:
        line_enabled: bool = True
        line_color: str = "#1f77b4"
        line_width: float = 2.0
        line_style: str = "Solid"
        dots_enabled: bool = False
        dots_color: str = "#1f77b4"
        dots_size: float = 6.0
        markers_enabled: bool = False
        markers_color: str = "#1f77b4"
        markers_symbol: str = "o"
        markers_size: float = 10.0
        x_axis: str = "Bottom"
        y_axis: str = "Left"
        visible: bool = True
        order: int = 0

    """Trace controls and secondary-axis handling for 1D plot windows."""

    def _register_main_line(self) -> None:
        """
        Keeps the main pyqtgraph line in the trace registry.

        """
        trace_styles = self.__dict__.setdefault("_trace_styles", {})
        trace_styles.setdefault(self.label, self._TraceStyle())
        line = self.__dict__.get("line")
        if line is None:
            return
        if "lines" in self.__dict__:
            self.lines[self.label] = line
        self._apply_trace_style(self.label, line)

    def _set_main_line_color(self, color: QtGui.QColor) -> None:
        trace_styles = self.__dict__.setdefault("_trace_styles", {})
        style = trace_styles.setdefault(self.label, self._TraceStyle())
        style.line_color = color.name()
        self._apply_trace_style(self.label, self.__dict__.get("line"))

    def initMenu(self) -> None:
        super().initMenu()
        view_menu = None
        for action in self.menuBar().actions():
            if action.text().replace("&", "") == "View":
                view_menu = action.menu()
                break
        if view_menu is None:
            return
        view_menu.addSeparator()
        trace_action = QtGui.QAction("Trace Appearance…", self)
        trace_action.triggered.connect(self.open_trace_appearance_dialog)
        view_menu.addAction(trace_action)


    def initAxes(self) -> None:
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
        self._trace_appearance_dialog = None
        
        # Produce scrollable widget to allow viewing of as many lines as needed
        self.lineScroll = qtw.QScrollArea()
        self.lineScroll.setWidgetResizable(True)
        self.lineScroll.setMinimumSize(1, 1)
        self.lineScroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
            self._set_main_line_color
            )
        self.box_layout.addWidget(main_line)
        main_line.adjustSize()
        self._apply_trace_style(self.label, self.line)
        
        # Force to top
        self.box_layout.addStretch()
        
        # Add empty box for user to use
        self.add_option_box(options=[])
        
        
    def _resize_scrollArea(self) -> None:
        """
        Updates the width of the dock widget to match the width of the largest
        row in the Scroll area so all data is visible

        Note. Prevents user from making dock widget any smaller.
        Adding self.lineScroll.setMinimumWidth(1) should fix this but my attempts
        have failed.
        """
        self.scrollWidget.adjustSize()
        # Get scrollArea width
        vertical_scrollbar = self.lineScroll.verticalScrollBar()
        scrollbar_width = (
            vertical_scrollbar.sizeHint().width()
            if vertical_scrollbar is not None
            else 0
            )
        scrollWidth = (
            self.scrollWidget.sizeHint().width() +
            2 *  self.lineScroll.frameWidth() +
            scrollbar_width
            )
        self.lineScroll.setMinimumWidth(scrollWidth)
        
        
    def add_option_box(self, options: list[str] | None = None) -> None:
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
        
    
    def update_line_picker(self, wins: list[Any] | None = None) -> None:
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


    def refresh_secondary_lines(self) -> None:
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
    def add_line(self, label: str) -> None:
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
        selected_box = None
        for box in self.option_boxes:
            if label == box.option_box.currentText():
                
                box.color_box.selectedColor.connect(
                    subplot.set_color
                    )
                
                box.axis_side.currentTextChanged.connect(
                    subplot.set_side
                    )
                selected_box = box
                break
        
        # debug line
        assert selected_box is not None
        
        # Set display
        subplot.set_color(selected_box.color_box.color())
        subplot.set_side(selected_box.axis_side.currentText().lower())
        trace_styles = self.__dict__.setdefault("_trace_styles", {})
        style = trace_styles.setdefault(label, self._TraceStyle(order=len(trace_styles)))
        style.line_color = selected_box.color_box.color().name()
        style.y_axis = "Right" if selected_box.axis_side.currentText().lower() == "right" else "Left"
        self._apply_trace_style(label, subplot)
        
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event: object) -> None:
        # Stopped lines as needed
        for line in list(self.lines.values())[1:]:
            self.remove_dataset.emit(line.from_win._guid)
            if not line.from_win.visible:
                line.from_win.monitor.stop()
                
            
        super().closeEvent(event)
        
    
    @QtCore.pyqtSlot(str)
    def remove_line(self, label: str) -> None:
        """
        Deletes line connect to box widget.

        Parameters
        ----------
        label : str
            The label of the chosen plot.
            
        """
        # Find box and remove box
        side = None
        for option in self.option_boxes:
            if option.option_box.currentText() == label:
                side = option.axis_side.currentText()
                self.option_boxes.remove(option)
                break
        assert side is not None
        
        # Remove line from viewbox
        line = self.lines[label]
        self.lines.pop(label)
        self.__dict__.get("_trace_styles", {}).pop(label, None)
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
    def updateViews(self, ev: object | None) -> None:
        """
        When moving main viewbox move/scale right viewbox but the same
        relative amount.

        Parameters
        ----------
        ev : PyQt6.<something?>
            
        """
        self.right_vb.setGeometry(self.vb.sceneBoundingRect())
        if ev is not None:
            if ev.__class__.__name__ == "QGraphicsSceneWheelEvent":
                self.right_vb.wheelEvent(ev)
            elif ev.__class__.__name__ == "MouseDragEvent":
                self.right_vb.mouseDragEvent(ev)

        # Prevents lines from moving outside the axes.
        self.right_vb.setGeometry(self.vb.sceneBoundingRect())

    def open_trace_appearance_dialog(self) -> None:
        if self._trace_appearance_dialog is None:
            self._trace_appearance_dialog = _TraceAppearanceDialog(self)
        self._trace_appearance_dialog.refresh_rows()
        self._trace_appearance_dialog.show()
        self._trace_appearance_dialog.raise_()
        self._trace_appearance_dialog.activateWindow()

    def _trace_measurement_name(self, label: str, line: Any) -> str:
        if label == self.label:
            return self.param.name
        from_win = getattr(line, "from_win", None)
        param = getattr(from_win, "param", None)
        return getattr(param, "name", str(label))

    def _apply_trace_style(self, label: str, line: Any) -> None:
        if line is None:
            return
        trace_styles = self.__dict__.setdefault("_trace_styles", {})
        style = trace_styles.setdefault(label, self._TraceStyle(order=len(trace_styles)))
        pen_style_map = {
            "Solid": QtCore.Qt.PenStyle.SolidLine,
            "Dash": QtCore.Qt.PenStyle.DashLine,
            "Dot": QtCore.Qt.PenStyle.DotLine,
            "Dash Dot": QtCore.Qt.PenStyle.DashDotLine,
        }
        if style.line_enabled:
            pen = pg.mkPen(
                color=QtGui.QColor(style.line_color),
                width=style.line_width,
                style=pen_style_map.get(style.line_style, QtCore.Qt.PenStyle.SolidLine),
            )
        else:
            pen = None
        symbol_color = style.markers_color if style.markers_enabled else style.dots_color
        method_calls = [
            ("setPen", pen),
            ("setSymbolPen", pg.mkPen(symbol_color)),
            ("setSymbolBrush", pg.mkBrush(symbol_color)),
            ("setSymbolSize", style.markers_size if style.markers_enabled else style.dots_size),
            ("setSymbol", style.markers_symbol if style.markers_enabled else ("o" if style.dots_enabled else None)),
            ("setVisible", style.visible),
        ]
        for method_name, value in method_calls:
            method = getattr(line, method_name, None)
            if callable(method):
                method(value)

        target_side = "right" if style.y_axis == "Right" else "left"
        current_side = getattr(line, "side", "left")
        if hasattr(line, "set_side") and current_side != target_side:
            line.set_side(target_side)

        z = style.order
        set_z = getattr(line, "setZValue", None)
        if callable(set_z):
            set_z(z)


class _TraceAppearanceDialog(qtw.QDialog):
    _COLOR_CHOICES = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#000000"]

    def __init__(self, owner: Plot1DTraceMixin):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Trace Appearance")
        self.resize(980, 460)
        self._building = False
        main = qtw.QHBoxLayout(self)
        main.setContentsMargins(14, 14, 14, 14)
        main.setSpacing(16)
        self.table = qtw.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["ID", "Preview", "Measurement", "Axis", "Order"])
        self.table.setAlternatingRowColors(True)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(qtw.QAbstractItemView.DragDropMode.DragOnly)
        self.table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumWidth(520)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, qtw.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, qtw.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, qtw.QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(0, 54)
        self.table.setColumnWidth(1, 98)
        self.table.itemSelectionChanged.connect(self._sync_controls_from_selection)
        main.addWidget(self.table, 5)
        panel = qtw.QWidget()
        panel.setMinimumWidth(360)
        form = qtw.QFormLayout(panel)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(9)
        self.line_enable = qtw.QCheckBox("Line")
        self.line_color = self._new_color_combo()
        self.line_width = qtw.QDoubleSpinBox(); self.line_width.setRange(0.5, 15); self.line_width.setValue(2)
        self.line_style = qtw.QComboBox(); self.line_style.addItems(["Solid", "Dash", "Dot", "Dash Dot"])
        self.dots_enable = qtw.QCheckBox("Dots")
        self.dots_color = self._new_color_combo()
        self.dots_size = qtw.QDoubleSpinBox(); self.dots_size.setRange(1, 20)
        self.marker_enable = qtw.QCheckBox("Markers")
        self.marker_color = self._new_color_combo()
        self.marker_symbol = qtw.QComboBox(); self.marker_symbol.addItems(["o", "s", "t", "d", "+", "x"])
        self.marker_size = qtw.QDoubleSpinBox(); self.marker_size.setRange(1, 30); self.marker_size.setValue(10)
        self.x_axis = qtw.QComboBox(); self.x_axis.addItems(["Bottom"])
        self.y_axis = qtw.QComboBox(); self.y_axis.addItems(["Left", "Right"])
        self.visible = qtw.QCheckBox("Visible")
        self.visible.setChecked(True)
        self.order = qtw.QSpinBox(); self.order.setRange(-999, 999)
        form.addRow(self.line_enable); form.addRow("Line color", self.line_color); form.addRow("Line thickness", self.line_width); form.addRow("Line style", self.line_style)
        form.addRow(self.dots_enable); form.addRow("Dots color", self.dots_color); form.addRow("Dots size", self.dots_size)
        form.addRow(self.marker_enable); form.addRow("Marker color", self.marker_color); form.addRow("Marker symbol", self.marker_symbol); form.addRow("Marker size", self.marker_size)
        form.addRow("Horizontal axis", self.x_axis); form.addRow("Vertical axis", self.y_axis); form.addRow(self.visible); form.addRow("Plot order", self.order)
        main.addWidget(panel, 4)
        for _widget, signal in [
            (self.line_enable, self.line_enable.toggled), (self.line_color, self.line_color.currentTextChanged),
            (self.line_width, self.line_width.valueChanged), (self.line_style, self.line_style.currentTextChanged),
            (self.dots_enable, self.dots_enable.toggled), (self.dots_color, self.dots_color.currentTextChanged),
            (self.dots_size, self.dots_size.valueChanged), (self.marker_enable, self.marker_enable.toggled),
            (self.marker_color, self.marker_color.currentTextChanged), (self.marker_symbol, self.marker_symbol.currentTextChanged),
            (self.marker_size, self.marker_size.valueChanged), (self.x_axis, self.x_axis.currentTextChanged),
            (self.y_axis, self.y_axis.currentTextChanged), (self.visible, self.visible.toggled), (self.order, self.order.valueChanged),
        ]:
            signal.connect(self._apply_selection)

    def _new_color_combo(self) -> qtw.QComboBox:
        combo = qtw.QComboBox()
        for color in self._COLOR_CHOICES:
            combo.addItem(QtGui.QIcon(self._color_swatch(color)), color)
        return combo

    def _color_swatch(self, color: str) -> QtGui.QPixmap:
        pixmap = QtGui.QPixmap(18, 18)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(95, 95, 95), 1))
        painter.setBrush(QtGui.QColor(color))
        painter.drawRoundedRect(QtCore.QRectF(2, 2, 14, 14), 2, 2)
        painter.end()
        return pixmap

    def refresh_rows(self):
        selected = set(self._selected_labels())
        self._building = True
        self.table.setRowCount(0)
        for row, (label, line) in enumerate(self.owner.lines.items()):
            self.table.insertRow(row)
            trace_id = label.split()[0].replace("ID:", "") if label.startswith("ID:") else str(row + 1)
            preview = "✕"
            measurement = self.owner._trace_measurement_name(label, line)
            style = self.owner._trace_styles.setdefault(label, self.owner._TraceStyle(order=row))
            axis_text = style.y_axis
            for col, value in enumerate([trace_id, preview, measurement, axis_text, str(style.order)]):
                item = qtw.QTableWidgetItem(value)
                item.setFlags(
                    QtCore.Qt.ItemFlag.ItemIsSelectable
                    | QtCore.Qt.ItemFlag.ItemIsEnabled
                    )
                if col in (0, 3, 4):
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)
            preview_item = self.table.item(row, 1)
            preview_item.setText("")
            preview_item.setIcon(QtGui.QIcon(self._trace_preview_pixmap(style)))
            preview_item.setSizeHint(QtCore.QSize(92, 34))
            preview_item.setFlags(preview_item.flags() | QtCore.Qt.ItemFlag.ItemIsDragEnabled)
            self.table.item(row, 0).setData(QtCore.Qt.ItemDataRole.UserRole, label)
            self.table.setRowHeight(row, 44)
            if label in selected:
                self.table.selectionModel().select(
                    self.table.model().index(row, 0),
                    QtCore.QItemSelectionModel.SelectionFlag.Select
                    | QtCore.QItemSelectionModel.SelectionFlag.Rows,
                    )
        self._building = False
        if self.table.rowCount() and not self._selected_labels():
            self.table.selectRow(0)

    def _trace_preview_pixmap(self, style: Plot1DTraceMixin._TraceStyle) -> QtGui.QPixmap:
        pixmap = QtGui.QPixmap(88, 30)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        points = [
            QtCore.QPointF(6, 21),
            QtCore.QPointF(24, 9),
            QtCore.QPointF(42, 15),
            QtCore.QPointF(62, 7),
            QtCore.QPointF(82, 18),
        ]
        if style.line_enabled:
            pen = QtGui.QPen(QtGui.QColor(style.line_color), max(1.0, style.line_width))
            style_map = {
                "Dash": QtCore.Qt.PenStyle.DashLine,
                "Dot": QtCore.Qt.PenStyle.DotLine,
                "Dash Dot": QtCore.Qt.PenStyle.DashDotLine,
            }
            pen.setStyle(style_map.get(style.line_style, QtCore.Qt.PenStyle.SolidLine))
            painter.setPen(pen)
            for start, end in zip(points[:-1], points[1:], strict=True):
                painter.drawLine(start, end)
        if style.dots_enabled or style.markers_enabled:
            color = QtGui.QColor(style.markers_color if style.markers_enabled else style.dots_color)
            size = min(max(style.markers_size if style.markers_enabled else style.dots_size, 4.0), 11.0)
            painter.setPen(QtGui.QPen(color, 1.4))
            painter.setBrush(QtGui.QBrush(color))
            symbol = style.markers_symbol if style.markers_enabled else "o"
            for point in points:
                self._draw_preview_marker(painter, point, size, symbol)
        painter.end()
        return pixmap

    def _draw_preview_marker(
            self,
            painter: QtGui.QPainter,
            center: QtCore.QPointF,
            size: float,
            symbol: str,
            ) -> None:
        half = size / 2
        rect = QtCore.QRectF(center.x() - half, center.y() - half, size, size)
        if symbol == "s":
            painter.drawRect(rect)
        elif symbol == "t":
            painter.drawPolygon(QtGui.QPolygonF([
                QtCore.QPointF(center.x(), center.y() - half),
                QtCore.QPointF(center.x() - half, center.y() + half),
                QtCore.QPointF(center.x() + half, center.y() + half),
            ]))
        elif symbol == "d":
            painter.drawPolygon(QtGui.QPolygonF([
                QtCore.QPointF(center.x(), center.y() - half),
                QtCore.QPointF(center.x() - half, center.y()),
                QtCore.QPointF(center.x(), center.y() + half),
                QtCore.QPointF(center.x() + half, center.y()),
            ]))
        elif symbol in {"+", "x"}:
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            if symbol == "+":
                painter.drawLine(QtCore.QPointF(center.x() - half, center.y()), QtCore.QPointF(center.x() + half, center.y()))
                painter.drawLine(QtCore.QPointF(center.x(), center.y() - half), QtCore.QPointF(center.x(), center.y() + half))
            else:
                painter.drawLine(QtCore.QPointF(center.x() - half, center.y() - half), QtCore.QPointF(center.x() + half, center.y() + half))
                painter.drawLine(QtCore.QPointF(center.x() - half, center.y() + half), QtCore.QPointF(center.x() + half, center.y() - half))
        else:
            painter.drawEllipse(rect)

    def _selected_labels(self) -> list[str]:
        labels = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            labels.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return labels

    def _sync_controls_from_selection(self):
        labels = self._selected_labels()
        if not labels:
            return
        style = self.owner._trace_styles[labels[0]]
        self._building = True
        self.line_enable.setChecked(style.line_enabled); self.line_color.setCurrentText(style.line_color); self.line_width.setValue(style.line_width); self.line_style.setCurrentText(style.line_style)
        self.dots_enable.setChecked(style.dots_enabled); self.dots_color.setCurrentText(style.dots_color); self.dots_size.setValue(style.dots_size)
        self.marker_enable.setChecked(style.markers_enabled); self.marker_color.setCurrentText(style.markers_color); self.marker_symbol.setCurrentText(style.markers_symbol); self.marker_size.setValue(style.markers_size)
        self.x_axis.setCurrentText(style.x_axis); self.y_axis.setCurrentText(style.y_axis); self.visible.setChecked(style.visible); self.order.setValue(style.order)
        self._building = False

    def _apply_selection(self, *_args):
        if self._building:
            return
        for label in self._selected_labels():
            style = self.owner._trace_styles.setdefault(label, self.owner._TraceStyle())
            style.line_enabled = self.line_enable.isChecked(); style.line_color = self.line_color.currentText(); style.line_width = self.line_width.value(); style.line_style = self.line_style.currentText()
            style.dots_enabled = self.dots_enable.isChecked(); style.dots_color = self.dots_color.currentText(); style.dots_size = self.dots_size.value()
            style.markers_enabled = self.marker_enable.isChecked(); style.markers_color = self.marker_color.currentText(); style.markers_symbol = self.marker_symbol.currentText(); style.markers_size = self.marker_size.value()
            style.x_axis = self.x_axis.currentText(); style.y_axis = self.y_axis.currentText(); style.visible = self.visible.isChecked(); style.order = self.order.value()
            line = self.owner.lines.get(label)
            if line is not None:
                self.owner._apply_trace_style(label, line)
        self.refresh_rows()
