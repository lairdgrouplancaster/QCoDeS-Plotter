"""A coordinate-preserving 3D slice viewer built on the normal heatmap UI."""

from dataclasses import replace

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.numeric_samples import numeric_isfinite
from qplot.datahandling.plot_progress import PlotProgress
from qplot.tools.volume_worker import VolumeLoader, VolumeTask, processed_plane

from ._commands import command_spec, create_action
from ._widgets.operations import OperationsState
from .plot2d import plot2d


class plot3d(plot2d):
    """One volume and one independently selectable, processed viewing plane."""

    cursor_has_depth = True
    plane_changed = QtCore.pyqtSignal()
    _initial_load_message = "3D viewer ready; indexing recorded coordinates..."
    _extra_publication_fields = (
        "volume", "slice_frame", "_depth_index", "_depth_value", "_applied_operations",
        "_applied_operation_state", "_shared_levels", "_range_complete", "_range_key",
        "_displayed_result",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.volume = None
        self.slice_frame = None
        self._displayed_result = None
        self._depth_value = None
        self._depth_index = 0
        self._navigation_only = False
        self._selected_coordinates = {}
        self._plane_states = {}
        self._range_task = None
        self._prefetch_task = None
        self._range_key = None
        self._range_complete = False
        self._shared_levels = None
        self._applied_operations = ()
        self._applied_operation_state = OperationsState((), 0)
        self._cuts = []
        self._color_mode = "volume"
        self._last_auto_mode = "volume"
        self._frozen = False

    def initAxes(self):
        super().initAxes()
        for axis, name in zip(("x", "y"), self.param.depends_on_[-1:-3:-1], strict=True):
            with QtCore.QSignalBlocker(self.axis_dropdown[axis]):
                self.axis_dropdown[axis].setCurrentText(name)
        self._axis_selection = self.axis_options

    def initOperations(self):
        super().initOperations()
        self.oper_dock.setWindowTitle("Slice operations")
        self.oper_widget.main_layout.itemAt(0).widget().setText("Slice operations")
        self.oper_widget.setToolTip("Applied to each full 2D plane, independently of zoom and selection.")
        self._update_operation_labels()

    def _update_operation_labels(self):
        axes = self.axis_options
        for index in range(self.oper_widget.list_options.count()):
            option = self.oper_widget.list_options.item(index)
            row = option.operation_row
            axis = row.spec.derivative_axis
            label = (f"d({self.param.label or self.param.name})/d({axes[axis]})"
                     if axis in axes else row.spec.name)
            option._label.setText(label)
            row._label.setText(label)

    def initFrame(self):
        super().initFrame()
        self.setMinimumSize(760, 560)
        for action in self.toolbarRef.actions():
            if self.toolbarRef.widgetForAction(action) is self.relevel_refresh:
                action.setVisible(False)
        self.addToolBarBreak(QtCore.Qt.ToolBarArea.TopToolBarArea)
        self.slice_toolbar = qtw.QToolBar("3D slice", self)
        self.slice_toolbar.setObjectName("sliceControls")
        self.slice_toolbar.setMovable(False)
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, self.slice_toolbar)
        self.slice_panel = qtw.QGroupBox("3D slice")
        self.slice_panel.setObjectName("slicePanel")
        self.slice_panel.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        panel_layout = qtw.QVBoxLayout(self.slice_panel)
        panel_layout.setContentsMargins(8, 6, 8, 6)
        panel_layout.setSpacing(4)
        axis_row = qtw.QHBoxLayout()
        axis_row.setSpacing(6)
        panel_layout.addLayout(axis_row)
        self.slice_toolbar.addWidget(self.slice_panel)
        for axis in ("x", "y"):
            self.axis_dropdown[axis].setMinimumContentsLength(12)
            self.axis_dropdown[axis].setSizeAdjustPolicy(
                qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            axis_row.addWidget(qtw.QLabel(f"{axis.upper()}:"))
            axis_row.addWidget(self.axis_dropdown[axis])
        self.depth_label = qtw.QLabel()
        self.follow_latest = qtw.QCheckBox("Follow latest")
        self.follow_latest.setToolTip("Follow the last acquired depth coordinate; manual navigation turns this off.")
        self.follow_latest.toggled.connect(self._follow_latest_changed)
        axis_row.addStretch()
        self.color_mode = qtw.QComboBox()
        for label, mode in (("Whole volume", "volume"), ("Current slice", "slice"), ("Locked", "locked")):
            self.color_mode.addItem(label, mode)
        self.color_mode.setToolTip("Scope of automatic colour limits; whole-volume limits include slice operations.")
        self.color_mode.currentIndexChanged.connect(self._color_mode_changed)
        axis_row.addWidget(qtw.QLabel("Colour:"))
        axis_row.addWidget(self.color_mode)
        self.symmetric_color = qtw.QCheckBox("Symmetric")
        self.symmetric_color.setToolTip("Keep the colour limits equally spaced above and below zero.")
        self.symmetric_color.toggled.connect(lambda _: self.scaleColorbar())
        axis_row.addWidget(self.symmetric_color)
        self.navigator = qtw.QHBoxLayout()
        self.navigator.setSpacing(6)
        panel_layout.addLayout(self.navigator)
        self.slice_slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slice_slider.setMinimumWidth(140)
        self.slice_slider.setTracking(True)
        self.slice_slider.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.slice_slider.setToolTip("Slice index")
        self.slice_slider.sliderPressed.connect(
            lambda: self.slice_slider.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor))
        self.slice_slider.sliderReleased.connect(
            lambda: self.slice_slider.setCursor(QtCore.Qt.CursorShape.OpenHandCursor))
        self.slice_slider.valueChanged.connect(self.select_slice)
        self.navigator.addWidget(self.slice_slider, 1)
        self.navigator.addWidget(qtw.QLabel("Slice:"))
        self.slice_index = qtw.QSpinBox()
        self.slice_index.setToolTip("Slice index")
        self.slice_index.setKeyboardTracking(False)
        self.slice_index.valueChanged.connect(self.select_slice)
        self.navigator.addWidget(self.slice_index)
        self.navigator.addSpacing(8)
        self.navigator.addWidget(self.depth_label)
        self.depth_entry = qtw.QLineEdit()
        self.depth_entry.setMaximumWidth(120)
        self.depth_entry.setMinimumWidth(85)
        self.depth_entry.setToolTip("Enter a coordinate; select the nearest recorded plane.")
        self.depth_entry.returnPressed.connect(self._select_coordinate)
        self.navigator.addWidget(self.depth_entry)
        self.depth_unit = qtw.QLabel()
        self.navigator.addWidget(self.depth_unit)
        self.navigator.addSpacing(8)
        self.navigator.addWidget(self.follow_latest)
        self.volume_status = qtw.QToolBar("Volume status", self.slice_panel)
        self.volume_status.setObjectName("volumeStatus")
        self.volume_status.setMovable(False)
        self.volume_status.setStyleSheet("QToolBar#volumeStatus { border: none; padding: 0; }")
        panel_layout.addWidget(self.volume_status)
        self.slice_status = qtw.QLabel(" Waiting for coordinates")
        self.volume_status.addWidget(self.slice_status)
        self.range_status = qtw.QLabel()
        self.range_status.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        self.volume_status.addWidget(self.range_status)
        self.stop_range = QtGui.QAction("Stop colour scan", self)
        self.stop_range.triggered.connect(self._stop_color_scan)
        self.volume_status.addAction(self.stop_range)
        self.stop_range.setVisible(False)
        self._range_timer = QtCore.QTimer(self)
        self._range_timer.setInterval(100)
        self._range_timer.timeout.connect(self._range_progress)
        self.slice_slider.installEventFilter(self)
        self.slice_index.setEnabled(False)
        self.slice_slider.setEnabled(False)
        self._update_depth_label()

    def initLabels(self):
        super().initLabels()
        self._update_cursor_tooltip()

    def _plot_layout_controls(self):
        return super()._plot_layout_controls() + tuple(
            control for name in ("slice_toolbar", "volume_status")
            if (control := self.__dict__.get(name)) is not None)

    def apply_cursor_label_widths(self):
        super().apply_cursor_label_widths()
        labels = self.__dict__.get("pos_labels", {})
        for label in labels.values():
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        index_label = labels.get("index")
        if index_label is not None:
            volume = self.__dict__.get("volume")
            # Reserve the dataset's maximum digit counts, not the hovered
            # indices. Include a little room for native-font rounding.
            maxima = [max(0, len(axis) - 1) for axis in volume.axes.values()] if volume is not None else [0, 0, 0]
            metrics = index_label.fontMetrics()
            width = max(metrics.horizontalAdvance("[" + ",".join(
                digit * len(str(maximum)) for maximum in maxima) + "]") for digit in "0123456789")
            index_label.setFixedWidth(width + 2)

    def _update_cursor_tooltip(self):
        if "cursor_readout" not in self.__dict__:
            return
        axes = (self._displayed_result.axis_param if self._displayed_result is not None else
                {role: self.param_dict[name] for role, name in self.axis_options.items()})
        parameters = (("X", axes["x"]), ("Y", axes["y"]),
                      ("Depth", self.param_dict[self._displayed_depth_name()]),
                      ("Value", self.__dict__.get("display_param", self.param)))
        lines = ["Indices: [X, Y, depth] (zero-based).", "Values: (X, Y, depth, value).",
                 "— means no finite value is available at this position."]
        for role, parameter in parameters:
            label = parameter.name
            if parameter.label and parameter.label != parameter.name:
                label += f" — {parameter.label}"
            if parameter.unit:
                label += f" ({parameter.unit})"
            lines.append(f"{role}: {label}")
        values = [self.pos_labels[role].text().strip() for role in ("x", "y", "depth", "z")]
        if any(values):
            lines.append(f"Current: {self.pos_labels['index'].text()} ({', '.join(values)})")
        self.cursor_readout.setToolTip("\n".join(lines))

    def format_cursor_number(self, value):
        return super().format_cursor_number(value) if np.isfinite(value) else "—"

    def mouseMoved(self, pos):
        super().mouseMoved(pos)
        depth_label = self.pos_labels.get("depth")
        if depth_label is None:
            return
        visible = not self.pos_labels["z"].isHidden()
        depth_label.setVisible(visible)
        self._cursor_depth_separator.setVisible(visible)
        value = self.slice_frame.depth if self.slice_frame is not None else None
        inside = self.plot.sceneBoundingRect().contains(pos)
        depth_label.setText(self.format_cursor_number(value)
                           if visible and value is not None and inside else "")
        if inside:
            for role in ("x", "y", "depth", "z"):
                label = self.pos_labels[role]
                if not label.isHidden() and not label.text().strip():
                    label.setText("—")
        self._update_cursor_tooltip()

    def initContextMenu(self):
        super().initContextMenu()
        auto_color = self._context_menu_action("Autoscale Color")
        auto_color.triggered.disconnect(self.scaleColorbar)
        auto_color.triggered.connect(self.autoscale_color)
        self._slice_action("slice.depth_cut", lambda: self.openSweep("d"))
        self._slice_action("slice.previous", lambda: self.step_slice(-1))
        self._slice_action("slice.next", lambda: self.step_slice(1))
        self._slice_action("slice.first", lambda: self.select_slice(0))
        self._slice_action("slice.last", lambda: self.select_slice(self.slice_index.maximum()))
        self.vbMenu.addAction("Freeze Slice as 2D Plot", self.freeze_slice)

    def _slice_action(self, name, callback):
        action = create_action(name, self)
        self.register_shortcut(action, command_spec(name))
        def invoke():
            if isinstance(qtw.QApplication.focusWidget(), (qtw.QLineEdit, qtw.QAbstractSpinBox, qtw.QComboBox)):
                return
            callback()
        action.triggered.connect(invoke)
        self.vbMenu.addAction(action)
        return action

    def _depth_name(self):
        return next(name for name in self.param.depends_on_ if name not in self.axis_options.values())

    def _update_depth_label(self):
        name = self._depth_name()
        self.depth_label.setText(f"  {name}: ")
        self.depth_label.setToolTip(f"Depth: {self.param_dict[name].label or name}")
        self.depth_unit.setText(f" {self.param_dict[name].unit} ")

    def _depth_text(self, value, name):
        if value is None:
            return ""
        # Keep routine floating-point tails out of the controls, but retain
        # enough digits that accepting the displayed value selects this plane.
        for digits in (8, 12, 17):
            text = format(value, f".{digits}g")
            if self.volume is None or self.volume.nearest_index(name, float(text)) == self.volume.nearest_index(name, value):
                return text
        return repr(value)

    def _create_plot_loader(self, *args, **kwargs):
        reuse = self.volume if (self._navigation_only or self._frozen or
                                self.volume is not None and self.volume.completed) else None
        worker = VolumeLoader(*args, volume=reuse, depth_value=self._depth_value,
                              depth_index=self._depth_index,
                              follow_latest=self.follow_latest.isChecked(), **kwargs)
        worker.operation_state = self.oper_widget.capture_state()
        return worker

    def _navigate(self):
        if self._closed or not self._plot_setup_complete:
            return
        worker = self.__dict__.get("worker")
        if worker is not None and worker.running:
            worker.cancel()
        self._navigation_only = True
        try:
            self.load_data()
        finally:
            self._navigation_only = False

    def select_slice(self, index):
        if self.volume is None:
            return
        values = self.volume.axes[self._depth_name()]
        if not values.size:
            return
        index = min(max(0, int(index)), len(values) - 1)
        with QtCore.QSignalBlocker(self.follow_latest):
            self.follow_latest.setChecked(False)
        self._depth_index, self._depth_value = index, float(values[index])
        self._selected_coordinates[self._depth_name()] = self._depth_value
        self._navigate()

    def step_slice(self, delta):
        self.select_slice(self._depth_index + delta)

    def _select_coordinate(self):
        if self.volume is None:
            return
        try:
            value = float(self.depth_entry.text())
            if not np.isfinite(value):
                raise ValueError()
        except ValueError:
            self.show_status("Enter a finite depth coordinate.", 5000)
            return
        self.select_slice(self.volume.nearest_index(self._depth_name(), value))

    def _follow_latest_changed(self, enabled):
        if enabled:
            self._navigate()

    def eventFilter(self, source, event):
        if source is self.__dict__.get("slice_slider") and event.type() == QtCore.QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            if delta:
                self.step_slice(1 if delta > 0 else -1)
            event.accept()
            return True
        return super().eventFilter(source, event)

    def change_axis(self, key):
        if not self._plot_setup_complete:
            return
        previous = self._axis_selection
        old_pair = (previous["x"], previous["y"])
        view = self.vb.viewRange()
        self._plane_states[old_pair] = (self.oper_widget.capture_state(), self.marquee, view)
        other = "y" if key == "x" else "x"
        if self.axis_dropdown[key].currentText() == self.axis_dropdown[other].currentText():
            with QtCore.QSignalBlocker(self.axis_dropdown[other]):
                self.axis_dropdown[other].setCurrentText(previous[key])
        current = self.axis_options
        pair = (current["x"], current["y"])
        if pair == old_pair:
            return
        if pair == old_pair[::-1]:
            state = self.oper_widget.capture_state(transpose=True)
            rect = self.marquee
            rect = None if rect is None else QtCore.QRectF(rect.y(), rect.x(), rect.height(), rect.width())
            ranges = view[::-1]
        else:
            state, rect, ranges = self._plane_states.get(pair, (OperationsState((), 0), None, None))
            self.plane_changed.emit()
        self.oper_widget.restore_state(state)
        self._update_operation_labels()
        self._axis_selection = current
        self.clear_marquee()
        if rect is not None:
            self.set_marquee_rect(rect)
        if ranges is not None:
            self.vb.setRange(xRange=ranges[0], yRange=ranges[1], padding=0)
        else:
            self.vb.enableAutoRange()
        self._depth_value = self._selected_coordinates.get(self._depth_name())
        if self._depth_value is None and self.volume is not None:
            depths = self.volume.axes[self._depth_name()]
            self._depth_value = float(depths[len(depths)//2]) if depths.size else None
        self._depth_index = 0
        self._update_depth_label()
        self._invalidate_volume_range()
        self._navigate()

    def _default_heatmap_axis_names(self):
        # Appearance's Swap X/Y works within whichever plane is selected.
        names = [name for name in self.param.depends_on_ if name != self._depth_name()]
        return names[1], names[0]

    def set_plot_axes_swapped(self, swapped):
        if swapped != self.plot_axes_swapped():
            self.axis_dropdown["x"].setCurrentText(self.axis_options["y"])
        return True

    def _update_large_heatmap_state(self, worker):
        # Slice workers are full-resolution private-index reads; they do not
        # use the 2D SQL spatial aggregation/reload path.
        self._large_heatmap_sql_mode = False
        self._update_heatmap_downsample_state(worker)

    def _prepare_plot_publication(self, worker, result):
        if result.slice_volume is not None:
            old_unit = getattr(self.__dict__.get("display_param"), "unit", None)
            old_state = self._applied_operation_state.entries
            old_volume = self.volume
            self.volume = result.slice_volume
            self.slice_frame = result.slice_frame
            self._displayed_result = result
            self._depth_index, self._depth_value = self.slice_frame.index, self.slice_frame.depth
            self._applied_operations = tuple(worker.operations)
            self._applied_operation_state = worker.operation_state
            if old_state != worker.operation_state.entries:
                self._invalidate_volume_range()
            elif old_volume is not self.volume:
                self._range_key = None
                self._range_complete = False
            if old_unit is not None and old_unit != result.display_param.unit:
                self._colorbar_manual_levels = None
                if self._color_mode == "locked":
                    self.color_mode.setCurrentIndex(0)
            if self._color_mode != "locked":
                self._colorbar_manual_levels = None
                self._colorbar_pending_primary = True
            self._update_navigator()

    def refreshPlot(self, finished=True, worker=None):
        worker = worker if worker is not None else self.__dict__.get("worker")
        if worker is not self.__dict__.get("worker"):
            if worker is not None:
                worker.running = False
            return
        result = getattr(worker, "result", None)
        super().refreshPlot(finished, worker=worker)
        if (finished and result is not None and self.volume is result.slice_volume
                and self.slice_frame is not None and self._can_process_refresh()
                and not getattr(worker, "_qplot_source_rejected", False)):
            self._update_plot_annotation()
            self.apply_cursor_label_widths()
            self._update_cursor_tooltip()
            position = self.__dict__.get("_last_cursor_scene_pos")
            if position is not None:
                self.mouseMoved(position)
            if self.slice_frame.values.size and not np.any(numeric_isfinite(self.slice_frame.values)):
                self.show_plot_state("No finite data in this slice", self._slice_description(), kind="empty")
            self._start_volume_range()
            self._prefetch_neighbours()
            for cut in tuple(self._cuts):
                cut.source_updated()

    def _update_navigator(self):
        frame = self.slice_frame
        assert frame is not None and self.volume is not None
        depths = self.volume.axes[self._depth_name()]
        for widget in (self.slice_slider, self.slice_index):
            with QtCore.QSignalBlocker(widget):
                widget.setRange(0, max(0, len(depths) - 1))
                widget.setValue(frame.index)
            widget.setEnabled(bool(depths.size) and not self._frozen)
        index_range = f"Slice index: 0-{len(depths) - 1}" if depths.size else "No recorded slices"
        self.slice_slider.setToolTip(index_range)
        self.slice_index.setToolTip(index_range)
        self.depth_entry.setText(self._depth_text(frame.depth, self._depth_name()))
        self.depth_entry.setToolTip(
            f"Depth: {self.param_dict[self._depth_name()].label or self._depth_name()}\n"
            f"Recorded coordinate: {frame.depth!r} {self.param_dict[self._depth_name()].unit}\n"
            "Enter a coordinate to select the nearest recorded plane.")
        self._selected_coordinates[self._depth_name()] = frame.depth
        for role in ("x", "y"):
            name = self.axis_options[role]
            values = self.volume.axes[name]
            if values.size:
                self._selected_coordinates.setdefault(name, float(values[len(values)//2]))
        filled = int(np.count_nonzero(frame.counts))
        missing = frame.counts.size - filled
        repeats = int(np.sum(np.maximum(frame.counts - 1, 0)))
        details = []
        if missing:
            details.append(f"{missing:,} {'cell' if missing == 1 else 'cells'} missing")
        if repeats:
            details.append(f"{repeats:,} repeats (finite mean)")
        if self.volume.invalid_coordinates:
            details.append(f"{self.volume.invalid_coordinates:,} samples lack finite coordinates")
        if self.volume.planned_shape:
            planned = self.volume.planned_shape[self.volume.names.index(self._depth_name())]
            details.append(f"{planned} slices planned")
        self.slice_status.setText(" · ".join(details))
        self.slice_status.setToolTip(f"{filled:,} of {frame.counts.size:,} cells contain finite measured data.")
        self._sync_volume_status()
        self._update_depth_label()

    def _sync_volume_status(self):
        range_text = self.range_status.text().lstrip(" ·")
        self.range_status.setText((" · " if self.slice_status.text() and range_text else "") + range_text)
        self.volume_status.setVisible(bool(self.slice_status.text() or self.range_status.text()))

    def _set_range_status(self, text):
        self.range_status.setText(text)
        self._sync_volume_status()

    def _invalidate_heatmap_geometry(self):
        rect = self.__dict__.get("marquee")
        super()._invalidate_heatmap_geometry()
        if rect is not None and self.slice_frame is not None:
            self.set_marquee_rect(rect)

    def _invalidate_volume_range(self):
        if self._range_task is not None:
            self._range_task.cancel()
        self._range_key = None
        self._range_complete = False
        self._shared_levels = None

    def _prefetch_neighbours(self):
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        if self.volume is None or self._closed:
            return
        volume = self.volume
        x, y = self.axis_options["x"], self.axis_options["y"]
        indices = (self._depth_index - 1, self._depth_index + 1)
        def prefetch(task):
            for index in indices:
                if 0 <= index < len(volume.depths(x, y)):
                    task.check()
                    volume.plane(x, y, index, check=task.check)
        task = VolumeTask(prefetch)
        self._prefetch_task = task
        registry = getattr(self.threadPool, "_qplot_workers", None)
        if registry is not None:
            registry.add(task)
            task.emitter.finished.connect(lambda _: registry.discard(task))
        self.threadPool.start(task, -1)

    def _stop_color_scan(self):
        if self._range_task is not None:
            self._range_task.cancel()
        self._range_timer.stop()
        self.stop_range.setVisible(False)
        self._set_range_status("Provisional range (scan stopped)")

    def _range_progress(self):
        task = self._range_task
        if task is None or not task.running:
            self._range_timer.stop()
            self.stop_range.setVisible(False)
        elif self._color_mode == "volume":
            progress = task.progress
            self._set_range_status(f"Provisional range · {progress.completed}/{progress.total or '?'} slices")

    def _color_mode_changed(self):
        mode = self.color_mode.currentData()
        self._color_mode = mode
        if mode != "volume":
            if self._range_task is not None and self._range_task.running:
                self._range_task.cancel()
                self._range_key = None
            self._range_timer.stop()
            self.stop_range.setVisible(False)
            self._set_range_status("")
        elif self._range_complete:
            self._set_range_status("")
        if mode == "locked":
            if "bar" in self.__dict__:
                self._colorbar_manual_levels = tuple(self.bar.levels())
        else:
            self._last_auto_mode = mode
            self._colorbar_manual_levels = None
            self.scaleColorbar()
        self._start_volume_range()

    def _finite_data_colorbar_range(self):
        if self._color_mode == "volume" and self._shared_levels is not None:
            return self._shared_levels
        levels = super()._finite_data_colorbar_range()
        if self._color_mode == "volume" and levels is not None:
            self._shared_levels = levels  # a stable provisional range
        return levels

    def _data_colorbar_levels(self):
        levels = super()._data_colorbar_levels()
        if levels is not None and self.__dict__.get("symmetric_color") is not None and self.symmetric_color.isChecked():
            extent = max(abs(levels[0]), abs(levels[1]))
            if extent and np.isfinite(2 * extent):
                return -extent, extent
        return levels

    def scaleColorbar(self, event=None):
        if self._color_mode == "locked":
            with QtCore.QSignalBlocker(self.color_mode):
                self.color_mode.setCurrentIndex(0 if self._last_auto_mode == "volume" else 1)
            self._color_mode = self._last_auto_mode
        super().scaleColorbar(event)

    def autoscale_color(self, event=None):
        """An explicit autoscale may tighten live limits or retry a stopped scan."""
        if self._color_mode == "volume" or self._color_mode == "locked" and self._last_auto_mode == "volume":
            self._invalidate_volume_range()
        self.scaleColorbar(event)
        self._start_volume_range()

    def setColorbarAuto(self):
        self._colorbar_manual_levels = None
        self._color_mode = self._last_auto_mode
        with QtCore.QSignalBlocker(self.color_mode):
            self.color_mode.setCurrentIndex(0 if self._color_mode == "volume" else 1)
        self.autoscale_color()

    def setColorbarManualRange(self, vmin, vmax):
        success = super().setColorbarManualRange(vmin, vmax)
        if success:
            self._set_locked_mode()
        return success

    def _colorbar_interactive_levels_finished(self, bar):
        super()._colorbar_interactive_levels_finished(bar)
        self._set_locked_mode()

    def _set_locked_mode(self):
        self._color_mode = "locked"
        with QtCore.QSignalBlocker(self.color_mode):
            self.color_mode.setCurrentIndex(2)
        self._color_mode_changed()

    def _start_volume_range(self):
        if self.volume is None or self._closed or self._color_mode != "volume":
            return
        key = (self.volume, tuple(self.axis_options.items()), self._applied_operation_state.entries)
        if self._range_key == key:
            return
        if self._range_task is not None:
            self._range_task.cancel()
        self._range_key = key
        self._range_complete = False
        self._set_range_status("Provisional colour range; scanning volume…")
        volume, axes, operations = self.volume, dict(self.axis_options), self._applied_operations
        param = self.param
        params = {role: self.param_dict[name] for role, name in axes.items()}
        def scan(task):
            lower = upper = None
            depths = volume.depths(axes["x"], axes["y"])
            for index in range(len(depths)):
                task.check()
                task.progress = PlotProgress("Calculating whole-volume colour range", index, len(depths), stage=2)
                frame = volume.plane(axes["x"], axes["y"], index, check=task.check)
                frame, _ = processed_plane(frame, operations, param, params, check=task.check, cancelled=task.is_cancelled)
                values = frame.values[numeric_isfinite(frame.values)]
                if values.size:
                    lo, hi = float(np.min(values)), float(np.max(values))
                    if not np.isfinite(lo) or not np.isfinite(hi):
                        raise ValueError("Processed volume values exceed the colour display range.")
                    lower = lo if lower is None else min(lo, lower)
                    upper = hi if upper is None else max(hi, upper)
            return (lower, upper)
        task = VolumeTask(scan)
        self._range_task = task
        self._range_timer.start()
        self.stop_range.setVisible(True)
        registry = getattr(self.threadPool, "_qplot_workers", None)
        if registry is not None:
            registry.add(task)
        def done(levels):
            if registry is not None:
                registry.discard(task)
            if task is not self._range_task or task.is_cancelled() or self._closed:
                return
            self._range_timer.stop()
            self.stop_range.setVisible(False)
            if not self._source_database_is_current() or self._range_key != key:
                return
            if levels is not None and levels[0] is not None:
                if not volume.completed and self._shared_levels is not None:
                    levels = (min(levels[0], self._shared_levels[0]), max(levels[1], self._shared_levels[1]))
                self._shared_levels = levels
                self._range_complete = True
                if self._color_mode == "volume":
                    self.scaleColorbar()
                    self._set_range_status("")
            elif levels is not None:
                self._set_range_status("No finite volume values")
        def failed(error):
            if task is self._range_task and not self._closed:
                self._set_range_status("Provisional range (scan failed)")
                self.range_status.setToolTip(str(error))
        task.emitter.finished.connect(done)
        task.emitter.error.connect(failed)
        self.threadPool.start(task, -1)

    def _slice_description(self):
        if self.slice_frame is None:
            return "3D slice"
        name = self._displayed_depth_name()
        return f"{name} = {self._depth_text(self.slice_frame.depth, name)} {self.param_dict[name].unit} · slice index {self.slice_frame.index}"

    def _displayed_depth_name(self):
        if self._displayed_result is None:
            return self._depth_name()
        displayed = {param.name for param in self._displayed_result.axis_param.values()}
        return next(name for name in self.param.depends_on_ if name not in displayed)

    def show_hover_pixel_outline(self, i, j):
        super().show_hover_pixel_outline(i, j)
        if self.slice_frame is not None:
            self._selected_coordinates[self.axis_options["x"]] = float(self.slice_frame.x[i])
            self._selected_coordinates[self.axis_options["y"]] = float(self.slice_frame.y[j])

    def _set_cursor_index_label(self, text):
        if text and self.slice_frame is not None:
            text = f"{text[:-1]},{self.slice_frame.index}]"
        super()._set_cursor_index_label(text)

    def _marquee_stats_text(self):
        text = super()._marquee_stats_text()
        selected = self._marquee_selected_data()
        count = int(np.count_nonzero(numeric_isfinite(selected))) if selected is not None else 0
        return f"{self._slice_description()}\n{count} finite cells\n{text}" if text else text

    def _add_marquee_color_context_action(self, menu):
        action = super()._add_marquee_color_context_action(menu)
        self._add_marquee_context_action(menu, "Mean within selection versus depth", lambda: self.openSweep("mean"))
        return action

    def openSweep(self, side):
        if self.volume is None or self.slice_frame is None or self.worker.running:
            return
        from qplot.tools.volume_cuts import CutSelection

        from ._volume_cut import VolumeCut
        x, y = self.axis_options["x"], self.axis_options["y"]
        if not self.volume.axes[x].size or not self.volume.axes[y].size:
            return
        x_value = self._selected_coordinates.get(x, float(self.volume.axes[x][0]))
        y_value = self._selected_coordinates.get(y, float(self.volume.axes[y][0]))
        rect = self.marquee.normalized() if side == "mean" and self.marquee is not None else None
        if side == "mean" and rect is None:
            return
        selection = CutSelection(side, x, y, self._depth_name(), x_value, y_value,
                                 self.slice_frame.depth,
                                 None if rect is None else (rect.left(), rect.right(), rect.top(), rect.bottom()))
        self.open_subplot.emit(VolumeCut, self._dataset_key, (self.param, self, selection))

    def move_sweep_with_arrow_key(self, key):
        cut = self.__dict__.get("_active_volume_cut")
        if cut is not None and cut in self._cuts:
            cut.move_marker(key)

    def _capture_heatmap_csv(self):
        capture = super()._capture_heatmap_csv()
        metadata = self._slice_export_metadata()
        return replace(capture, constants=metadata)

    def _slice_export_metadata(self):
        if self.slice_frame is None or self.volume is None:
            raise ValueError("No slice is ready to export.")
        name = self._displayed_depth_name()
        return ((f"{name} ({self.param_dict[name].unit})", self.slice_frame.depth),
                ("slice_index", self.slice_frame.index), ("database", self._dataset_key.database_path),
                ("run_guid", self._guid), ("source_revision", self.volume.revision),
                ("slice_operations", str(self._applied_operation_state.entries)),
                ("x_unit", self.axis_param["x"].unit), ("y_unit", self.axis_param["y"].unit),
                ("value_unit", self.display_param.unit),
                ("repeated_coordinate_policy", "finite mean"))

    def freeze_slice(self):
        if self.slice_frame is None or self.worker.running:
            return
        from ._volume_cut import FrozenSlice
        self.open_subplot.emit(FrozenSlice, self._dataset_key, (self.param, self))

    def accepts_preview_trace_drop(self, payload):
        return False

    def closeEvent(self, event):
        if self._range_task is not None:
            self._range_task.cancel()
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        if self.__dict__.get("_range_timer") is not None:
            self._range_timer.stop()
        super().closeEvent(event)
        if not self._merged_trace_users:
            self._release_volume()

    def _release_volume(self):
        for name in ("worker", "volume", "slice_frame", "_displayed_result",
                     "_range_task", "_range_key", "_prefetch_task"):
            self.__dict__[name] = None
