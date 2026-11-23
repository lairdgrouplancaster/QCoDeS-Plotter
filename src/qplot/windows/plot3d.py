"""A coordinate-preserving 3D slice viewer built on the normal heatmap UI."""

from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.numeric_samples import numeric_isfinite
from qplot.datahandling.plot_progress import PlotProgress
from qplot.tools.plot_csv import HeatmapCsv
from qplot.tools.volume_worker import VolumeLoader, VolumeTask, processed_plane

from ._commands import command_spec, create_action
from ._widgets.operations import OperationsState
from .plot2d import plot2d


class plot3d(plot2d):
    """One volume and one independently selectable, processed viewing plane."""

    cursor_has_depth = True
    # Slice workers read full-resolution planes from their private index.
    _uses_heatmap_viewport = False
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
        self.setMinimumHeight(560)
        self.addToolBarBreak(QtCore.Qt.ToolBarArea.TopToolBarArea)
        self.slice_toolbar = qtw.QToolBar("3D slice", self)
        self.slice_toolbar.setObjectName("sliceControls")
        self.slice_toolbar.setMovable(True)
        self.slice_toolbar.setAllowedAreas(
            QtCore.Qt.ToolBarArea.TopToolBarArea | QtCore.Qt.ToolBarArea.BottomToolBarArea)
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, self.slice_toolbar)
        self.slice_panel = qtw.QGroupBox()
        self.slice_panel.setAccessibleName("3D slice controls")
        self.slice_panel.setObjectName("slicePanel")
        self.slice_panel.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        panel_layout = qtw.QVBoxLayout(self.slice_panel)
        panel_layout.setContentsMargins(8, 6, 8, 6)
        panel_layout.setSpacing(4)
        controls = qtw.QGridLayout()
        controls.setHorizontalSpacing(6)
        controls.setVerticalSpacing(4)
        for column in (1, 3):
            controls.addItem(qtw.QSpacerItem(
                20, 0, qtw.QSizePolicy.Policy.Preferred, qtw.QSizePolicy.Policy.Minimum,
            ), 0, column, 2, 1)
        controls.setColumnStretch(4, 1)
        panel_layout.addLayout(controls)
        axis_controls = qtw.QGridLayout()
        axis_controls.setHorizontalSpacing(6)
        axis_controls.setVerticalSpacing(4)
        controls.addLayout(axis_controls, 0, 0, 2, 1)
        self.slice_toolbar.addWidget(self.slice_panel)
        for row, axis in enumerate(("x", "y")):
            self.axis_dropdown[axis].setMinimumContentsLength(12)
            self.axis_dropdown[axis].setMinimumWidth(85)
            self.axis_dropdown[axis].setSizeAdjustPolicy(
                qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            axis_controls.addWidget(qtw.QLabel(f"{axis.upper()}:"), row, 0)
            axis_controls.addWidget(self.axis_dropdown[axis], row, 1)
        self.depth_label = qtw.QLabel()
        self.follow_latest = qtw.QCheckBox("Follow latest")
        self.follow_latest.setToolTip("Follow the last acquired depth coordinate; manual navigation turns this off.")
        self.follow_latest.toggled.connect(self._follow_latest_changed)
        self.color_mode = qtw.QComboBox()
        self.color_mode.setMinimumContentsLength(10)
        self.color_mode.setMinimumWidth(85)
        self.color_mode.setSizeAdjustPolicy(
            qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for label, mode in (("Whole volume", "volume"), ("Current slice", "slice"), ("Manual", "locked")):
            self.color_mode.addItem(label, mode)
        self.color_mode.setToolTip("Use the whole volume or current slice for automatic colour limits. "
                                   "Manual keeps the chosen limits.")
        self.color_mode.currentIndexChanged.connect(self._color_mode_changed)
        color_row = qtw.QHBoxLayout()
        color_row.setSpacing(6)
        controls.addLayout(color_row, 0, 2, 2, 1, QtCore.Qt.AlignmentFlag.AlignTop)
        color_row.addWidget(qtw.QLabel("Colour:"))
        color_row.addWidget(self.color_mode)
        self.navigator = qtw.QGridLayout()
        self.navigator.setHorizontalSpacing(6)
        self.navigator.setVerticalSpacing(4)
        self.navigator.addItem(qtw.QSpacerItem(
            12, 0, qtw.QSizePolicy.Policy.Preferred, qtw.QSizePolicy.Policy.Minimum,
        ), 0, 3, 2, 1)
        self.navigator.setColumnStretch(4, 1)
        controls.addLayout(self.navigator, 0, 4, 2, 1)
        self.slice_slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slice_slider.setMinimumWidth(60)
        self.slice_slider.setTracking(True)
        self.slice_slider.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.slice_slider.setToolTip("Slice index")
        self.slice_slider.sliderPressed.connect(
            lambda: self.slice_slider.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor))
        self.slice_slider.sliderReleased.connect(
            lambda: self.slice_slider.setCursor(QtCore.Qt.CursorShape.OpenHandCursor))
        self.slice_slider.valueChanged.connect(self.select_slice)
        self.navigator.addWidget(self.slice_slider, 0, 4)
        label_alignment = QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        self.navigator.addWidget(qtw.QLabel("Slice:"), 0, 0, label_alignment)
        self.slice_index = qtw.QSpinBox()
        self.slice_index.setToolTip("Slice index")
        self.slice_index.setKeyboardTracking(False)
        self.slice_index.valueChanged.connect(self.select_slice)
        self.slice_index.setMaximumWidth(120)
        self.navigator.addWidget(self.slice_index, 0, 1)
        self.navigator.addWidget(self.depth_label, 1, 0, label_alignment)
        self.depth_entry = qtw.QLineEdit()
        self.depth_entry.setMinimumWidth(85)
        self.depth_entry.setMaximumWidth(120)
        self.depth_entry.setToolTip("Enter a coordinate; select the nearest recorded plane.")
        self.depth_entry.returnPressed.connect(self._select_coordinate)
        self.navigator.addWidget(self.depth_entry, 1, 1)
        self.depth_unit = qtw.QLabel()
        self.navigator.addWidget(self.depth_unit, 1, 2)
        self.navigator.addWidget(self.follow_latest, 1, 4)
        self.volume_status = qtw.QToolBar("Volume status", self.slice_panel)
        self.volume_status.setObjectName("volumeStatus")
        self.volume_status.setMovable(False)
        self.volume_status.setStyleSheet("QToolBar#volumeStatus { border: none; padding: 0; }")
        panel_layout.addWidget(self.volume_status)
        self.slice_status = qtw.QLabel(" Waiting for coordinates")
        self.volume_status.addWidget(self.slice_status)
        self.range_status = qtw.QLabel()
        self.range_status.setToolTip("Progress of the scan used to determine colour limits across the volume.")
        self.range_status.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        self.volume_status.addWidget(self.range_status)
        self.stop_range = QtGui.QAction("Stop colour scan", self)
        self.stop_range.setToolTip("Cancel the background scan for whole-volume colour limits.")
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

    def _update_depth_readout(self):
        """The displayed slice coordinate is known independently of the cursor."""
        depth_label = self.pos_labels.get("depth")
        if depth_label is None:
            return
        visible = not self.pos_labels["z"].isHidden()
        depth_label.setVisible(visible)
        self._cursor_depth_separator.setVisible(visible)
        value = self.slice_frame.depth if self.slice_frame is not None else None
        depth_label.setText(self.format_cursor_number(value)
                           if visible and value is not None else "")

    def mouseMoved(self, pos):
        super().mouseMoved(pos)
        self._update_depth_readout()
        inside = self.plot.sceneBoundingRect().contains(pos)
        if inside:
            for role in ("x", "y", "depth", "z"):
                label = self.pos_labels[role]
                if not label.isHidden() and not label.text().strip():
                    label.setText("—")
        self._update_cursor_tooltip()

    def initContextMenu(self):
        super().initContextMenu()
        auto_color = self._context_menu_action("Autoscale Colour")
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
        other = "y" if key == "x" else "x"
        if self.axis_dropdown[key].currentText() == self.axis_dropdown[other].currentText():
            with QtCore.QSignalBlocker(self.axis_dropdown[other]):
                self.axis_dropdown[other].setCurrentText(previous[key])
        current = self.axis_options
        pair = (current["x"], current["y"])
        if pair == old_pair:
            return
        with self._transposing_axis_scales() if pair == old_pair[::-1] else nullcontext():
            self._change_slice_plane(old_pair, pair)

    def _change_slice_plane(self, old_pair, pair):
        # Either orientation must restore the last edits to this physical plane.
        view = self.vb.viewRange()
        rect = self.marquee
        self._plane_states[old_pair] = (self.oper_widget.capture_state(), rect, view)
        transposed_rect = None if rect is None else QtCore.QRectF(rect.y(), rect.x(), rect.height(), rect.width())
        self._plane_states[old_pair[::-1]] = (
            self.oper_widget.capture_state(transpose=True), transposed_rect, view[::-1],
        )
        state, rect, ranges = self._plane_states.get(pair, (OperationsState((), 0), None, None))
        if pair != old_pair[::-1]:
            self.plane_changed.emit()
        self.oper_widget.restore_state(state)
        self._update_operation_labels()
        self._axis_selection = self.axis_options
        self.clear_marquee()
        if rect is not None:
            # The previous plane's grid is still displayed until navigation completes.
            self._draw_marquee_rect(rect)
        if pair == old_pair[::-1]:
            self._transpose_heatmap_axis_assignments()
            self._transpose_trace_axis_assignments()
        elif ranges is not None:
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
            self._update_navigator()

    def _complete_plot_publication(self, worker):
        result = worker.result
        if (self.volume is not result.slice_volume or self.slice_frame is None
                or not self._can_process_refresh()):
            return
        # Finish the slice's dependent views while rollback state is retained.
        for update in (self._update_plot_annotation, self.apply_cursor_label_widths,
                       self._update_depth_readout, self._update_cursor_tooltip):
            if not self._publication_step(worker, update):
                return
        position = self.__dict__.get("_last_cursor_scene_pos")
        if position is not None:
            if not self._publication_step(worker, lambda: self.mouseMoved(position)):
                return
        if self.slice_frame.values.size and not np.any(numeric_isfinite(self.slice_frame.values)):
            if not self._publication_step(worker, lambda: self.show_plot_state(
                    "No finite data in this slice", self._slice_description(), kind="empty")):
                return
        for update in (self._start_volume_range, self._prefetch_neighbours):
            if not self._publication_step(worker, update):
                return
        for cut in tuple(self._cuts):
            if not self._publication_step(worker, cut.source_updated):
                return

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
        elif self._effective_colour_scope() == "volume":
            progress = task.progress
            self._set_range_status(f"Provisional range · {progress.completed}/{progress.total or '?'} slices")

    def _create_colour_source_controls(self) -> qtw.QWidget:
        row = qtw.QWidget()
        layout = qtw.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self.colour_source_buttons = {}
        for scope in ("slice", "volume"):
            button = qtw.QRadioButton(self.color_mode.itemText(self.color_mode.findData(scope)), row)
            button.setChecked(scope == self._effective_colour_scope())
            button.clicked.connect(partial(self._colour_scope_changed, scope))
            layout.addWidget(button)
            self.colour_source_buttons[scope] = button
        layout.addStretch()
        row.setFocusProxy(self.colour_source_buttons[self._effective_colour_scope()])
        return row

    def _colour_scope_changed(self, scope):
        if scope == self._effective_colour_scope():
            return
        if self._color_mode == "locked":
            self._last_auto_mode = scope
            self._color_mode_changed()
        else:
            self.color_mode.setCurrentIndex(self.color_mode.findData(scope))

    def _sync_color_mode_controls(self):
        entry = self.__dict__.get("colorbar_range_entry")
        if entry is not None:
            button = self.colour_source_buttons[self._effective_colour_scope()]
            button.setChecked(True)
            self._sync_colour_filter_controls()
            self._sync_colorbar_scale_mode()

    def _colour_filters_enabled(self) -> bool:
        return self._effective_colour_scope() == "slice"

    def _effective_colour_scope(self):
        return self._last_auto_mode if self._color_mode == "locked" else self._color_mode

    def _colour_fit_options(self):
        if self._effective_colour_scope() == "volume":
            return False, 100
        return super()._colour_fit_options()

    def _capture_colour_range_settings(self):
        return super()._capture_colour_range_settings(), self._color_mode, self._last_auto_mode

    def _restore_colour_range_settings(self, saved):
        settings, self._color_mode, self._last_auto_mode = saved
        with QtCore.QSignalBlocker(self.color_mode):
            self.color_mode.setCurrentIndex(self.color_mode.findData(self._color_mode))
        self._sync_color_mode_controls()
        super()._restore_colour_range_settings(settings)
        self._start_volume_range()

    def _apply_colour_range_state(self, state):
        if state.automatic and self._color_mode == "locked":
            self._color_mode = self._last_auto_mode
            with QtCore.QSignalBlocker(self.color_mode):
                self.color_mode.setCurrentIndex(self.color_mode.findData(self._color_mode))
        super()._apply_colour_range_state(state)
        self._sync_color_mode_controls()
        self._start_volume_range()

    def _color_mode_changed(self):
        mode = self.color_mode.currentData()
        self._color_mode = mode
        if self._effective_colour_scope() != "volume":
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
            self._sync_color_mode_controls()
            self.scaleColorbar()
        self._start_volume_range()
        self._sync_color_mode_controls()

    def _sync_heatmap_colorbar_items(self, *, rescale=False):
        super()._sync_heatmap_colorbar_items(rescale=rescale or self._color_mode != "locked")

    def _finite_data_colorbar_range(self):
        levels = super()._finite_data_colorbar_range()
        if self._effective_colour_scope() != "volume" or not self._heatmap_is_visible(self):
            return levels
        if self._shared_levels is None:
            # Keep the provisional volume range independent of overlays so
            # removing an overlay can shrink the shared colour scale again.
            data = self.__dict__.get("dataGrid")
            if data is not None:
                values = np.asarray(data, dtype=float)
                finite = values[np.isfinite(values)]
                if finite.size:
                    self._shared_levels = float(finite.min()), float(finite.max())
        if self._shared_levels is None:
            return levels
        if levels is None:
            return self._shared_levels
        return min(levels[0], self._shared_levels[0]), max(levels[1], self._shared_levels[1])

    def scaleColorbar(self, event=None):
        if self._color_mode == "locked":
            with QtCore.QSignalBlocker(self.color_mode):
                self.color_mode.setCurrentIndex(0 if self._last_auto_mode == "volume" else 1)
            self._color_mode = self._last_auto_mode
        super().scaleColorbar(event)
        self._sync_color_mode_controls()

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
        if self.volume is None or self._closed or self._effective_colour_scope() != "volume":
            return
        key = (self.volume, tuple(self.axis_options.items()), self._applied_operation_state.entries,
               self.worker.complex_mode)
        if self._range_key == key:
            return
        if self._range_task is not None:
            self._range_task.cancel()
        self._range_key = key
        self._range_complete = False
        self._set_range_status("Provisional colour range; scanning volume…")
        volume, axes, operations = self.volume, dict(self.axis_options), self._applied_operations
        param = self.param
        complex_mode = self.worker.complex_mode
        params = {role: self.param_dict[name] for role, name in axes.items()}
        def scan(task):
            lower = upper = None
            depths = volume.depths(axes["x"], axes["y"])
            for index in range(len(depths)):
                task.check()
                task.progress = PlotProgress("Calculating whole-volume colour range", index, len(depths), stage=2)
                frame = volume.plane(axes["x"], axes["y"], index, check=task.check)
                frame, _ = processed_plane(frame, operations, param, params, check=task.check,
                                          cancelled=task.is_cancelled, complex_mode=complex_mode)
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
        return f"Slice: {self._slice_description()}\n{text}" if text else text

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

    def _move_keyboard_selected_cut(self, key):
        self.move_sweep_with_arrow_key(key)

    def activate_volume_cut(self, cut):
        self._active_volume_cut = cut
        for candidate in self._cuts:
            if candidate.marker is not None:
                width = 3 if candidate is cut else (1 if candidate.selection.kind == "d" else 2)
                candidate.marker.setPen(pg.mkPen(candidate.cut_color, width=width))
        self.widget.setFocus()

    def cycle_selected_cut(self, backwards=False):
        cuts = sorted(
            (cut for cut in self._cuts if not cut._closed
             and cut.marker is not None and cut.marker.isVisible()),
            key=lambda cut: cut.cut_id,
        )
        if not cuts:
            return
        selected = self.__dict__.get("_active_volume_cut")
        if selected in cuts:
            index = (cuts.index(selected) + (-1 if backwards else 1)) % len(cuts)
        else:
            index = -1 if backwards else 0
        self.activate_volume_cut(cuts[index])

    def handle_sweep_line_double_click(self, event):
        cuts = [cut for cut in self._cuts if not cut._closed and cut.marker is not None]
        marker = self._probe_at_scene_position([cut.marker for cut in cuts], event.scenePos())
        if marker is None:
            return False
        event.accept()
        selected = next(cut for cut in cuts if cut.marker is marker)
        closing = tuple(self._cuts) if self.sweep_line_remove_all_requested(event) else (selected,)
        for cut in closing:
            cut.close()
        return True

    def _capture_heatmap_csv(self, layer: Any = None) -> HeatmapCsv:
        capture = super()._capture_heatmap_csv(layer)
        if layer is not None and layer is not self:
            return capture
        metadata = self._slice_export_metadata(units=self._heatmap_csv_units())
        return replace(capture, constants=metadata)

    def _slice_export_metadata(self, *, units=()):
        """Capture the published plane's provenance. Detached plots supply their own units."""
        if self.slice_frame is None or self.volume is None:
            raise ValueError("No slice is ready to export.")
        name = self._displayed_depth_name()
        return ((f"{name} ({self.param_dict[name].unit})", self.slice_frame.depth),
                ("slice_index", self.slice_frame.index), ("database", self._dataset_key.database_path),
                ("run_guid", self._guid), ("source_revision", self.volume.revision),
                ("slice_operations", str(self._applied_operation_state.entries)),
                *units, ("repeated_coordinate_policy", "finite mean"))

    def freeze_slice(self):
        if self.slice_frame is None or self.worker.running:
            return
        from ._volume_cut import FrozenSlice
        self.open_subplot.emit(FrozenSlice, self._dataset_key, (self.param, self))

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
