from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._dragdrop import make_run_preview_mime
from ._subplots import subplot1d
from ._widgets import picker_1d

TRACE_COLOR_PALETTE = (
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
    "#000000",
    "#ffffff",
    )

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
        lines: dict[Any, Any]
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

        def update_theme(self, config: Any) -> None: ...

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

    _trace_styles: dict[Any, _TraceStyle]
    _trace_controls: dict[Any, Any]
    _trace_appearance_dialog: "_TraceAppearanceDialog | None"

    def _ensure_trace_styles(self) -> dict[Any, _TraceStyle]:
        styles = self.__dict__.get("_trace_styles")
        if not isinstance(styles, dict):
            styles = {}
            self.__dict__["_trace_styles"] = styles
        return cast(dict[Any, Plot1DTraceMixin._TraceStyle], styles)

    def _ensure_trace_controls(self) -> dict[Any, Any]:
        controls = self.__dict__.get("_trace_controls")
        if not isinstance(controls, dict):
            controls = {}
            self.__dict__["_trace_controls"] = controls
        return cast(dict[Any, Any], controls)

    def _initial_trace_style(self, order: int = 0) -> _TraceStyle:
        style = self._TraceStyle(order=order)
        style.line_color = TRACE_COLOR_PALETTE[0]
        style.dots_color = style.line_color
        style.markers_color = style.line_color
        return style

    @staticmethod
    def _window_trace_key(window: Any) -> Any:
        """Return a database-aware identity, with labels as a legacy fallback."""

        return getattr(window, "_trace_key", getattr(window, "label", None))

    @staticmethod
    def _picker_matches_trace(box: Any, trace_key: Any, label: str) -> bool:
        """Return whether a picker row represents the newly added trace."""

        box_key = box.option_box.currentData()
        return box_key == trace_key or (
            box_key in (None, label)
            and label == box.option_box.currentText()
            )

    def _stored_trace_key(self, key: Any, line: Any) -> Any:
        """Return the source identity represented by one stored plot line."""

        if line is self.__dict__.get("line"):
            return getattr(self, "_trace_key", key)
        from_win = getattr(line, "from_win", None)
        return self._window_trace_key(from_win) if from_win is not None else key

    def _has_trace_window(self, window: Any) -> bool:
        """Return whether ``window`` is already represented on this plot."""

        trace_key = self._window_trace_key(window)
        return any(
            self._stored_trace_key(stored_key, line) == trace_key
            for stored_key, line in self.__dict__.get("lines", {}).items()
            )

    def _secondary_lines(self) -> list[Any]:
        """Return secondary traces without relying on their display order."""

        main_line = self.__dict__.get("line")
        return [
            line
            for line in self.__dict__.get("lines", {}).values()
            if line is not None and line is not main_line
            ]

    def _sync_right_axis_visibility(self) -> None:
        """Show right-axis values exactly when a secondary trace uses them."""

        plot = self.__dict__.get("plot")
        if plot is None:
            return

        main_line = self.__dict__.get("line")
        styles = self._ensure_trace_styles()
        show_values = any(
            line is not None
            and line is not main_line
            and styles.get(key, self._initial_trace_style()).y_axis == "Right"
            for key, line in self.__dict__.get("lines", {}).items()
            )
        plot.getAxis("right").setStyle(showValues=show_values)

    def _trace_display_label(self, key: Any, line: Any) -> str:
        """Return the unchanged user-facing label for an internally keyed trace."""

        if line is self.__dict__.get("line"):
            return self.label
        from_win = getattr(line, "from_win", None)
        return str(getattr(from_win, "label", key))

    def _register_main_line(self) -> None:
        """
        Keeps the main pyqtgraph line in the trace registry.

        """
        line = self.__dict__.get("line")
        if "lines" in self.__dict__ and line is not None:
            self.lines[self.label] = line
        self._ensure_trace_styles().setdefault(self.label, self._initial_trace_style())
        if line is not None and callable(getattr(line, "setPen", None)):
            self._apply_trace_style(self.label, line)

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
        self._set_trace_line_color(self.label, color)

    def _set_trace_line_color(self, label: Any, color: Any) -> None:
        style = self._ensure_trace_styles().setdefault(
            label,
            self._initial_trace_style(),
            )
        style.line_color = color.name() if hasattr(color, "name") else str(color)
        self._apply_trace_style(label, self.__dict__.get("lines", {}).get(label))

    def _set_trace_y_axis(self, label: Any, side: str) -> None:
        style = self._ensure_trace_styles().setdefault(
            label,
            self._initial_trace_style(),
            )
        if self.__dict__.get("lines", {}).get(label) is self.__dict__.get("line"):
            style.y_axis = "Left"
            self._sync_trace_control(label)
            return
        style.y_axis = "Right" if side.lower() == "right" else "Left"
        self._apply_trace_style(label, self.__dict__.get("lines", {}).get(label))

    def _sync_trace_control(self, label: Any) -> None:
        control = self._ensure_trace_controls().get(label)
        style = self._ensure_trace_styles().get(label)
        if control is None or style is None:
            return

        color_box = getattr(control, "color_box", None)
        if color_box is not None:
            color_box.setColor(QtGui.QColor(style.line_color))

        axis_side = getattr(control, "axis_side", None)
        if axis_side is not None and axis_side.currentText() != style.y_axis:
            previous_blocked = axis_side.blockSignals(True)
            try:
                axis_side.setCurrentText(style.y_axis)
            finally:
                axis_side.blockSignals(previous_blocked)


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
        self._trace_controls = {}
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
        main_line.color_box.selectedColor.connect(
            self._set_main_line_color
            )
        self._trace_controls[self.label] = main_line
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
            labels = [item.label for item in self.mergable]
            trace_keys = [self._window_trace_key(item) for item in self.mergable]
            new_option = picker_1d(
                self,
                self.config,
                labels,
                item_data=trace_keys,
                )
        
        # Connect Slots
        new_option.itemSelected.connect(
            lambda label, option=new_option: self.add_line(
                label,
                option.option_box.currentData(),
                )
            )
        new_option.closed.connect(
            lambda label, option=new_option: self.remove_line(
                label,
                getattr(option, "_trace_key", option.option_box.currentData()),
                )
            )
        
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
        if wins is not None:
            self.mergable = wins
        
        # Only add options which are not already being plotted
        if self.option_boxes:
            selected_keys = {
                getattr(box, "_trace_key", box.option_box.currentData())
                for box in self.option_boxes
                if box.option_box.currentIndex() >= 0
                }
            available = [
                item for item in self.mergable
                if self._window_trace_key(item) not in selected_keys
                ]
            for box in self.option_boxes:
                if box.option_box.isEnabled():
                    box.reset_box(
                        [item.label for item in available],
                        item_data=[self._window_trace_key(item) for item in available],
                        )


    def refresh_secondary_lines(self) -> None:
        """
        Refreshes added trace lines and restarts hidden live-trace monitors.

        """
        for line in self._secondary_lines():
            line.refresh()
            from_win = line.from_win
            if (
                not from_win.visible
                and from_win.ds.running
                and not from_win.monitor.isActive()
                ):
                from_win.monitorIntervalChanged(from_win.spinBox.value())
    
    
    @QtCore.pyqtSlot(str)
    def add_line(self, label: str, trace_key: Any = None) -> None:
        """
        Produces a secondary plot based on user selection in dropdown menus
        
        See both subplot1d and custom_viewbox in:
            qplot.tools.subplots
        for setup and other functions

        Parameters
        ----------
        label : str
            The label of the chosen plot.
        trace_key : object, optional
            Database-aware source identity supplied by the picker.

        Returns
        -------
        None.

        """
        
        win = None
        
        # Find the exact selected window. Labels remain display-only and may be
        # shared by copied runs opened from different databases.
        if trace_key is not None:
            for item in self.mergable:
                if self._window_trace_key(item) == trace_key:
                    win = item
                    self.mergable.remove(item)
                    break
        if win is None:
            for item in self.mergable:
                if item.label != label:
                    continue
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
            
        # Create and track new line
        self.make_ds.emit(win._dataset_key)
        trace_key = self._window_trace_key(win)
        try:
            subplot = subplot1d(self, win)
        except Exception:
            self.remove_dataset.emit(win._dataset_key)
            self.mergable.append(win)
            for box in self.option_boxes:
                if not self._picker_matches_trace(box, trace_key, label):
                    continue
                box.option_box.setEnabled(True)
                box.del_box.setEnabled(False)
                box.reset_box(
                    [item.label for item in self.mergable],
                    item_data=[self._window_trace_key(item) for item in self.mergable],
                    )
                break
            raise

        # Produce a new empty box for the next selection only after the line
        # has been constructed successfully.
        self.add_option_box()
        self.lines[trace_key] = subplot
        
        # Connect box options to line
        selected_box = None
        for box in self.option_boxes:
            if self._picker_matches_trace(box, trace_key, label):
                
                box.color_box.selectedColor.connect(
                    lambda color, selected_key=trace_key: self._set_trace_line_color(
                        selected_key,
                        color,
                        )
                    )
                
                box.axis_side.currentTextChanged.connect(
                    lambda side, selected_key=trace_key: self._set_trace_y_axis(
                        selected_key,
                        side,
                        )
                    )
                box._trace_key = trace_key
                selected_box = box
                break
        
        # debug line
        assert selected_box is not None
        
        styles = self._ensure_trace_styles()
        style = styles.setdefault(trace_key, self._initial_trace_style(order=len(styles)))
        style.line_color = selected_box.color_box.color().name()
        style.y_axis = "Right" if selected_box.axis_side.currentText().lower() == "right" else "Left"
        self._ensure_trace_controls()[trace_key] = selected_box
        self._apply_trace_style(trace_key, subplot)
        
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event: object) -> None:
        # Stopped lines as needed
        for line in self._secondary_lines():
            disconnect_source_updates = getattr(
                line,
                "disconnect_source_updates",
                None,
                )
            if callable(disconnect_source_updates):
                disconnect_source_updates()
            self.remove_dataset.emit(line.from_win._dataset_key)

        main_line = self.__dict__.get("line")
        self.lines = {
            key: line
            for key, line in self.__dict__.get("lines", {}).items()
            if line is main_line
            }
                
            
        super().closeEvent(event)
        
    
    @QtCore.pyqtSlot(str)
    def remove_line(self, label: str, trace_key: Any = None) -> None:
        """
        Deletes line connect to box widget.

        Parameters
        ----------
        label : str
            The label of the chosen plot.
        trace_key : object, optional
            Database-aware identity of the trace to remove.
            
        """
        # Find box and remove box
        side = None
        selected_key = trace_key
        for option in self.option_boxes:
            option_key = getattr(option, "_trace_key", option.option_box.currentData())
            if selected_key is not None and option_key != selected_key:
                continue
            if selected_key is None and option.option_box.currentText() != label:
                continue
            selected_key = option_key if option_key is not None else label
            side = option.axis_side.currentText()
            self.option_boxes.remove(option)
            break
        assert side is not None
        assert selected_key is not None
        
        # Remove line from viewbox
        line = self.lines[selected_key]
        self.lines.pop(selected_key)
        self._trace_styles.pop(selected_key, None)
        self._ensure_trace_controls().pop(selected_key, None)
        disconnect_source_updates = getattr(
            line,
            "disconnect_source_updates",
            None,
            )
        if callable(disconnect_source_updates):
            disconnect_source_updates()
        # Fetch correct viewbox to remove from
        vb = self.plot if side.lower() == "left" else self.right_vb
        vb.removeItem(line)

        self._sync_right_axis_visibility()
        
        # Remove track of window
        self.remove_dataset.emit(line.from_win._dataset_key)
        
        # Update box options
        self.get_mergables.emit()
        # Resize dock widget
        self._resize_scrollArea()
        dialog = self.__dict__.get("_trace_appearance_dialog")
        if dialog is not None:
            dialog.refresh_rows()
    
    
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

    def update_theme(self, config: Any) -> None:
        super().update_theme(config)
        for label, line in self.__dict__.get("lines", {}).items():
            self._apply_trace_style(label, line)

        dialog = self.__dict__.get("_trace_appearance_dialog")
        if dialog is not None:
            dialog.refresh_theme()

    def _trace_measurement_name(self, label: Any, line: Any) -> str:
        if line is self.__dict__.get("line"):
            return self.param.name
        from_win = getattr(line, "from_win", None)
        param = getattr(from_win, "param", None)
        return getattr(param, "name", str(label))

    def _apply_trace_style(self, label: Any, line: Any) -> None:
        styles = self._ensure_trace_styles()
        style = styles.setdefault(label, self._initial_trace_style(order=len(styles)))
        if style.dots_enabled and style.markers_enabled:
            style.dots_enabled = False
        self._sync_trace_control(label)
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

        self._sync_right_axis_visibility()

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
        super().__init__(0, 3, dialog)
        self.dialog = dialog

    def startDrag(self, supported_actions):
        index = self.currentIndex()
        if not index.isValid():
            return

        trace_key = self.dialog._trace_key_for_row(index.row())
        mime_data = self.dialog._trace_mime_data(trace_key)
        if mime_data is None:
            return

        style = self.dialog.owner._trace_styles.get(trace_key)
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
    _CUSTOM_COLOR_DATA = "__custom_color__"
    _LINE_STYLES = (
        ("Solid", QtCore.Qt.PenStyle.SolidLine),
        ("Dash", QtCore.Qt.PenStyle.DashLine),
        ("Dot", QtCore.Qt.PenStyle.DotLine),
        ("Dash Dot", QtCore.Qt.PenStyle.DashDotLine),
        )
    _MARKER_SYMBOLS = ("o", "s", "t", "d", "+", "x")

    def __init__(self, owner: Plot1DTraceMixin):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Trace Appearance")
        self.resize(780, 360)
        self.setMinimumSize(700, 300)
        self._building = False
        self.setStyleSheet(
            self.styleSheet()
            + """
            QDoubleSpinBox#traceAppearanceSpin {
                padding-right: 18px;
            }
            QDoubleSpinBox#traceAppearanceSpin::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 15px;
                height: 10px;
                border-left: 1px solid palette(mid);
                border-bottom: 1px solid palette(mid);
                border-top-right-radius: 4px;
                background: transparent;
            }
            QDoubleSpinBox#traceAppearanceSpin::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 15px;
                height: 10px;
                border-left: 1px solid palette(mid);
                border-bottom-right-radius: 4px;
                background: transparent;
            }
            QDoubleSpinBox#traceAppearanceSpin::up-arrow,
            QDoubleSpinBox#traceAppearanceSpin::down-arrow {
                width: 7px;
                height: 7px;
            }
            QTableWidget#traceAppearanceTable {
                border: 1px solid palette(mid);
                background-color: palette(base);
                alternate-background-color: palette(alternate-base);
                gridline-color: palette(midlight);
                selection-background-color: palette(alternate-base);
                selection-color: palette(text);
            }
            QTableWidget#traceAppearanceTable::item {
                padding: 2px 6px;
                border: none;
            }
            QTableWidget#traceAppearanceTable::item:selected {
                background-color: palette(alternate-base);
                color: palette(text);
            }
            QTableWidget#traceAppearanceTable::item:hover {
                background-color: palette(window);
                color: palette(text);
            }
            QTableWidget#traceAppearanceTable QHeaderView::section {
                font-weight: normal;
            }
            """
            )

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
        self.table.setObjectName("traceAppearanceTable")
        self.table.setHorizontalHeaderLabels(
            ["ID", "Preview", "Measurement"]
            )
        self.table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.table.setIconSize(QtCore.QSize(64, 22))
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(qtw.QAbstractItemView.DragDropMode.DragOnly)
        self.table.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.table.setItemDelegateForColumn(
            self._COL_PREVIEW,
            _TracePreviewDelegate(self.table),
            )
        horizontal_header = self.table.horizontalHeader()
        if horizontal_header is not None:
            header_font = horizontal_header.font()
            header_font.setBold(False)
            horizontal_header.setFont(header_font)
            for column in range(self.table.columnCount()):
                header_item = self.table.horizontalHeaderItem(column)
                if header_item is not None:
                    item_font = header_item.font()
                    item_font.setBold(False)
                    header_item.setFont(item_font)
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
        vertical_header = self.table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
            vertical_header.setDefaultSectionSize(28)
            vertical_header.setMinimumSectionSize(24)
        self.table.itemSelectionChanged.connect(self._sync_controls_from_selection)

        table_tools = qtw.QHBoxLayout()
        table_tools.setContentsMargins(0, 0, 0, 0)
        table_tools.setSpacing(4)
        table_tools.addStretch()
        self.move_up_button = qtw.QToolButton(trace_group)
        self.move_down_button = qtw.QToolButton(trace_group)
        move_buttons = qtw.QWidget(trace_group)
        move_buttons_layout = qtw.QVBoxLayout(move_buttons)
        move_buttons_layout.setContentsMargins(0, 0, 0, 0)
        move_buttons_layout.setSpacing(0)
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
            button.setFixedSize(22, 15)
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            style = self.style()
            if style is not None:
                button.setIcon(style.standardIcon(pixmap))
            move_buttons_layout.addWidget(button)
        table_tools.addWidget(move_buttons)
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

        self.line_enable = qtw.QCheckBox("Line")
        self.line_color = qtw.QComboBox(); self._add_color_items(self.line_color, TRACE_COLOR_PALETTE)
        self.line_width = qtw.QDoubleSpinBox(); self.line_width.setRange(0.5, 15); self.line_width.setValue(2); self.line_width.setDecimals(1); self.line_width.setSingleStep(0.5)
        self.line_style = qtw.QComboBox(); self._add_line_style_items(self.line_style)
        self.dots_enable = qtw.QCheckBox("Dots")
        self.dots_color = qtw.QComboBox(); self._add_color_items(self.dots_color, TRACE_COLOR_PALETTE)
        self.dots_size = qtw.QDoubleSpinBox(); self.dots_size.setRange(1, 20); self.dots_size.setDecimals(1); self.dots_size.setSingleStep(1)
        self.marker_enable = qtw.QCheckBox("Markers")
        self.marker_color = qtw.QComboBox(); self._add_color_items(self.marker_color, TRACE_COLOR_PALETTE)
        self.marker_symbol = qtw.QComboBox(); self._add_marker_symbol_items(self.marker_symbol)
        self.marker_size = qtw.QDoubleSpinBox(); self.marker_size.setRange(1, 30); self.marker_size.setValue(10); self.marker_size.setDecimals(1); self.marker_size.setSingleStep(1)
        self.x_axis = qtw.QComboBox(); self.x_axis.addItems(["Bottom"])
        self.y_axis = qtw.QComboBox(); self.y_axis.addItems(["Left", "Right"])
        self.visible = qtw.QCheckBox("Visible")
        self.visible.setChecked(True)

        for combo in (self.x_axis, self.y_axis):
            combo.setMinimumContentsLength(4)
        color_combo_width = 86
        for combo in (self.line_color, self.dots_color, self.marker_color):
            combo.setIconSize(QtCore.QSize(42, 16))
            combo.setFixedWidth(color_combo_width)
            combo_view = combo.view()
            if combo_view is not None:
                combo_view.setMinimumWidth(color_combo_width)
        for spin in (self.line_width, self.dots_size, self.marker_size):
            spin.setObjectName("traceAppearanceSpin")
            spin.setFixedWidth(76)
        style_symbol_width = 86
        self.line_style.setIconSize(QtCore.QSize(58, 16))
        self.line_style.setFixedWidth(style_symbol_width)
        line_style_view = self.line_style.view()
        if line_style_view is not None:
            line_style_view.setMinimumWidth(style_symbol_width)
        self.marker_symbol.setIconSize(QtCore.QSize(28, 18))
        self.marker_symbol.setFixedWidth(style_symbol_width)
        marker_symbol_view = self.marker_symbol.view()
        if marker_symbol_view is not None:
            marker_symbol_view.setMinimumWidth(style_symbol_width)
        axis_combo_width = 108
        self.x_axis.setFixedWidth(axis_combo_width)
        self.y_axis.setFixedWidth(axis_combo_width)

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
            [("Width", self.line_width), ("", self.line_style)],
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
            [("Size", self.marker_size), ("", self.marker_symbol)],
            )
        display_layout.setColumnStretch(5, 1)
        panel_layout.addWidget(display_group)

        panel_layout.addWidget(self.visible)

        trace_settings = qtw.QGroupBox("Axes", panel)
        trace_grid = qtw.QGridLayout(trace_settings)
        trace_grid.setContentsMargins(8, 10, 8, 8)
        trace_grid.setHorizontalSpacing(8)
        trace_grid.setVerticalSpacing(4)
        trace_grid.addWidget(qtw.QLabel("Horizontal"), 0, 0)
        trace_grid.addWidget(self.x_axis, 0, 1)
        trace_grid.addWidget(qtw.QLabel("Vertical"), 1, 0)
        trace_grid.addWidget(self.y_axis, 1, 1)
        trace_grid.setColumnStretch(2, 1)
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
        self.dots_enable.toggled.connect(self._dots_enabled_changed)
        self.marker_enable.toggled.connect(self._markers_enabled_changed)
        for _widget, signal in [
            (self.line_enable, self.line_enable.toggled), (self.line_color, self.line_color.currentIndexChanged),
            (self.line_width, self.line_width.valueChanged), (self.line_style, self.line_style.currentIndexChanged),
            (self.dots_enable, self.dots_enable.toggled), (self.dots_color, self.dots_color.currentIndexChanged),
            (self.dots_size, self.dots_size.valueChanged), (self.marker_enable, self.marker_enable.toggled),
            (self.marker_color, self.marker_color.currentIndexChanged), (self.marker_symbol, self.marker_symbol.currentIndexChanged),
            (self.marker_size, self.marker_size.valueChanged), (self.x_axis, self.x_axis.currentTextChanged),
            (self.y_axis, self.y_axis.currentTextChanged), (self.visible, self.visible.toggled),
        ]:
            signal.connect(self._apply_selection)
        self._update_control_enabled_states(False)

    def _dots_enabled_changed(self, enabled: bool) -> None:
        if not enabled:
            return

        was_building = self._building
        self._building = True
        try:
            self.marker_enable.setChecked(False)
        finally:
            self._building = was_building

    def _markers_enabled_changed(self, enabled: bool) -> None:
        if not enabled:
            return

        was_building = self._building
        self._building = True
        try:
            self.dots_enable.setChecked(False)
        finally:
            self._building = was_building

    def _add_display_row(
            self,
            layout: qtw.QGridLayout,
            row: int,
            toggle: qtw.QCheckBox,
            color_combo: qtw.QComboBox,
            controls: list[tuple[str, qtw.QWidget]],
            ) -> None:
        layout.addWidget(toggle, row, 0)
        layout.addWidget(color_combo, row, 1)
        column = 2
        for label, widget in controls:
            if label:
                layout.addWidget(qtw.QLabel(label), row, column)
                layout.addWidget(widget, row, column + 1)
                column += 2
            else:
                layout.addWidget(widget, row, column)
                column += 1

    def _add_color_items(self, combo: qtw.QComboBox, colors: Sequence[str]) -> None:
        for color in colors:
            combo.addItem(self._color_icon(color), "", color)
        combo.addItem("Custom", self._CUSTOM_COLOR_DATA)

    def _add_line_style_items(self, combo: qtw.QComboBox) -> None:
        for name, _pen_style in self._LINE_STYLES:
            combo.addItem(self._line_style_icon(name), "", name)

    def _add_marker_symbol_items(self, combo: qtw.QComboBox) -> None:
        for symbol in self._MARKER_SYMBOLS:
            combo.addItem(self._marker_symbol_icon(symbol), "", symbol)

    def refresh_theme(self) -> None:
        for index, (name, _pen_style) in enumerate(self._LINE_STYLES):
            self.line_style.setItemIcon(index, self._line_style_icon(name))
        for index, symbol in enumerate(self._MARKER_SYMBOLS):
            self.marker_symbol.setItemIcon(
                index,
                self._marker_symbol_icon(symbol),
                )
        self.refresh_rows()

    def _color_icon(self, color: str) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(38, 16)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setPen(QtGui.QPen(QtGui.QColor("#444444")))
        painter.setBrush(QtGui.QBrush(QtGui.QColor(color)))
        painter.drawRoundedRect(1, 1, 36, 14, 2, 2)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _line_style_icon(self, style: str) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(54, 16)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.Text))
        pen.setWidthF(2.0)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setStyle(self._line_pen_style(style))
        painter.setPen(pen)
        painter.drawLine(4, pixmap.height() // 2, pixmap.width() - 4, pixmap.height() // 2)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _marker_symbol_icon(self, symbol: str) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(24, 18)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        color = self.palette().color(QtGui.QPalette.ColorRole.Text)
        pen = QtGui.QPen(color)
        pen.setWidthF(1.6)
        painter.setPen(pen)
        painter.setBrush(QtGui.QBrush(color))
        self._draw_marker_symbol(
            painter,
            QtCore.QPointF(pixmap.width() / 2, pixmap.height() / 2),
            5.0,
            symbol,
            )
        painter.end()
        return QtGui.QIcon(pixmap)

    def _line_pen_style(self, style: str) -> QtCore.Qt.PenStyle:
        return dict(self._LINE_STYLES).get(style, QtCore.Qt.PenStyle.SolidLine)

    def _combo_value(self, combo: qtw.QComboBox) -> str:
        data = combo.itemData(combo.currentIndex(), QtCore.Qt.ItemDataRole.UserRole)
        if data == self._CUSTOM_COLOR_DATA:
            return self._selected_style_color(combo)
        if data is None:
            return combo.currentText()
        return str(data)

    def _set_combo_value(self, combo: qtw.QComboBox, value: str) -> None:
        previous_blocked = combo.blockSignals(True)
        try:
            normalized_value = (
                QtGui.QColor(value).name()
                if isinstance(value, str) and value.startswith("#")
                else value
                )
            for index in range(combo.count()):
                data = combo.itemData(index, QtCore.Qt.ItemDataRole.UserRole)
                if isinstance(data, str) and data.startswith("#"):
                    data = QtGui.QColor(data).name()
                if combo.itemText(index) == value or data == normalized_value:
                    combo.setCurrentIndex(index)
                    return

            if isinstance(normalized_value, str) and normalized_value.startswith("#"):
                custom_index = self._custom_color_index(combo)
                insert_index = custom_index if custom_index >= 0 else combo.count()
                combo.insertItem(
                    insert_index,
                    self._color_icon(normalized_value),
                    "",
                    normalized_value,
                    )
                combo.setCurrentIndex(insert_index)
                return

            combo.setCurrentText(value)
        finally:
            combo.blockSignals(previous_blocked)

    def _custom_color_index(self, combo: qtw.QComboBox) -> int:
        for index in range(combo.count()):
            if combo.itemData(index, QtCore.Qt.ItemDataRole.UserRole) == self._CUSTOM_COLOR_DATA:
                return index
        return -1

    def _selected_style_color(self, combo: qtw.QComboBox) -> str:
        attr = {
            self.line_color: "line_color",
            self.dots_color: "dots_color",
            self.marker_color: "markers_color",
            }.get(combo)
        labels = self._selected_labels()
        if attr and labels:
            style = self.owner._trace_styles.get(labels[0])
            if style is not None:
                return str(getattr(style, attr))
        return TRACE_COLOR_PALETTE[0]

    def _resolve_custom_color_selection(self) -> bool:
        for combo in (self.line_color, self.dots_color, self.marker_color):
            if combo.itemData(combo.currentIndex(), QtCore.Qt.ItemDataRole.UserRole) != self._CUSTOM_COLOR_DATA:
                continue

            previous_color = self._selected_style_color(combo)
            color = qtw.QColorDialog.getColor(
                QtGui.QColor(previous_color),
                self,
                "Select trace color",
                )
            if not color.isValid():
                self._set_combo_value(combo, previous_color)
                return False

            self._set_combo_value(combo, color.name())
        return True

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
            pen.setStyle(self._line_pen_style(style.line_style))
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
            for row, (trace_key, line) in enumerate(self.owner.lines.items()):
                self.table.insertRow(row)
                label = self.owner._trace_display_label(trace_key, line)
                trace_id = label.split()[0].replace("ID:", "") if label.startswith("ID:") else str(row + 1)
                measurement = self.owner._trace_measurement_name(trace_key, line)
                trace_is_draggable = self._trace_mime_data(trace_key) is not None
                style = self.owner._trace_styles.setdefault(
                    trace_key,
                    self.owner._initial_trace_style(order=row),
                    )

                values = {
                    self._COL_ID: trace_id,
                    self._COL_MEASUREMENT: measurement,
                    }
                for col, value in values.items():
                    item = qtw.QTableWidgetItem(value)
                    flags = item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable
                    if not trace_is_draggable:
                        flags &= ~QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    item.setFlags(flags)
                    if col == self._COL_ID:
                        item.setTextAlignment(
                            QtCore.Qt.AlignmentFlag.AlignRight
                            | QtCore.Qt.AlignmentFlag.AlignVCenter
                            )
                    else:
                        item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)
                    item.setToolTip(value)
                    self.table.setItem(row, col, item)

                preview_item = qtw.QTableWidgetItem()
                preview_item.setIcon(self._trace_icon(style))
                preview_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                preview_flags = (
                    preview_item.flags()
                    & ~QtCore.Qt.ItemFlag.ItemIsEditable
                    )
                if trace_is_draggable:
                    preview_flags |= QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    preview_item.setToolTip("Drag trace preview")
                else:
                    preview_flags &= ~QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    preview_item.setToolTip("This trace cannot be dragged between plots")
                preview_item.setFlags(preview_flags)
                self.table.setItem(row, self._COL_PREVIEW, preview_item)

                label_item = self.table.item(row, self._COL_ID)
                if label_item is not None:
                    label_item.setData(QtCore.Qt.ItemDataRole.UserRole, trace_key)
                    label_item.setToolTip(label)
                self.table.setRowHeight(row, 28)
                if trace_key in selected:
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
                self.owner._initial_trace_style(order=row_count - row - 1),
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

    def _selected_labels(self) -> list[Any]:
        labels: list[Any] = []
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return labels
        for idx in selection_model.selectedRows():
            item = self.table.item(idx.row(), self._COL_ID)
            if item is not None:
                label = item.data(QtCore.Qt.ItemDataRole.UserRole)
                if label in self.owner.lines:
                    labels.append(label)
        return labels

    def _trace_key_for_row(self, row: int) -> Any:
        item = self.table.item(row, self._COL_ID)
        if item is None:
            return None
        return item.data(QtCore.Qt.ItemDataRole.UserRole)

    def _trace_mime_data(self, label: Any) -> QtCore.QMimeData | None:
        line = self.owner.lines.get(label)
        source = (
            self.owner
            if line is self.owner.__dict__.get("line")
            else getattr(line, "from_win", None)
            )
        if source is None:
            return None

        guid = getattr(source, "_guid", "")
        param = getattr(source, "param", None)
        parameter = getattr(param, "name", "")
        if (
                not guid
                or not parameter
                or len(getattr(param, "depends_on_", ())) != 1
                ):
            return None
        return make_run_preview_mime(
            guid,
            parameter,
            getattr(param, "depends_on_", ()),
            getattr(getattr(source, "_dataset_key", None), "database_path", None),
            )

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
            self.line_enable.setChecked(style.line_enabled); self._set_combo_value(self.line_color, style.line_color); self.line_width.setValue(style.line_width); self._set_combo_value(self.line_style, style.line_style)
            self.dots_enable.setChecked(style.dots_enabled); self._set_combo_value(self.dots_color, style.dots_color); self.dots_size.setValue(style.dots_size)
            self.marker_enable.setChecked(style.markers_enabled); self._set_combo_value(self.marker_color, style.markers_color); self._set_combo_value(self.marker_symbol, style.markers_symbol); self.marker_size.setValue(style.markers_size)
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
        selected_lines = [
            self.owner.lines.get(label)
            for label in self._selected_labels()
            ] if has_selection else []
        self.y_axis.setEnabled(
            has_selection
            and all(line is not self.owner.__dict__.get("line") for line in selected_lines)
            )
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
        if not self._resolve_custom_color_selection():
            self._update_control_enabled_states(bool(self._selected_labels()))
            return
        self._update_control_enabled_states(bool(self._selected_labels()))
        for label in self._selected_labels():
            style = self.owner._trace_styles.setdefault(
                label,
                self.owner._initial_trace_style(),
                )
            style.line_enabled = self.line_enable.isChecked(); style.line_color = self._combo_value(self.line_color); style.line_width = self.line_width.value(); style.line_style = self._combo_value(self.line_style)
            style.dots_enabled = self.dots_enable.isChecked(); style.dots_color = self._combo_value(self.dots_color); style.dots_size = self.dots_size.value()
            style.markers_enabled = self.marker_enable.isChecked(); style.markers_color = self._combo_value(self.marker_color); style.markers_symbol = self._combo_value(self.marker_symbol); style.markers_size = self.marker_size.value()
            style.x_axis = self.x_axis.currentText()
            if self.y_axis.isEnabled():
                style.y_axis = self.y_axis.currentText()
            style.visible = self.visible.isChecked()
            line = self.owner.lines.get(label)
            if line is not None:
                self.owner._apply_trace_style(label, line)
        self.refresh_rows()
