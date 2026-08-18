from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._plot_refresh import plot_refresh_required
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
        top_right_vb: Any
        top_vb: Any
        scrollWidget: qtw.QWidget
        spinBox: Any
        vb: Any

        def initAxes(self) -> None: ...

        def initMenu(self) -> None: ...

        def update_theme(self, config: Any) -> None: ...

        def closeEvent(self, event: object) -> None: ...

        def add_trace_from_dialog(self, label: str, trace_key: Any) -> None: ...
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
        opacity: float = 1.0
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

    def _ensure_trace_axis_viewboxes(
            self,
            *,
            top: bool = False,
            right: bool = False,
            ) -> None:
        """Create the overlay viewboxes needed by a trace's axis pair."""

        if "vb" not in self.__dict__ or "plot" not in self.__dict__:
            return

        if top and right:
            top = right = True

        if right and self.__dict__.get("right_vb") is None:
            self.right_vb = pg.ViewBox()
            self.right_vb.setDefaultPadding(0)
            self.plot.scene().addItem(self.right_vb)
            self.plot.getAxis("right").linkToView(self.right_vb)
            self.right_vb.setXLink(self.vb)

        if top and self.__dict__.get("top_vb") is None:
            self.top_vb = pg.ViewBox()
            self.top_vb.setDefaultPadding(0)
            self.plot.scene().addItem(self.top_vb)
            self.plot.getAxis("top").linkToView(self.top_vb)
            self.top_vb.setYLink(self.vb)

        if (
                top
                and right
                and self.__dict__.get("top_right_vb") is None
                ):
            self.top_right_vb = pg.ViewBox()
            self.top_right_vb.setDefaultPadding(0)
            self.plot.scene().addItem(self.top_right_vb)
            self.top_right_vb.setXLink(self.top_vb)
            self.top_right_vb.setYLink(self.right_vb)

        if not self.__dict__.get("_trace_axis_viewboxes_connected", False):
            self.vb.main_moved.connect(self.updateViews)
            self.vb.sigResized.connect(self.updateViews)
            self.plot.autoBtn.clicked.connect(self._trace_axis_auto_button_clicked)
            self.vb.autoRange_triggered.connect(
                self._trace_axis_auto_range_requested
            )
            self._trace_axis_viewboxes_connected = True

        install_range_handlers = getattr(
            self,
            "_install_axis_scale_viewbox_range_handlers",
            None,
        )
        if callable(install_range_handlers):
            if self.__dict__.get("right_vb") is not None:
                install_range_handlers(self.right_vb, y_axis="y2")
            if self.__dict__.get("top_vb") is not None:
                install_range_handlers(self.top_vb, x_axis="x2")

        self.updateViews(None)

    def _trace_axis_viewbox(self, style: _TraceStyle) -> Any:
        """Return the viewbox representing one horizontal/vertical axis pair."""

        uses_top = style.x_axis == "Top"
        uses_right = style.y_axis == "Right"
        if uses_top or uses_right:
            self._ensure_trace_axis_viewboxes(top=uses_top, right=uses_right)
        if uses_top and uses_right:
            return self.top_right_vb
        if uses_top:
            return self.top_vb
        if uses_right:
            return self.right_vb
        return self.vb

    def _move_trace_to_axis_viewbox(self, line: Any, target: Any) -> None:
        """Move a trace to its axis-pair viewbox without changing its data."""

        get_viewbox = getattr(line, "getViewBox", None)
        current = get_viewbox() if callable(get_viewbox) else None
        if current is target:
            return

        if current is self.vb:
            self.plot.removeItem(line)
        elif current is not None:
            current.removeItem(line)

        if target is self.vb:
            self.plot.addItem(line)
        else:
            target.addItem(line)

    @QtCore.pyqtSlot()
    def _trace_axis_auto_button_clicked(self) -> None:
        """Mirror the plot auto button to every trace-axis viewbox."""

        enabled = self.plot.autoBtn.mode == "auto"
        if not enabled:
            self.__dict__.get("_axis_scale_custom_auto_axes", set()).clear()
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.enableAutoRange(enable=enabled)

    @QtCore.pyqtSlot()
    def _trace_axis_auto_range_requested(self) -> None:
        """Auto-range overlay viewboxes, then merge per-axis trace bounds."""

        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.autoRange()
        force_autoscale = getattr(self, "force_all_axes_autoscale", None)
        if callable(force_autoscale):
            force_autoscale()
        else:
            self._refresh_trace_axis_auto_ranges()

    def _refresh_trace_axis_auto_ranges(self) -> None:
        """Merge automatic bounds across every viewbox contributing to an axis."""

        if "vb" not in self.__dict__:
            return
        axis_is_used = getattr(self, "_axis_scale_axis_is_used", None)
        axis_viewbox = getattr(self, "_axis_scale_viewbox", None)
        apply_filtered = getattr(self, "_apply_axis_scale_filtered_auto", None)
        if not all(callable(method) for method in (
                axis_is_used,
                axis_viewbox,
                apply_filtered,
                )):
            return

        custom_axes = self.__dict__.get("_axis_scale_custom_auto_axes", set())
        for axis in ("x", "y", "x2", "y2"):
            if not axis_is_used(axis):
                custom_axes.discard(axis)
                continue
            viewbox = axis_viewbox(axis)
            axis_number = 0 if axis in ("x", "x2") else 1
            auto_range = viewbox.getState(copy=False)["autoRange"][axis_number]
            if axis in custom_axes or auto_range is not False:
                apply_filtered(axis)

    def _refresh_top_axis_auto_range(self) -> None:
        """Compatibility wrapper for refreshing all linked trace axes."""

        self._refresh_trace_axis_auto_ranges()

    def _trace_axis_parameter(self, trace: Any, display_axis: str) -> Any:
        """Return the parameter plotted on one display axis for ``trace``."""

        host_axis_param = self.__dict__.get("axis_param", {})
        if trace is self.__dict__.get("line"):
            return host_axis_param.get(display_axis) or getattr(self, "param", None)

        source = getattr(trace, "from_win", None)
        if source is None:
            return host_axis_param.get(display_axis)

        source_axis = display_axis
        choose_from = getattr(trace, "choose_from", None)
        if choose_from is not None and len(choose_from) == 2:
            source_axis = choose_from[0 if display_axis == "x" else 1]

        source_axis_param = getattr(source, "axis_param", {})
        if isinstance(source_axis_param, dict):
            param = source_axis_param.get(source_axis)
            if param is not None:
                return param

        try:
            source_options = source.axis_options
        except (AttributeError, RuntimeError):
            source_options = {}
        source_name = source_options.get(source_axis)
        source_params = getattr(source, "param_dict", {})
        param = source_params.get(source_name) if source_name is not None else None
        if param is not None:
            return param

        # A regular 1D source's y parameter is its dependent measurement. If
        # metadata is incomplete, a shared display axis is still described by
        # the host parameter rather than by the wrong source measurement.
        if source_axis == "y":
            param = getattr(source, "param", None)
            if param is not None:
                return param
        return host_axis_param.get(display_axis) or getattr(source, "param", None)

    def _sync_vertical_axis_visibility(self, side: str) -> None:
        """Synchronise one vertical axis with the traces assigned to it."""

        plot = self.__dict__.get("plot")
        if plot is None:
            return

        axis_name = side.lower()
        style_side = "Right" if axis_name == "right" else "Left"
        styles = self._ensure_trace_styles()
        traces = [
            (key, line)
            for key, line in self.__dict__.get("lines", {}).items()
            if line is not None
            and styles.get(key, self._initial_trace_style()).y_axis == style_side
        ]
        axis = plot.getAxis(axis_name)
        axis.setStyle(showValues=bool(traces))
        if not traces:
            axis.setLabel(text="", units="")
        else:
            trace_key, trace = traces[0]
            param = self._trace_axis_parameter(trace, "y")
            label = getattr(param, "label", None) or self._trace_measurement_name(
                trace_key,
                trace,
            )
            unit = getattr(param, "unit", "") or ""
            axis.setLabel(text=str(label), units=str(unit))

        sync_tabs = getattr(self, "_sync_axis_scale_tab_states", None)
        if callable(sync_tabs):
            sync_tabs()

    def _sync_right_axis_visibility(self) -> None:
        """Synchronise right-axis values and label with its active trace."""

        self._sync_vertical_axis_visibility("right")

    def _sync_left_axis_visibility(self) -> None:
        """Synchronise left-axis values and label with its active trace."""

        self._sync_vertical_axis_visibility("left")

    def _sync_top_axis_visibility(self) -> None:
        """Synchronise the linked top x-axis with traces assigned to it."""

        plot = self.__dict__.get("plot")
        if plot is None:
            return

        styles = self._ensure_trace_styles()
        top_traces = [
            (key, line)
            for key, line in self.__dict__.get("lines", {}).items()
            if line is not None
            and styles.get(key, self._initial_trace_style()).x_axis == "Top"
        ]
        top_axis = plot.getAxis("top")
        top_axis.setStyle(showValues=bool(top_traces))
        if not top_traces:
            top_axis.setLabel(text="", units="")
            sync_tabs = getattr(self, "_sync_axis_scale_tab_states", None)
            if callable(sync_tabs):
                sync_tabs()
            self._refresh_top_axis_auto_range()
            return

        trace_key, trace = top_traces[0]
        axis_param = self._trace_axis_parameter(trace, "x")
        label = getattr(axis_param, "label", None) or getattr(axis_param, "name", "")
        if not label:
            label = self._trace_measurement_name(trace_key, trace)
        unit = getattr(axis_param, "unit", "") or ""
        top_axis.setLabel(text=str(label), units=str(unit))
        sync_tabs = getattr(self, "_sync_axis_scale_tab_states", None)
        if callable(sync_tabs):
            sync_tabs()
        self._refresh_top_axis_auto_range()

    def _set_param_axis_labels(self) -> None:
        """Update primary labels, then restore any selected top trace axis."""

        super()._set_param_axis_labels()
        self._sync_left_axis_visibility()
        self._sync_right_axis_visibility()
        self._sync_top_axis_visibility()

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
        self._install_trace_appearance_click_handler(self.label, line)
        install_axis_handler = getattr(self, "_install_axis_scale_trace_handler", None)
        if callable(install_axis_handler):
            install_axis_handler(line)

    def _install_trace_appearance_click_handler(self, trace_key: Any, line: Any) -> None:
        """Open Trace Appearance for a trace when it is double-clicked."""

        if line is None or getattr(line, "_qplot_trace_click_handler", False):
            return

        set_clickable = getattr(line, "setCurveClickable", None)
        if not callable(set_clickable):
            return

        set_clickable(True, width=8)
        signal = getattr(line, "sigClicked", None)
        if signal is None or not callable(getattr(signal, "connect", None)):
            return

        signal.connect(
            lambda _line, event, key=trace_key: self._trace_clicked(key, event)
        )
        line._qplot_trace_click_handler = True

    def _trace_clicked(self, trace_key: Any, event: Any) -> None:
        """Show the clicked trace's settings only for a left double-click."""

        is_double_click = getattr(event, "double", lambda: False)()
        is_left_click = (
            getattr(event, "button", lambda: None)()
            == QtCore.Qt.MouseButton.LeftButton
        )
        if is_double_click and is_left_click:
            self.open_trace_appearance_dialog(trace_key)

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
        Sets up the hidden 1D axis state and trace controls.

        A 1D plot only has one independent/dependent axis pair. The visible
        dropdown dock is therefore replaced by the Swap X/Y checkbox in Trace
        Appearance. Two-dimensional plots continue to use the dock.

        """
        super().initAxes()

        self.axes_dock.hide()
        self.axes_dock.toggleViewAction().setVisible(False)
        
        # Keep the legacy picker widgets only as an internal bridge for
        # preview drops and hidden-source construction. Trace Appearance is
        # the sole visible trace-control surface.
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
        self.lineScroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.lineScroll.setSizePolicy(
            qtw.QSizePolicy.Policy.Expanding,
            qtw.QSizePolicy.Policy.Expanding,
            )
        self.lineScroll.hide()
        
        # QScrollArea can only take 1 widget. That widget holds the layout.
        self.scrollWidget = qtw.QWidget()
        self.scrollWidget.setSizePolicy(
            qtw.QSizePolicy.Policy.Ignored,
            qtw.QSizePolicy.Policy.Preferred,
            )
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
        Refresh the scroll area's geometry without preventing dock resizing.

        Trace controls provide a useful preferred size, but the containing dock
        must remain shrinkable on smaller plot windows. A horizontal scrollbar
        exposes any controls that cannot fit at the user's chosen width.
        """
        self.scrollWidget.adjustSize()
        self.lineScroll.setMinimumWidth(1)
        self.lineScroll.updateGeometry()
        
        
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
        dialog = self.__dict__.get("_trace_appearance_dialog")
        if dialog is not None:
            dialog.refresh_available_traces()

    def add_trace_from_dialog(self, label: str, trace_key: Any) -> None:
        """Add a trace selected in Trace Appearance."""

        request = self.__dict__.get("_trace_add_request")
        dataset_key = getattr(trace_key, "dataset_key", None)
        parameter_name = getattr(trace_key, "parameter_name", None)
        if callable(request) and dataset_key is not None and parameter_name:
            request(dataset_key, parameter_name)
            return

        selected_box = next(
            (
                box
                for box in self.option_boxes
                if box.option_box.isEnabled()
                and box.option_box.currentIndex() < 0
            ),
            None,
        )
        if selected_box is None:
            self.add_option_box()
            selected_box = self.option_boxes[-1]

        selected_box.option_box.blockSignals(True)
        try:
            selected_box.option_box.clear()
            selected_box.option_box.addItem(label, userData=trace_key)
            selected_box.option_box.setCurrentIndex(0)
            selected_box.option_box.setEnabled(False)
            selected_box.del_box.setEnabled(True)
        finally:
            selected_box.option_box.blockSignals(False)

        self.add_line(label, trace_key)

    def available_trace_candidates(self) -> list[tuple[str, Any]]:
        """Return database-backed traces eligible for Trace Appearance."""

        provider = self.__dict__.get("_trace_candidate_provider")
        if callable(provider):
            return list(provider())
        return [
            (source.label, self._window_trace_key(source))
            for source in getattr(self, "mergable", [])
        ]


    def refresh_secondary_lines(self) -> None:
        """
        Refreshes added trace lines and restarts hidden live-trace monitors.

        """
        for line in self._secondary_lines():
            line.refresh()
            from_win = line.from_win
            if (
                not from_win.visible
                and plot_refresh_required(from_win)
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
        
        # Secondary traces may be assigned to the right axis immediately.
        self._ensure_trace_axis_viewboxes(right=True)
            
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
        if self.plot_axes_swapped():
            self._transpose_trace_axis_style(style)
        self._ensure_trace_controls()[trace_key] = selected_box
        self._apply_trace_style(trace_key, subplot)
        self._install_trace_appearance_click_handler(trace_key, subplot)
        install_axis_handler = getattr(self, "_install_axis_scale_trace_handler", None)
        if callable(install_axis_handler):
            install_axis_handler(subplot)
        
    
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
            if option.parent() is not None:
                option.setParent(None)
                option.deleteLater()
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
        get_viewbox = getattr(line, "getViewBox", None)
        viewbox = get_viewbox() if callable(get_viewbox) else None
        if viewbox is self.vb:
            self.plot.removeItem(line)
        elif viewbox is not None:
            viewbox.removeItem(line)
        else:
            # Compatibility fallback for lightweight test and plugin traces.
            owner = self.plot if side.lower() == "left" else self.right_vb
            owner.removeItem(line)

        self._sync_right_axis_visibility()
        self._sync_left_axis_visibility()
        self._sync_top_axis_visibility()
        
        # Remove track of window
        self.remove_dataset.emit(line.from_win._dataset_key)
        
        # Update box options
        self.get_mergables.emit()
        # Resize dock widget
        self._resize_scrollArea()
        dialog = self.__dict__.get("_trace_appearance_dialog")
        if dialog is not None:
            dialog.refresh_rows()

    def _default_plot_axis_names(self) -> tuple[str, str] | None:
        """Return the independent/dependent axis pair for a 1D plot."""

        independent = tuple(getattr(self.param, "depends_on_", ()))
        dependent = getattr(self.param, "name", "")
        if len(independent) != 1 or not dependent or independent[0] == dependent:
            return None
        return str(independent[0]), str(dependent)

    def can_swap_plot_axes(self) -> bool:
        """Return whether this window has a valid, reversible 1D axis pair."""

        names = self._default_plot_axis_names()
        dropdowns = self.__dict__.get("axis_dropdown", {})
        if names is None or set(dropdowns) != {"x", "y"}:
            return False

        for dropdown in dropdowns.values():
            if any(dropdown.findText(name) < 0 for name in names):
                return False

        axis_data = self.__dict__.get("axis_data")
        if isinstance(axis_data, dict) and {"x", "y"}.issubset(axis_data):
            try:
                if len(axis_data["x"]) != len(axis_data["y"]):
                    return False
            except TypeError:
                return False
        return True

    def plot_axes_swapped(self) -> bool:
        """Return whether the dependent variable is currently horizontal."""

        names = self._default_plot_axis_names()
        if names is None:
            return False
        independent, dependent = names
        options = self.axis_options
        return options.get("x") == dependent and options.get("y") == independent

    def set_plot_axes_swapped(self, swapped: bool) -> bool:
        """Apply the requested 1D axis orientation and refresh the plot."""

        if not self.can_swap_plot_axes():
            return False

        names = self._default_plot_axis_names()
        assert names is not None
        independent, dependent = names
        target = (
            {"x": dependent, "y": independent}
            if swapped
            else {"x": independent, "y": dependent}
            )
        previous = dict(self.axis_options)
        if previous == target:
            self._update_axis_context_message()
            return True

        for axis, name in target.items():
            dropdown = self.axis_dropdown[axis]
            was_blocked = dropdown.blockSignals(True)
            try:
                dropdown.setCurrentIndex(dropdown.findText(name))
            finally:
                dropdown.blockSignals(was_blocked)
        self._axis_selection = dict(target)

        if previous == {"x": target["y"], "y": target["x"]}:
            self._transpose_trace_axis_assignments()
            self._swap_loaded_plot_axes()
        self._update_axis_context_message()

        dialog = self.__dict__.get("_trace_appearance_dialog")
        if dialog is not None:
            dialog.sync_swap_axes_control()
        self.refreshWindow(force=True)
        return True

    @staticmethod
    def _transpose_trace_axis_style(style: _TraceStyle) -> None:
        """Transpose a trace's physical axis sides in place."""

        previous_x = style.x_axis
        previous_y = style.y_axis
        style.x_axis = "Bottom" if previous_y == "Left" else "Top"
        style.y_axis = "Left" if previous_x == "Bottom" else "Right"

    def _transpose_trace_axis_assignments(self) -> None:
        """Keep every trace attached to the equivalent axis after X/Y swap."""

        styles = self._ensure_trace_styles()
        for style in styles.values():
            self._transpose_trace_axis_style(style)
        for trace_key, line in self.__dict__.get("lines", {}).items():
            self._apply_trace_style(trace_key, line)

    def _swap_loaded_plot_axes(self) -> None:
        """Swap the displayed 1D arrays and labels before the reload finishes."""

        axis_data = self.__dict__.get("axis_data")
        axis_param = self.__dict__.get("axis_param")
        if not (
                isinstance(axis_data, dict)
                and isinstance(axis_param, dict)
                and {"x", "y"}.issubset(axis_data)
                and {"x", "y"}.issubset(axis_param)
                ):
            return

        axis_data["x"], axis_data["y"] = axis_data["y"], axis_data["x"]
        axis_param["x"], axis_param["y"] = axis_param["y"], axis_param["x"]

        line = self.__dict__.get("line")
        if line is not None:
            line.setData(x=axis_data["x"], y=axis_data["y"])

        clear_marquee = getattr(self, "clear_marquee", None)
        if self.__dict__.get("marquee") is not None and callable(clear_marquee):
            clear_marquee()
        hide_snap_marker = getattr(self, "_hide_snap_marker", None)
        if callable(hide_snap_marker):
            hide_snap_marker()

        self.refresh_secondary_lines()
        self._set_param_axis_labels()
        trace_updated = getattr(self, "trace_updated", None)
        if trace_updated is not None and callable(getattr(trace_updated, "emit", None)):
            trace_updated.emit()

    def _update_axis_context_message(self) -> None:
        """Refresh the persistent axis relationship in the coordinates bar."""

        update = getattr(self, "_update_coordinate_context", None)
        if callable(update):
            update()
    
    
    @QtCore.pyqtSlot(object)
    def updateViews(self, ev: object | None) -> None:
        """
        When moving main viewbox move/scale right viewbox but the same
        relative amount.

        Parameters
        ----------
        ev : PyQt6.<something?>
            
        """
        geometry = self.vb.sceneBoundingRect()
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.setGeometry(geometry)
        right_vb = self.__dict__.get("right_vb")
        top_vb = self.__dict__.get("top_vb")
        constrained_axis = getattr(self.vb, "_main_moved_axis", None)
        if ev is not None:
            if ev.__class__.__name__ == "QGraphicsSceneWheelEvent":
                if right_vb is not None:
                    right_vb.wheelEvent(ev, axis=1)
                if top_vb is not None:
                    top_vb.wheelEvent(ev, axis=0)
            elif ev.__class__.__name__ == "MouseDragEvent":
                if right_vb is not None and constrained_axis in (None, 1):
                    right_vb.mouseDragEvent(ev, axis=1)
                if top_vb is not None and constrained_axis in (None, 0):
                    top_vb.mouseDragEvent(ev, axis=0)

        # Prevent lines in overlay viewboxes from escaping the plot rectangle.
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.setGeometry(geometry)

    def open_trace_appearance_dialog(self, trace_key: Any = None) -> None:
        if self.__dict__.get("_trace_appearance_dialog") is None:
            self._trace_appearance_dialog = _TraceAppearanceDialog(self)
        get_mergables = getattr(self, "get_mergables", None)
        if get_mergables is not None and callable(getattr(get_mergables, "emit", None)):
            get_mergables.emit()
        self._trace_appearance_dialog.refresh_rows()
        if trace_key is not None:
            self._trace_appearance_dialog.select_trace(trace_key)
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
        style.opacity = min(max(float(style.opacity), 0.0), 1.0)
        set_opacity = getattr(line, "setOpacity", None)
        set_graphics_effect = getattr(line, "setGraphicsEffect", None)
        if callable(set_opacity) and callable(set_graphics_effect):
            # PlotDataItem draws its curve and symbols as separate child items.
            # Applying opacity to the parent makes them blend twice where they
            # overlap, so composite the complete trace before fading it instead.
            set_opacity(1.0)
            opacity_effect = getattr(line, "_qplot_trace_opacity_effect", None)
            if style.opacity < 1.0:
                if opacity_effect is None:
                    opacity_effect = qtw.QGraphicsOpacityEffect(line)
                    set_graphics_effect(opacity_effect)
                    line._qplot_trace_opacity_effect = opacity_effect
                opacity_effect.setOpacity(style.opacity)
            elif opacity_effect is not None:
                set_graphics_effect(None)
                line._qplot_trace_opacity_effect = None
        elif callable(set_opacity):
            set_opacity(style.opacity)
        line.setVisible(style.visible)

        target_side = "right" if style.y_axis == "Right" else "left"
        if hasattr(line, "side"):
            line.side = target_side
        if "vb" in self.__dict__ and "plot" in self.__dict__:
            target_viewbox = self._trace_axis_viewbox(style)
            self._move_trace_to_axis_viewbox(line, target_viewbox)
            sync_log_mode = getattr(self, "_sync_axis_scale_line_log_mode", None)
            if callable(sync_log_mode):
                sync_log_mode(label, line)

        self._sync_left_axis_visibility()
        self._sync_right_axis_visibility()
        self._sync_top_axis_visibility()

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
    Trace table that supports internal trace reordering.

    """

    _REORDER_MIME_TYPE = "application/x-qplot-trace-reorder"

    def __init__(self, dialog: "_TraceAppearanceDialog"):
        super().__init__(0, 3, dialog)
        self.dialog = dialog
        self._dragged_rows: list[int] = []
        self._drag_origin = QtCore.QModelIndex()
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._drop_indicator = qtw.QWidget(self.viewport())
        self._drop_indicator.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._drop_indicator.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_StyledBackground
        )
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet("background-color: palette(highlight);")
        self._drop_indicator.hide()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_origin = self.indexAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._drag_origin = QtCore.QModelIndex()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        self._update_reorder_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.viewport().unsetCursor()
        super().leaveEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if self._is_internal_reorder_drag(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if self._is_internal_reorder_drag(event):
            self._show_drop_indicator(
                self._drop_insertion_row(event.position().toPoint())
            )
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self._hide_drop_indicator()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        self._hide_drop_indicator()
        if not self._is_internal_reorder_drag(event):
            event.ignore()
            return

        source_rows = self._dragged_rows
        self._dragged_rows = []
        if not source_rows:
            event.ignore()
            return

        self.dialog._move_rows_to_position(
            source_rows,
            self._drop_insertion_row(event.position().toPoint()),
            )
        event.acceptProposedAction()

    def startDrag(self, supported_actions):
        del supported_actions
        index = self._drag_origin
        if not index.isValid():
            index = self.currentIndex()
        if not index.isValid() or index.column() == self.dialog._COL_PREVIEW:
            return

        source_rows = self.dialog._selected_rows()
        if not source_rows:
            return

        mime_data = QtCore.QMimeData()
        mime_data.setData(self._REORDER_MIME_TYPE, QtCore.QByteArray())
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime_data)
        self._dragged_rows = source_rows
        try:
            drag.exec(QtCore.Qt.DropAction.MoveAction)
        finally:
            self._dragged_rows = []
            self._hide_drop_indicator()

    def _is_internal_reorder_drag(self, event: QtGui.QDropEvent) -> bool:
        return (
            event.source() is self
            and event.mimeData().hasFormat(self._REORDER_MIME_TYPE)
            )

    def _drop_insertion_row(self, position: QtCore.QPoint) -> int:
        index = self.indexAt(position)
        if not index.isValid():
            return self.rowCount()
        if position.y() > self.visualRect(index).center().y():
            return index.row() + 1
        return index.row()

    def _update_reorder_cursor(self, position: QtCore.QPoint) -> None:
        index = self.indexAt(position)
        if index.isValid() and index.column() != self.dialog._COL_PREVIEW:
            self.viewport().setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
            return
        self.viewport().unsetCursor()

    def _show_drop_indicator(self, destination_row: int) -> None:
        if not self.rowCount():
            self._hide_drop_indicator()
            return

        destination_row = max(0, min(destination_row, self.rowCount()))
        if destination_row == self.rowCount():
            index = self.model().index(self.rowCount() - 1, 0)
            y_position = self.visualRect(index).bottom() + 1
        else:
            index = self.model().index(destination_row, 0)
            y_position = self.visualRect(index).top()
        self._drop_indicator.setGeometry(
            0,
            max(0, y_position - 1),
            self.viewport().width(),
            self._drop_indicator.height(),
            )
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def _hide_drop_indicator(self) -> None:
        self._drop_indicator.hide()


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
            }
            QTableWidget#traceAppearanceTable::item {
                padding: 2px 6px;
                border: none;
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

        trace_group = qtw.QGroupBox(self)
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
        self.table.setToolTip("Drag a trace up or down to change its order.")
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDragDropMode(qtw.QAbstractItemView.DragDropMode.DragDrop)
        self.table.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.table.setDropIndicatorShown(False)
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

        trace_layout.addWidget(self.table)
        trace_actions = qtw.QHBoxLayout()
        trace_actions.setContentsMargins(0, 0, 0, 0)
        trace_actions.setSpacing(6)
        self.add_trace_combo = qtw.QComboBox(self)
        self.add_trace_combo.setObjectName("traceAppearanceAddCombo")
        self.add_trace_combo.setMinimumContentsLength(18)
        self.add_trace_combo.setSizeAdjustPolicy(
            qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_trace_combo.setToolTip("Choose an available measurement to add.")
        self.add_trace_button = qtw.QPushButton("Add Trace", self)
        self.add_trace_button.setObjectName("traceAppearanceAddButton")
        self.add_trace_button.setToolTip("Add the selected measurement to this plot.")
        self.add_trace_button.setEnabled(False)
        self.remove_trace_button = qtw.QPushButton("Remove Trace", self)
        self.remove_trace_button.setObjectName("traceAppearanceRemoveButton")
        self.remove_trace_button.setToolTip(
            "Remove the selected secondary trace or traces from this plot."
        )
        self.remove_trace_button.setEnabled(False)
        trace_actions.addWidget(self.add_trace_combo, 1)
        trace_actions.addWidget(self.add_trace_button)
        trace_actions.addWidget(self.remove_trace_button)
        trace_layout.addLayout(trace_actions)
        body.addWidget(trace_group, 5)

        panel = qtw.QWidget(self)
        panel_layout = qtw.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

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
        self.opacity = qtw.QSpinBox(); self.opacity.setRange(0, 100); self.opacity.setValue(100); self.opacity.setSuffix("%")
        self.opacity.setObjectName("traceAppearanceOpacity")
        self.opacity.setToolTip("Trace opacity")
        self.opacity_slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.opacity_slider.setObjectName("traceAppearanceOpacitySlider")
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setToolTip("Trace opacity")
        self.x_axis = qtw.QComboBox(); self.x_axis.addItems(["Bottom", "Top"])
        self.y_axis = qtw.QComboBox(); self.y_axis.addItems(["Left", "Right"])
        self.swap_axes = qtw.QCheckBox("Swap X/Y")
        self.swap_axes.setToolTip(
            "Plot the dependent variable horizontally and the independent "
            "variable vertically."
            )
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
        self.opacity.setFixedWidth(68)
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

        display_group = qtw.QGroupBox(panel)
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

        visibility_layout = qtw.QHBoxLayout()
        visibility_layout.setContentsMargins(0, 0, 0, 0)
        visibility_layout.setSpacing(6)
        visibility_layout.addWidget(self.visible)
        visibility_layout.addSpacing(16)
        visibility_layout.addWidget(qtw.QLabel("Opacity"))
        visibility_layout.addWidget(self.opacity_slider, 1)
        visibility_layout.addWidget(self.opacity)
        panel_layout.addLayout(visibility_layout)

        trace_settings = qtw.QGroupBox("Trace axes", panel)
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

        plot_axes = qtw.QGroupBox("Plot axes", panel)
        plot_axes_layout = qtw.QVBoxLayout(plot_axes)
        plot_axes_layout.setContentsMargins(8, 10, 8, 8)
        plot_axes_layout.addWidget(self.swap_axes)
        panel_layout.addWidget(plot_axes)
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
            self.opacity_slider,
            self.opacity,
            *self._line_controls,
            *self._dots_controls,
            *self._marker_controls,
            ]
        self.dots_enable.toggled.connect(self._dots_enabled_changed)
        self.marker_enable.toggled.connect(self._markers_enabled_changed)
        self.opacity_slider.valueChanged.connect(self.opacity.setValue)
        self.opacity.valueChanged.connect(self.opacity_slider.setValue)
        self.add_trace_combo.currentIndexChanged.connect(
            self._add_trace_selection_changed
        )
        self.add_trace_button.clicked.connect(self._add_selected_trace)
        self.remove_trace_button.clicked.connect(self._remove_selected_traces)
        self.swap_axes.toggled.connect(self._swap_axes_toggled)
        for _widget, signal in [
            (self.line_enable, self.line_enable.toggled), (self.line_color, self.line_color.currentIndexChanged),
            (self.line_width, self.line_width.valueChanged), (self.line_style, self.line_style.currentIndexChanged),
            (self.dots_enable, self.dots_enable.toggled), (self.dots_color, self.dots_color.currentIndexChanged),
            (self.dots_size, self.dots_size.valueChanged), (self.marker_enable, self.marker_enable.toggled),
            (self.marker_color, self.marker_color.currentIndexChanged), (self.marker_symbol, self.marker_symbol.currentIndexChanged),
            (self.marker_size, self.marker_size.valueChanged), (self.x_axis, self.x_axis.currentTextChanged),
            (self.y_axis, self.y_axis.currentTextChanged), (self.visible, self.visible.toggled), (self.opacity, self.opacity.valueChanged),
        ]:
            signal.connect(self._apply_selection)
        self._update_control_enabled_states(False)
        self.sync_swap_axes_control()

    def sync_swap_axes_control(self) -> None:
        """Synchronise the global Swap X/Y checkbox with the plot window."""

        can_swap = getattr(self.owner, "can_swap_plot_axes", None)
        is_swapped = getattr(self.owner, "plot_axes_swapped", None)
        enabled = bool(callable(can_swap) and can_swap())
        checked = bool(enabled and callable(is_swapped) and is_swapped())
        was_blocked = self.swap_axes.blockSignals(True)
        try:
            self.swap_axes.setEnabled(enabled)
            self.swap_axes.setChecked(checked)
        finally:
            self.swap_axes.blockSignals(was_blocked)

    def _swap_axes_toggled(self, checked: bool) -> None:
        """Apply a requested plot-axis orientation immediately."""

        setter = getattr(self.owner, "set_plot_axes_swapped", None)
        if not callable(setter) or not setter(checked):
            self.sync_swap_axes_control()

    def refresh_available_traces(self) -> None:
        """Refresh measurements available to the Add Trace control."""

        previous_key = self.add_trace_combo.currentData()
        plotted_keys = set(self.owner.lines)
        available = [
            (label, trace_key)
            for label, trace_key in self.owner.available_trace_candidates()
            if trace_key not in plotted_keys
        ]

        self.add_trace_combo.blockSignals(True)
        try:
            self.add_trace_combo.clear()
            self.add_trace_combo.addItem("Select a trace to add…", userData=None)
            for label, trace_key in available:
                self.add_trace_combo.addItem(
                    label,
                    userData=trace_key,
                )
            index = self.add_trace_combo.findData(previous_key)
            self.add_trace_combo.setCurrentIndex(max(index, 0))
        finally:
            self.add_trace_combo.blockSignals(False)
        self._add_trace_selection_changed(self.add_trace_combo.currentIndex())

    def _add_trace_selection_changed(self, _index: int) -> None:
        self.add_trace_button.setEnabled(
            self.add_trace_combo.currentData() is not None
        )

    def _add_selected_trace(self, _checked: bool = False) -> None:
        trace_key = self.add_trace_combo.currentData()
        if trace_key is None:
            return
        try:
            self.owner.add_trace_from_dialog(
                self.add_trace_combo.currentText(),
                trace_key,
            )
        except Exception as error:
            qtw.QMessageBox.critical(
                self,
                "Could Not Add Trace",
                f"The selected trace could not be added:\n{error}",
            )
            self.refresh_available_traces()
            return

        self.refresh_rows()
        self.select_trace(trace_key)

    def _remove_selected_traces(self, _checked: bool = False) -> None:
        primary_line = self.owner.__dict__.get("line")
        selected = [
            (trace_key, self.owner.lines.get(trace_key))
            for trace_key in self._selected_labels()
        ]
        removable = [
            (trace_key, line)
            for trace_key, line in selected
            if line is not None and line is not primary_line
        ]
        if len(removable) != len(selected) or not removable:
            return

        for trace_key, line in removable:
            label = self.owner._trace_display_label(trace_key, line)
            self.owner.remove_line(label, trace_key)
        self.refresh_rows()

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
        painter.setOpacity(style.opacity * (1.0 if style.visible else 0.35))

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
        self.sync_swap_axes_control()
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
                    flags = (
                        item.flags()
                        & ~QtCore.Qt.ItemFlag.ItemIsEditable
                        | QtCore.Qt.ItemFlag.ItemIsDragEnabled
                        )
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
                    & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    )
                preview_item.setFlags(preview_flags)
                preview_item.setToolTip("Trace preview")
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

        if self.table.rowCount() and not self._selected_labels():
            self.table.selectRow(0)
        else:
            self._sync_controls_from_selection()
        self.refresh_available_traces()

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

    def _move_rows_to_position(
            self,
            source_rows: Sequence[int],
            destination_row: int,
            ) -> None:
        labels = list(self.owner.lines)
        source_rows = sorted({row for row in source_rows if 0 <= row < len(labels)})
        if not source_rows:
            return

        destination_row = max(0, min(destination_row, len(labels)))
        selected = [labels[row] for row in source_rows]
        destination_row -= sum(row < destination_row for row in source_rows)
        remaining = [
            label for row, label in enumerate(labels)
            if row not in source_rows
            ]
        reordered = (
            remaining[:destination_row]
            + selected
            + remaining[destination_row:]
            )
        if reordered == labels:
            return

        old_lines = self.owner.lines
        reordered_lines = [(label, old_lines[label]) for label in reordered]
        old_lines.clear()
        old_lines.update(reordered_lines)
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

    def select_trace(self, trace_key: Any) -> bool:
        """Select and reveal the row for ``trace_key`` if it is present."""

        for row in range(self.table.rowCount()):
            if self._trace_key_for_row(row) != trace_key:
                continue
            self.table.clearSelection()
            self.table.selectRow(row)
            item = self.table.item(row, self._COL_ID)
            if item is not None:
                self.table.setCurrentItem(item)
                self.table.scrollToItem(item)
            return True
        return False

    def _sync_controls_from_selection(self):
        if self._building:
            return
        labels = self._selected_labels()
        if not labels:
            self._update_control_enabled_states(False)
            return
        style = self.owner._trace_styles[labels[0]]
        self._building = True
        try:
            self.line_enable.setChecked(style.line_enabled); self._set_combo_value(self.line_color, style.line_color); self.line_width.setValue(style.line_width); self._set_combo_value(self.line_style, style.line_style)
            self.dots_enable.setChecked(style.dots_enabled); self._set_combo_value(self.dots_color, style.dots_color); self.dots_size.setValue(style.dots_size)
            self.marker_enable.setChecked(style.markers_enabled); self._set_combo_value(self.marker_color, style.markers_color); self._set_combo_value(self.marker_symbol, style.markers_symbol); self.marker_size.setValue(style.markers_size)
            self.x_axis.setCurrentText(style.x_axis); self.y_axis.setCurrentText(style.y_axis); self.visible.setChecked(style.visible); self.opacity.setValue(round(style.opacity * 100))
        finally:
            self._building = False

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
        self.y_axis.setEnabled(has_selection)
        self.remove_trace_button.setEnabled(
            bool(selected_lines)
            and all(
                line is not None and line is not self.owner.__dict__.get("line")
                for line in selected_lines
            )
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
            style.y_axis = self.y_axis.currentText()
            style.visible = self.visible.isChecked()
            style.opacity = self.opacity.value() / 100
            line = self.owner.lines.get(label)
            if line is not None:
                self.owner._apply_trace_style(label, line)
        self.refresh_rows()
