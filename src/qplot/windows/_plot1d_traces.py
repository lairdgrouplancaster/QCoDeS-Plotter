from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._dragdrop import make_run_preview_mime
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
        param: Any
        plot: Any
        remove_dataset: Any
        right_vb: Any
        scrollWidget: qtw.QWidget
        spinBox: Any
        vb: Any

        def initAxes(self) -> None: ...

        def initMenu(self) -> None: ...

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

    _trace_styles: dict[str, _TraceStyle]
    _trace_appearance_dialog: "_TraceAppearanceDialog | None"

    def _ensure_trace_styles(self) -> dict[str, _TraceStyle]:
        styles = self.__dict__.get("_trace_styles")
        if not isinstance(styles, dict):
            styles = {}
            self.__dict__["_trace_styles"] = styles
        return cast(dict[str, Plot1DTraceMixin._TraceStyle], styles)

    def _register_main_line(self) -> None:
        """
        Keeps the main pyqtgraph line in the trace registry.

        """
        line = self.__dict__.get("line")
        if "lines" in self.__dict__ and line is not None:
            self.lines[self.label] = line
        self._ensure_trace_styles().setdefault(self.label, self._TraceStyle())

    def initMenu(self) -> None:
        super().initMenu()
        view_menu = None
        menu_bar = self.menuBar()
        if menu_bar is None:
            return
        for action in menu_bar.actions():
            if action.text().replace("&", "") == "View":
                view_menu = action.menu()
                break
        if view_menu is None:
            return
        view_menu.addSeparator()
        trace_action = QtGui.QAction("Trace Appearance…", self)
        trace_action.triggered.connect(self.open_trace_appearance_dialog)
        view_menu.addAction(trace_action)

    def _set_main_line_color(self, color: Any) -> None:
        style = self._ensure_trace_styles().setdefault(self.label, self._TraceStyle())
        style.line_color = color.name() if hasattr(color, "name") else str(color)
        line = self.__dict__.get("line")
        if line is not None:
            self._apply_trace_style(self.label, line)


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
        styles = self._ensure_trace_styles()
        style = styles.setdefault(label, self._TraceStyle(order=len(styles)))
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
        self._trace_styles.pop(label, None)
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
        styles = self._ensure_trace_styles()
        style = styles.setdefault(label, self._TraceStyle(order=len(styles)))
        if line is None:
            return
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
        line.setPen(pen)
        line.setSymbolPen(pg.mkPen(style.markers_color if style.markers_enabled else style.dots_color))
        line.setSymbolBrush(
            pg.mkBrush(style.markers_color if style.markers_enabled else style.dots_color)
        )
        line.setSymbolSize(style.markers_size if style.markers_enabled else style.dots_size)
        line.setSymbol(style.markers_symbol if style.markers_enabled else ("o" if style.dots_enabled else None))
        line.setVisible(style.visible)

        target_side = "right" if style.y_axis == "Right" else "left"
        current_side = getattr(line, "side", "left")
        if hasattr(line, "set_side") and current_side != target_side:
            line.set_side(target_side)

        z = style.order
        set_z = getattr(line, "setZValue", None)
        if callable(set_z):
            set_z(z)


class _TracePreviewDelegate(qtw.QStyledItemDelegate):
    """
    Paint trace preview icons centered in their table cell.

    """

    def paint(self, painter, option, index):
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        if not isinstance(icon, QtGui.QIcon) or icon.isNull():
            super().paint(painter, option, index)
            return

        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QtGui.QIcon()

        widget = opt.widget
        style = widget.style() if widget else qtw.QApplication.style()
        if style is None:
            super().paint(painter, option, index)
            return
        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        icon_size = icon.actualSize(opt.rect.size())
        icon_rect = QtCore.QRect(QtCore.QPoint(), icon_size)
        icon_rect.moveCenter(opt.rect.center())
        icon.paint(painter, icon_rect, QtCore.Qt.AlignmentFlag.AlignCenter)


class _TraceTableWidget(qtw.QTableWidget):
    """
    Trace table with run-preview drag payloads.

    """

    def __init__(self, dialog: "_TraceAppearanceDialog"):
        super().__init__(0, 5, dialog)
        self.dialog = dialog

    def startDrag(self, supported_actions):
        index = self.currentIndex()
        if not index.isValid():
            return

        label = self.dialog._label_for_row(index.row())
        mime_data = self.dialog._trace_mime_data(label)
        if mime_data is None:
            super().startDrag(supported_actions)
            return

        style = self.dialog.owner._trace_styles.get(label)
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime_data)
        if style is not None:
            pixmap = self.dialog._trace_pixmap(style, QtCore.QSize(72, 26))
            drag.setPixmap(pixmap)
            drag.setHotSpot(QtCore.QPoint(pixmap.width() // 2, pixmap.height() // 2))
        drag.exec(QtCore.Qt.DropAction.CopyAction)


class _TraceAppearanceDialog(qtw.QDialog):
    _COL_ID = 0
    _COL_PREVIEW = 1
    _COL_MEASUREMENT = 2
    _COL_VISIBLE = 3
    _COL_AXIS = 4

    def __init__(self, owner: Plot1DTraceMixin):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Trace Appearance")
        self.resize(840, 360)
        self.setMinimumSize(760, 300)
        self._building = False

        main = qtw.QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        body = qtw.QHBoxLayout()
        body.setSpacing(10)
        main.addLayout(body, 1)

        trace_group = qtw.QGroupBox("Traces", self)
        trace_layout = qtw.QVBoxLayout(trace_group)
        trace_layout.setContentsMargins(8, 8, 8, 8)
        trace_layout.setSpacing(6)

        self.table = _TraceTableWidget(self)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Preview", "Measurement", "Show", "Axis"]
            )
        self.table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.table.setIconSize(QtCore.QSize(64, 22))
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(qtw.QAbstractItemView.DragDropMode.DragOnly)
        self.table.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.table.setItemDelegateForColumn(
            self._COL_PREVIEW,
            _TracePreviewDelegate(self.table),
            )
        horizontal_header = self.table.horizontalHeader()
        if horizontal_header is not None:
            horizontal_header.setFixedHeight(24)
            horizontal_header.setSectionResizeMode(
                self._COL_ID,
                qtw.QHeaderView.ResizeMode.ResizeToContents,
                )
            horizontal_header.setSectionResizeMode(
                self._COL_PREVIEW,
                qtw.QHeaderView.ResizeMode.ResizeToContents,
                )
            horizontal_header.setSectionResizeMode(
                self._COL_MEASUREMENT,
                qtw.QHeaderView.ResizeMode.Stretch,
                )
            for column in (self._COL_VISIBLE, self._COL_AXIS):
                horizontal_header.setSectionResizeMode(
                    column,
                    qtw.QHeaderView.ResizeMode.ResizeToContents,
                    )
        vertical_header = self.table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
            vertical_header.setDefaultSectionSize(32)
            vertical_header.setMinimumSectionSize(26)
        self.table.itemSelectionChanged.connect(self._sync_controls_from_selection)

        self.table.itemChanged.connect(self._table_item_changed)

        table_tools = qtw.QHBoxLayout()
        table_tools.setContentsMargins(0, 0, 0, 0)
        table_tools.setSpacing(4)
        table_tools.addStretch()
        self.move_up_button = qtw.QToolButton(trace_group)
        self.move_down_button = qtw.QToolButton(trace_group)
        for button, pixmap, tooltip in (
                (
                    self.move_up_button,
                    qtw.QStyle.StandardPixmap.SP_ArrowUp,
                    "Move selected traces up",
                    ),
                (
                    self.move_down_button,
                    qtw.QStyle.StandardPixmap.SP_ArrowDown,
                    "Move selected traces down",
                    ),
                ):
            button.setAutoRaise(True)
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            style = self.style()
            if style is not None:
                button.setIcon(style.standardIcon(pixmap))
            table_tools.addWidget(button)
        self.move_up_button.clicked.connect(lambda: self._move_selected_rows(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_rows(1))

        trace_layout.addLayout(table_tools)
        trace_layout.addWidget(self.table)
        body.addWidget(trace_group, 5)

        panel = qtw.QWidget(self)
        panel_layout = qtw.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.selection_summary = qtw.QLabel("No trace selected", panel)
        self.selection_summary.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.NoTextInteraction
            )
        panel_layout.addWidget(self.selection_summary)

        colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#000000"]

        self.line_enable = qtw.QCheckBox("Line")
        self.line_color = qtw.QComboBox(); self._add_color_items(self.line_color, colors)
        self.line_width = qtw.QDoubleSpinBox(); self.line_width.setRange(0.5, 15); self.line_width.setValue(2); self.line_width.setDecimals(1); self.line_width.setSingleStep(0.5)
        self.line_style = qtw.QComboBox(); self.line_style.addItems(["Solid", "Dash", "Dot", "Dash Dot"])
        self.dots_enable = qtw.QCheckBox("Dots")
        self.dots_color = qtw.QComboBox(); self._add_color_items(self.dots_color, colors)
        self.dots_size = qtw.QDoubleSpinBox(); self.dots_size.setRange(1, 20); self.dots_size.setDecimals(1); self.dots_size.setSingleStep(1)
        self.marker_enable = qtw.QCheckBox("Markers")
        self.marker_color = qtw.QComboBox(); self._add_color_items(self.marker_color, colors)
        self.marker_symbol = qtw.QComboBox(); self.marker_symbol.addItems(["o", "s", "t", "d", "+", "x"])
        self.marker_size = qtw.QDoubleSpinBox(); self.marker_size.setRange(1, 30); self.marker_size.setValue(10); self.marker_size.setDecimals(1); self.marker_size.setSingleStep(1)
        self.x_axis = qtw.QComboBox(); self.x_axis.addItems(["Bottom"])
        self.y_axis = qtw.QComboBox(); self.y_axis.addItems(["Left", "Right"])
        self.visible = qtw.QCheckBox("Visible")
        self.visible.setChecked(True)

        for combo in (
            self.line_color,
            self.dots_color,
            self.marker_color,
            self.line_style,
            self.marker_symbol,
            self.x_axis,
            self.y_axis,
            ):
            combo.setMinimumContentsLength(4)
        for combo in (self.line_color, self.dots_color, self.marker_color):
            combo.setIconSize(QtCore.QSize(34, 14))
            combo.setFixedWidth(94)
        for spin in (self.line_width, self.dots_size, self.marker_size):
            spin.setFixedWidth(64)
        self.line_style.setFixedWidth(94)
        self.marker_symbol.setFixedWidth(62)
        self.x_axis.setFixedWidth(92)
        self.y_axis.setFixedWidth(82)

        display_group = qtw.QGroupBox("Display", panel)
        display_layout = qtw.QGridLayout(display_group)
        display_layout.setContentsMargins(8, 10, 8, 8)
        display_layout.setHorizontalSpacing(6)
        display_layout.setVerticalSpacing(4)
        self._add_display_row(
            display_layout,
            0,
            self.line_enable,
            self.line_color,
            [("Width", self.line_width), ("Style", self.line_style)],
            )
        self._add_display_row(
            display_layout,
            1,
            self.dots_enable,
            self.dots_color,
            [("Size", self.dots_size)],
            )
        self._add_display_row(
            display_layout,
            2,
            self.marker_enable,
            self.marker_color,
            [("Symbol", self.marker_symbol), ("Size", self.marker_size)],
            )
        display_layout.setColumnStretch(7, 1)
        panel_layout.addWidget(display_group)

        trace_settings = qtw.QGroupBox("Axes", panel)
        trace_grid = qtw.QGridLayout(trace_settings)
        trace_grid.setContentsMargins(8, 10, 8, 8)
        trace_grid.setHorizontalSpacing(8)
        trace_grid.setVerticalSpacing(4)
        trace_grid.addWidget(self.visible, 0, 0)
        trace_grid.addWidget(qtw.QLabel("Horizontal"), 0, 1)
        trace_grid.addWidget(self.x_axis, 0, 2)
        trace_grid.addWidget(qtw.QLabel("Vertical"), 1, 1)
        trace_grid.addWidget(self.y_axis, 1, 2)
        trace_grid.setColumnStretch(3, 1)
        panel_layout.addWidget(trace_settings)
        panel_layout.addStretch()

        body.addWidget(panel, 4)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        main.addWidget(buttons)

        self._line_controls = [self.line_color, self.line_width, self.line_style]
        self._dots_controls = [self.dots_color, self.dots_size]
        self._marker_controls = [self.marker_color, self.marker_symbol, self.marker_size]
        self._selection_controls = [
            self.line_enable,
            self.dots_enable,
            self.marker_enable,
            self.x_axis,
            self.y_axis,
            self.visible,
            *self._line_controls,
            *self._dots_controls,
            *self._marker_controls,
            ]
        for _widget, signal in [
            (self.line_enable, self.line_enable.toggled), (self.line_color, self.line_color.currentTextChanged),
            (self.line_width, self.line_width.valueChanged), (self.line_style, self.line_style.currentTextChanged),
            (self.dots_enable, self.dots_enable.toggled), (self.dots_color, self.dots_color.currentTextChanged),
            (self.dots_size, self.dots_size.valueChanged), (self.marker_enable, self.marker_enable.toggled),
            (self.marker_color, self.marker_color.currentTextChanged), (self.marker_symbol, self.marker_symbol.currentTextChanged),
            (self.marker_size, self.marker_size.valueChanged), (self.x_axis, self.x_axis.currentTextChanged),
            (self.y_axis, self.y_axis.currentTextChanged), (self.visible, self.visible.toggled),
        ]:
            signal.connect(self._apply_selection)
        self._update_control_enabled_states(False)

    def _add_display_row(
            self,
            layout: qtw.QGridLayout,
            row: int,
            toggle: qtw.QCheckBox,
            color_combo: qtw.QComboBox,
            controls: list[tuple[str, qtw.QWidget]],
            ) -> None:
        layout.addWidget(toggle, row, 0)
        layout.addWidget(qtw.QLabel("Color"), row, 1)
        layout.addWidget(color_combo, row, 2)
        column = 3
        for label, widget in controls:
            layout.addWidget(qtw.QLabel(label), row, column)
            layout.addWidget(widget, row, column + 1)
            column += 2

    def _add_color_items(self, combo: qtw.QComboBox, colors: list[str]) -> None:
        for color in colors:
            combo.addItem(self._color_icon(color), color)

    def _color_icon(self, color: str) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(28, 14)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setPen(QtGui.QPen(QtGui.QColor("#444444")))
        painter.setBrush(QtGui.QBrush(QtGui.QColor(color)))
        painter.drawRoundedRect(1, 1, 26, 12, 2, 2)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _trace_pixmap(
            self,
            style: Plot1DTraceMixin._TraceStyle,
            size: QtCore.QSize | None = None,
            ) -> QtGui.QPixmap:
        if size is None:
            size = QtCore.QSize(64, 22)
        pixmap = QtGui.QPixmap(size)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if style.visible else 0.35)

        points = [
            QtCore.QPointF(5, size.height() - 7),
            QtCore.QPointF(size.width() * 0.38, 7),
            QtCore.QPointF(size.width() * 0.68, size.height() - 8),
            QtCore.QPointF(size.width() - 5, 8),
            ]
        if style.line_enabled:
            pen = QtGui.QPen(QtGui.QColor(style.line_color))
            pen.setWidthF(max(1.0, min(style.line_width, 4.0)))
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen_style_map = {
                "Solid": QtCore.Qt.PenStyle.SolidLine,
                "Dash": QtCore.Qt.PenStyle.DashLine,
                "Dot": QtCore.Qt.PenStyle.DotLine,
                "Dash Dot": QtCore.Qt.PenStyle.DashDotLine,
            }
            pen.setStyle(pen_style_map.get(style.line_style, QtCore.Qt.PenStyle.SolidLine))
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QtGui.QPolygonF(points))

        if style.dots_enabled:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(style.dots_color))
            radius = max(2.0, min(style.dots_size / 2.0, 4.5))
            for point in points:
                painter.drawEllipse(point, radius, radius)

        if style.markers_enabled:
            pen = QtGui.QPen(QtGui.QColor(style.markers_color))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(QtGui.QColor(style.markers_color))
            radius = max(3.0, min(style.markers_size / 2.0, 5.0))
            for point in points[::2]:
                self._draw_marker_symbol(painter, point, radius, style.markers_symbol)

        if not (style.line_enabled or style.dots_enabled or style.markers_enabled):
            pen = QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.Mid))
            pen.setWidthF(1.0)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(6, size.height() // 2, size.width() - 6, size.height() // 2)

        painter.end()
        return pixmap

    def _draw_marker_symbol(
            self,
            painter: QtGui.QPainter,
            point: QtCore.QPointF,
            radius: float,
            symbol: str,
            ) -> None:
        if symbol == "s":
            rect = QtCore.QRectF(
                point.x() - radius,
                point.y() - radius,
                radius * 2,
                radius * 2,
                )
            painter.drawRect(rect)
        elif symbol == "t":
            polygon = QtGui.QPolygonF([
                QtCore.QPointF(point.x(), point.y() - radius),
                QtCore.QPointF(point.x() - radius, point.y() + radius),
                QtCore.QPointF(point.x() + radius, point.y() + radius),
                ])
            painter.drawPolygon(polygon)
        elif symbol == "d":
            polygon = QtGui.QPolygonF([
                QtCore.QPointF(point.x(), point.y() - radius),
                QtCore.QPointF(point.x() - radius, point.y()),
                QtCore.QPointF(point.x(), point.y() + radius),
                QtCore.QPointF(point.x() + radius, point.y()),
                ])
            painter.drawPolygon(polygon)
        elif symbol == "+":
            painter.drawLine(
                QtCore.QPointF(point.x() - radius, point.y()),
                QtCore.QPointF(point.x() + radius, point.y()),
                )
            painter.drawLine(
                QtCore.QPointF(point.x(), point.y() - radius),
                QtCore.QPointF(point.x(), point.y() + radius),
                )
        elif symbol == "x":
            painter.drawLine(
                QtCore.QPointF(point.x() - radius, point.y() - radius),
                QtCore.QPointF(point.x() + radius, point.y() + radius),
                )
            painter.drawLine(
                QtCore.QPointF(point.x() - radius, point.y() + radius),
                QtCore.QPointF(point.x() + radius, point.y() - radius),
                )
        else:
            painter.drawEllipse(point, radius, radius)

    def _trace_icon(self, style: Plot1DTraceMixin._TraceStyle) -> QtGui.QIcon:
        return QtGui.QIcon(self._trace_pixmap(style))

    def refresh_rows(self):
        selected = set(self._selected_labels())
        self._sync_plot_order_from_rows()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        try:
            for row, (label, line) in enumerate(self.owner.lines.items()):
                self.table.insertRow(row)
                trace_id = label.split()[0].replace("ID:", "") if label.startswith("ID:") else str(row + 1)
                measurement = self.owner._trace_measurement_name(label, line)
                style = self.owner._trace_styles.setdefault(label, self.owner._TraceStyle(order=row))
                axis_text = style.y_axis

                values = {
                    self._COL_ID: trace_id,
                    self._COL_MEASUREMENT: measurement,
                    self._COL_AXIS: axis_text,
                    }
                for col, value in values.items():
                    item = qtw.QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    item.setToolTip(value)
                    self.table.setItem(row, col, item)

                preview_item = qtw.QTableWidgetItem()
                preview_item.setIcon(self._trace_icon(style))
                preview_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                preview_item.setFlags(
                    (preview_item.flags() | QtCore.Qt.ItemFlag.ItemIsDragEnabled)
                    & ~QtCore.Qt.ItemFlag.ItemIsEditable
                    )
                preview_item.setToolTip("Drag trace preview")
                self.table.setItem(row, self._COL_PREVIEW, preview_item)

                visible_item = qtw.QTableWidgetItem()
                visible_item.setFlags(
                    (visible_item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    & ~QtCore.Qt.ItemFlag.ItemIsEditable
                    )
                visible_item.setCheckState(
                    QtCore.Qt.CheckState.Checked
                    if style.visible
                    else QtCore.Qt.CheckState.Unchecked
                    )
                visible_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                visible_item.setToolTip("Trace visibility")
                self.table.setItem(row, self._COL_VISIBLE, visible_item)

                label_item = self.table.item(row, self._COL_ID)
                if label_item is not None:
                    label_item.setData(QtCore.Qt.ItemDataRole.UserRole, label)
                    label_item.setToolTip(label)
                self.table.setRowHeight(row, 32)
                if label in selected:
                    self.table.selectRow(row)
        finally:
            self.table.blockSignals(False)

        if self.table.rowCount() and not selected:
            self.table.selectRow(0)
        else:
            self._sync_controls_from_selection()

    def _sync_plot_order_from_rows(self) -> None:
        row_count = len(self.owner.lines)
        for row, (label, line) in enumerate(self.owner.lines.items()):
            style = self.owner._trace_styles.setdefault(
                label,
                self.owner._TraceStyle(order=row_count - row - 1),
                )
            style.order = row_count - row - 1
            set_z = getattr(line, "setZValue", None)
            if callable(set_z):
                set_z(style.order)

    def _move_selected_rows(self, direction: int) -> None:
        selected = set(self._selected_labels())
        if not selected:
            return

        labels = list(self.owner.lines)
        original = list(labels)
        if direction < 0:
            for index, label in enumerate(labels):
                if index and label in selected and labels[index - 1] not in selected:
                    labels[index - 1], labels[index] = labels[index], labels[index - 1]
        elif direction > 0:
            for index in range(len(labels) - 1, -1, -1):
                label = labels[index]
                if (
                        index < len(labels) - 1
                        and label in selected
                        and labels[index + 1] not in selected
                        ):
                    labels[index + 1], labels[index] = labels[index], labels[index + 1]

        if labels == original:
            self._update_move_button_states()
            return

        old_lines = self.owner.lines
        reordered = [(label, old_lines[label]) for label in labels]
        old_lines.clear()
        old_lines.update(reordered)
        self._sync_plot_order_from_rows()
        self.refresh_rows()

    def _selected_rows(self) -> list[int]:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return []
        return sorted({idx.row() for idx in selection_model.selectedRows()})

    def _selected_labels(self) -> list[str]:
        labels: list[str] = []
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return labels
        for idx in selection_model.selectedRows():
            item = self.table.item(idx.row(), self._COL_ID)
            if item is not None:
                labels.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return labels

    def _label_for_row(self, row: int) -> str:
        item = self.table.item(row, self._COL_ID)
        if item is None:
            return ""
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")

    def _trace_mime_data(self, label: str) -> QtCore.QMimeData | None:
        line = self.owner.lines.get(label)
        source = self.owner if label == self.owner.label else getattr(line, "from_win", None)
        if source is None:
            return None

        guid = getattr(source, "_guid", "")
        param = getattr(source, "param", None)
        parameter = getattr(param, "name", "")
        if not guid or not parameter:
            return None
        return make_run_preview_mime(
            guid,
            parameter,
            getattr(param, "depends_on_", ()),
            )

    def _table_item_changed(self, item: qtw.QTableWidgetItem) -> None:
        if self._building or item.column() != self._COL_VISIBLE:
            return

        label = self._label_for_row(item.row())
        if not label:
            return

        style = self.owner._trace_styles.setdefault(label, self.owner._TraceStyle())
        style.visible = item.checkState() == QtCore.Qt.CheckState.Checked
        line = self.owner.lines.get(label)
        if line is not None:
            self.owner._apply_trace_style(label, line)

        preview_item = self.table.item(item.row(), self._COL_PREVIEW)
        if preview_item is not None:
            preview_item.setIcon(self._trace_icon(style))
        if label in self._selected_labels():
            self._sync_controls_from_selection()

    def _sync_controls_from_selection(self):
        if self._building:
            return
        labels = self._selected_labels()
        if not labels:
            self.selection_summary.setText("No trace selected")
            self._update_control_enabled_states(False)
            return
        style = self.owner._trace_styles[labels[0]]
        self._building = True
        try:
            self.line_enable.setChecked(style.line_enabled); self.line_color.setCurrentText(style.line_color); self.line_width.setValue(style.line_width); self.line_style.setCurrentText(style.line_style)
            self.dots_enable.setChecked(style.dots_enabled); self.dots_color.setCurrentText(style.dots_color); self.dots_size.setValue(style.dots_size)
            self.marker_enable.setChecked(style.markers_enabled); self.marker_color.setCurrentText(style.markers_color); self.marker_symbol.setCurrentText(style.markers_symbol); self.marker_size.setValue(style.markers_size)
            self.x_axis.setCurrentText(style.x_axis); self.y_axis.setCurrentText(style.y_axis); self.visible.setChecked(style.visible)
        finally:
            self._building = False

        if len(labels) == 1:
            measurement = self.owner._trace_measurement_name(labels[0], self.owner.lines[labels[0]])
            self.selection_summary.setText(f"Editing {measurement}")
        else:
            self.selection_summary.setText(f"Editing {len(labels)} traces")
        self._update_control_enabled_states(True)

    def _update_control_enabled_states(self, has_selection: bool) -> None:
        for widget in self._selection_controls:
            widget.setEnabled(has_selection)
        for widget in self._line_controls:
            widget.setEnabled(has_selection and self.line_enable.isChecked())
        for widget in self._dots_controls:
            widget.setEnabled(has_selection and self.dots_enable.isChecked())
        for widget in self._marker_controls:
            widget.setEnabled(has_selection and self.marker_enable.isChecked())
        self._update_move_button_states()

    def _update_move_button_states(self) -> None:
        rows = self._selected_rows()
        self.move_up_button.setEnabled(bool(rows) and rows[0] > 0)
        self.move_down_button.setEnabled(
            bool(rows) and rows[-1] < self.table.rowCount() - 1
            )

    def _apply_selection(self, *_args):
        if self._building:
            return
        self._update_control_enabled_states(bool(self._selected_labels()))
        for label in self._selected_labels():
            style = self.owner._trace_styles.setdefault(label, self.owner._TraceStyle())
            style.line_enabled = self.line_enable.isChecked(); style.line_color = self.line_color.currentText(); style.line_width = self.line_width.value(); style.line_style = self.line_style.currentText()
            style.dots_enabled = self.dots_enable.isChecked(); style.dots_color = self.dots_color.currentText(); style.dots_size = self.dots_size.value()
            style.markers_enabled = self.marker_enable.isChecked(); style.markers_color = self.marker_color.currentText(); style.markers_symbol = self.marker_symbol.currentText(); style.markers_size = self.marker_size.value()
            style.x_axis = self.x_axis.currentText(); style.y_axis = self.y_axis.currentText(); style.visible = self.visible.isChecked()
            line = self.owner.lines.get(label)
            if line is not None:
                self.owner._apply_trace_style(label, line)
        self.refresh_rows()
