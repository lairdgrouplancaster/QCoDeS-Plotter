"""Live coordinate-bound volume cuts and independent frozen 2D snapshots."""

from copy import copy
from dataclasses import replace
from html import escape

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from qcodes.dataset.descriptions.param_spec import ParamSpec

from qplot.datahandling.complex_data import ComplexSamples, contains_complex
from qplot.datahandling.plot_progress import PlotProgress
from qplot.tools.plot_csv import CsvColumns
from qplot.tools.plot_result import capture_plot_result
from qplot.tools.volume_cuts import extract_cut
from qplot.tools.worker import loader

from ._source_consumer import SourceConsumerRegistration
from ._widgets import CopyableTableWidget
from .plot1d import plot1d
from .plot2d import plot2d


class CutLoader(loader):
    def __init__(self, *args, volume, selection, slice_operations, source_param,
                 source_params, source_complex_mode="magnitude", **kwargs):
        kwargs["read_data"] = False
        super().__init__(*args, **kwargs)
        self.volume = volume
        self.selection = selection
        self.slice_operations = slice_operations
        self.source_param = source_param
        self.source_params = source_params
        self.source_complex_mode = source_complex_mode
        self.cut_description = ""
        self.cut_metadata: tuple[tuple[str, object], ...] = ()

    def _run(self):
        try:
            self._check_cancelled()
            self._require_expected_source_current()
            x, y, counts, value_param = extract_cut(
                self.volume, self.selection, self.slice_operations, self.source_param, self.source_params,
                check=self._check_cancelled, cancelled=self.is_cancelled,
                complex_mode=self.source_complex_mode,
                progress=lambda n, total: setattr(self, "_progress", PlotProgress(
                    "Extracting cut through slices", n, total, stage=2)),
            )
            self.axis_data = {"x": x, "y": y}
            if getattr(self.source_param, "type", None) == "complex":
                raw_x, raw_values, _, _ = extract_cut(
                    self.volume, self.selection, (), self.source_param, self.source_params,
                    check=self._check_cancelled, cancelled=self.is_cancelled, complex_mode=None,
                )
                if contains_complex(raw_values):
                    self.complex_samples = ComplexSamples(
                        (raw_x,), (self.source_params[self.selection.varying],),
                        raw_values, self.source_complex_mode,
                    )
            self.axis_param = {"x": self.source_params[self.selection.varying], "y": copy(value_param)}
            self.display_param = copy(value_param)
            operated = self.do_operations()
            if operated is not None:
                self.axis_data = {"x": operated[0], "y": operated[1]}
                self._apply_operation_metadata()
            self.dataset_completed = self.volume.completed
            self.loaded_from_sql_heatmap = True
            self.dataset_length_at_start = self.volume.revision
            self.loaded_point_count = len(x)
            self._check_cancelled()
            self._require_expected_source_current()
            self.counts = counts
            self._emit_finished(True, replace(capture_plot_result(self), slice_volume=self.volume))
        except Exception as error:
            if self.is_cancelled():
                self._finish_cancelled()
            else:
                self.emitter.errorOccurred.emit(error)
                self._emit_finished(False)


class VolumeCut(plot1d):
    _extra_publication_fields = ("_displayed_cut_metadata", "_displayed_description")

    def __init__(self, dataset_key, param, source, selection, config, threadPool,
                 dataset_holder, **kwargs):
        self.source = source
        self.source_param = param
        self.selection = selection
        self.volume = source.volume
        self.slice_operations = source._applied_operations
        self.slice_operation_state = source._applied_operation_state
        self._registration = SourceConsumerRegistration(source)
        self._source_signature = None
        self._displayed_cut_metadata = ()
        self._displayed_description = None
        self.marker = None
        self.depth_marker = None
        synthetic = ParamSpec(param.name, "numeric", label=param.label, unit=param.unit,
                              depends_on=[selection.varying])
        super().__init__(dataset_key, synthetic, config, threadPool, dataset_holder, **kwargs)
        self.cut_id = source._reserve_sweep_id()
        self.cut_color = pg.intColor(self.cut_id, hues=12)
        self._plot_source_info = (dataset_key.database_path, self.ds.run_id, f"{param.label or param.name} · cut {self.cut_id + 1}")

    def initAxes(self):
        super().initAxes()
        self.axes_dock.hide()
        self.axes_dock.toggleViewAction().setVisible(False)

    def change_axis(self, key):
        # A cut's named varying coordinate is fixed by its definition.
        with QtCore.QSignalBlocker(self.axis_dropdown[key]):
            self.axis_dropdown[key].setCurrentText(self._axis_selection[key])

    def initFrame(self):
        super().initFrame()
        self.setMinimumSize(600, 420)
        self.line.setData(connect="finite")
        self.line.setPen(pg.mkPen(self.cut_color, width=2))
        self.cut_toolbar = qtw.QToolBar("Cut controls", self)
        self.addToolBarBreak()
        self.addToolBar(self.cut_toolbar)
        self.raw_data = qtw.QCheckBox("Raw data")
        self.raw_data.setToolTip("Use measured values before the source slice operations are applied.")
        self.raw_data.toggled.connect(lambda _: self._reload_cut())
        self.cut_toolbar.addWidget(self.raw_data)
        self.pin_slice = qtw.QCheckBox("Pin to this slice")
        self.pin_slice.setToolTip("Keep this cut at the current depth when navigating the source volume.")
        self.pin_slice.toggled.connect(lambda _: self.source_updated())
        pin_action = self.cut_toolbar.addWidget(self.pin_slice)
        assert pin_action is not None
        pin_action.setVisible(self.selection.kind in ("h", "v"))
        self.position_controls = {}
        names = [self.selection.y] if self.selection.kind == "h" else [self.selection.x] if self.selection.kind == "v" else [self.selection.x, self.selection.y]
        if self.selection.kind == "mean":
            names = []
        for name in names:
            self.cut_toolbar.addWidget(qtw.QLabel(f" {name}: "))
            entry = qtw.QLineEdit()
            entry.setToolTip("Enter the fixed coordinate for this cut, then press Return.")
            entry.setMaximumWidth(120)
            entry.setText(repr(self.selection.x_value if name == self.selection.x else self.selection.y_value))
            entry.returnPressed.connect(lambda name=name: self._position_entered(name))
            self.cut_toolbar.addWidget(entry)
            self.position_controls[name] = entry
        self.coverage_label = qtw.QLabel()
        self.coverage_label.setToolTip("Coverage of measured data contributing to this cut.")
        self.cut_toolbar.addWidget(self.coverage_label)
        self.coverage_action = QtGui.QAction("Coverage…", self)
        self.coverage_action.setToolTip("Show how many measured cells contribute at each depth.")
        self.coverage_action.triggered.connect(self.show_coverage)
        self.cut_toolbar.addAction(self.coverage_action)
        self.coverage_action.setVisible(self.selection.kind == "mean")
        self._registration.register(self)
        self.source._cuts.append(self)
        self.source._active_volume_cut = self
        self.source.plane_changed.connect(self._plane_changed)
        self._create_marker()

    def _create_marker(self):
        s = self.selection
        if s.kind in ("h", "v"):
            self.marker = pg.InfiniteLine(pos=s.y_value if s.kind == "h" else s.x_value,
                                          angle=0 if s.kind == "h" else 90, movable=True,
                                          pen=pg.mkPen(self.cut_color, width=2),
                                          label=str(self.cut_id + 1))
            self.marker.sigPositionChangeFinished.connect(self._marker_moved)
        elif s.kind == "d":
            self.marker = pg.TargetItem(pos=(s.x_value, s.y_value), movable=True,
                                        pen=pg.mkPen(self.cut_color), label=str(self.cut_id + 1))
            self.marker.sigPositionChangeFinished.connect(self._marker_moved)
        if self.marker is not None:
            self.source._add_plot_overlay(self.marker)
        if s.kind in ("d", "mean"):
            self.depth_marker = pg.InfiniteLine(pos=s.depth_value or 0, movable=True,
                                               pen=pg.mkPen(self.cut_color, style=QtCore.Qt.PenStyle.DashLine))
            self._add_plot_overlay(self.depth_marker)
            self.depth_marker.sigPositionChangeFinished.connect(self._depth_moved)
            self.plot.scene().sigMouseClicked.connect(self._curve_clicked)

    def _position_entered(self, name):
        try:
            value = float(self.position_controls[name].text())
            if not np.isfinite(value):
                raise ValueError()
        except ValueError:
            self.show_status("Enter a finite coordinate.", 5000)
            return
        index = self.volume.nearest_index(name, value)
        values = self.volume.axes[name]
        if values.size:
            self.selection = replace(self.selection, **{
                "x_value" if name == self.selection.x else "y_value": float(values[index])})
            self._sync_marker()
            self._reload_cut()

    def _marker_moved(self):
        if self.marker is None:
            return
        self.source._active_volume_cut = self
        s = self.selection
        if s.kind == "d":
            positions = {s.x: self.marker.pos().x(), s.y: self.marker.pos().y()}
        else:
            positions = {s.y if s.kind == "h" else s.x: self.marker.value()}
        changes = {}
        for name, value in positions.items():
            values = self.volume.axes[name]
            index = self.volume.nearest_index(name, value)
            if values.size:
                changes["x_value" if name == s.x else "y_value"] = float(values[index])
        self.selection = replace(s, **changes)
        self._sync_marker()
        self._reload_cut()

    def _sync_marker(self):
        s = self.selection
        for name, entry in self.position_controls.items():
            entry.setText(repr(s.x_value if name == s.x else s.y_value))
        if self.marker is None:
            return
        same_plane = self.source.axis_options == {"x": s.x, "y": s.y}
        visible = same_plane and (s.kind == "d" or self.source._depth_value == s.depth_value)
        self.marker.setVisible(visible and not self._closed)
        with QtCore.QSignalBlocker(self.marker):
            if s.kind == "d":
                self.marker.setPos(s.x_value, s.y_value)
            else:
                self.marker.setValue(s.y_value if s.kind == "h" else s.x_value)

    def move_marker(self, key):
        if self.marker is None or not self.marker.isVisible() or self._closed:
            return False
        s = self.selection
        horizontal = key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right)
        if s.kind == "h" and horizontal or s.kind == "v" and not horizontal:
            return False
        name = s.x if horizontal else s.y
        values = self.volume.axes[name]
        value = s.x_value if horizontal else s.y_value
        delta = -1 if key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Down) else 1
        index = min(max(0, self.volume.nearest_index(name, value) + delta), len(values) - 1)
        self.selection = replace(s, **{"x_value" if horizontal else "y_value": float(values[index])})
        self._sync_marker()
        self._reload_cut()
        return True

    def _depth_moved(self):
        if self.depth_marker is None:
            return
        if (not self.line.opts.get("fftMode") and not self.line.opts.get("phasemapMode")
                and self.source._depth_name() == self.selection.depth and not self.source._closed):
            self.source.select_slice(self.volume.nearest_index(self.selection.depth, self.depth_marker.value()))

    def _curve_clicked(self, event):
        if self.depth_marker is None:
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.plot.sceneBoundingRect().contains(event.scenePos()):
            point = self.vb.mapSceneToView(event.scenePos())
            self.depth_marker.setValue(point.x())
            self._depth_moved()

    def _plane_changed(self):
        if self.selection.kind in ("h", "v"):
            self.pin_slice.setChecked(True)

    def source_updated(self):
        if self._closed or self.source.volume is None:
            return
        self.volume = self.source.volume
        s = self.selection
        if self.source.axis_options == {"x": s.y, "y": s.x}:
            self.selection = replace(s, x=s.y, y=s.x, x_value=s.y_value, y_value=s.x_value,
                                     kind={"h": "v", "v": "h"}.get(s.kind, s.kind),
                                     bounds=None if s.bounds is None else (*s.bounds[2:], *s.bounds[:2]))
            if self.marker is not None and s.kind in ("h", "v"):
                self.marker.setAngle(90 if self.selection.kind == "v" else 0)
        same_plane = self.source.axis_options == {"x": self.selection.x, "y": self.selection.y}
        if same_plane:
            self.slice_operations = self.source._applied_operations
            self.slice_operation_state = self.source._applied_operation_state
            if self.selection.kind in ("h", "v") and not self.pin_slice.isChecked():
                self.selection = replace(self.selection, depth_value=self.source._depth_value)
        if self.depth_marker is not None and self.source._depth_name() == self.selection.depth:
            with QtCore.QSignalBlocker(self.depth_marker):
                self.depth_marker.setValue(self.source._depth_value or 0)
        self._sync_marker()
        signature = (self.volume, self.selection, self.slice_operation_state.entries,
                     self.source.worker.complex_mode)
        if signature != self._source_signature:
            self._source_signature = signature
            self._reload_cut()

    def _reload_cut(self):
        if not self._plot_setup_complete or self._closed:
            return
        if (worker := self.__dict__.get("worker")) is not None and worker.running:
            worker.cancel()
        self.load_data()

    def _create_plot_loader(self, *args, **kwargs):
        worker = CutLoader(*args, volume=self.volume, selection=self.selection,
                           slice_operations=() if self.raw_data.isChecked() else self.slice_operations,
                           source_param=self.source_param, source_params=self.source.param_dict,
                           source_complex_mode=self.source.worker.complex_mode, **kwargs)
        worker.cut_description = self._requested_description()
        worker.cut_metadata = (
            ("cut", worker.cut_description), ("database", self._dataset_key.database_path),
            ("run_guid", self._guid), ("source_revision", self.volume.revision),
            ("slice_operations", "raw" if self.raw_data.isChecked() else str(self.slice_operation_state.entries)),
            ("cut_operations", str(self.oper_widget.capture_state().entries)),
        )
        return worker

    def _prepare_plot_publication(self, worker, result):
        self._displayed_cut_metadata = worker.cut_metadata
        self._displayed_description = worker.cut_description

    def refreshWindow(self, force=False):
        if not self._plot_setup_complete or self._closed:
            return
        if force:
            self._reload_cut()
        # The retained parent owns the acquisition clock. Each accepted parent
        # revision calls source_updated; cut navigation never opens a database.

    def _ensure_refresh_monitor(self):
        self.monitor.stop()

    def refreshPlot(self, finished=True, worker=None):
        worker = worker if worker is not None else self.__dict__.get("worker")
        super().refreshPlot(finished, worker=worker)
        if finished and worker is not None and worker is self.__dict__.get("worker") and getattr(worker, "result", None) is not None:
            self._update_plot_annotation()
            counts = getattr(worker, "counts", np.array([]))
            self._coverage_counts = counts.copy()
            self._coverage_depths = worker.volume.axes[worker.selection.depth].copy()
            self.coverage_label.setText(f" Valid cells per depth: {counts.min()}–{counts.max()}" if self.selection.kind == "mean" and counts.size else "")
        self.monitor.stop()

    def _requested_description(self):
        s = self.selection
        if s.kind == "mean":
            fixed = f"{s.x}, {s.y} region {s.bounds}"
        else:
            fixed = ", ".join(f"{name}={value} {self.source.param_dict[name].unit}" for name, value in
                              ((s.x, s.x_value), (s.y, s.y_value), (s.depth, s.depth_value)) if name != s.varying)
        return f"Cut {self.cut_id + 1}: {fixed} · {'raw' if self.raw_data.isChecked() else 'processed slices'}"

    def _update_plot_annotation(self):
        super()._update_plot_annotation()
        if self.__dict__.get("raw_data") is not None:
            _append_annotation(self, self._displayed_description or self._requested_description())

    def _capture_line_csv(self, exporter, *, transformed=False):
        capture = super()._capture_line_csv(exporter, transformed=transformed)
        if (self.selection.kind == "mean" and isinstance(capture, CsvColumns)
                and len(capture.columns) == 2
                and np.array_equal(capture.columns[0], self.__dict__.get("_coverage_depths", []))):
            capture = replace(capture, headers=(*capture.headers, "finite_cells"),
                              columns=(*capture.columns, self._coverage_counts.copy()))
        return replace(capture, constants=(*self._displayed_cut_metadata,
                                          ("x_unit", self.axis_param["x"].unit),
                                          ("value_unit", self.display_param.unit)))

    def show_coverage(self):
        if "_coverage_counts" not in self.__dict__:
            return
        dialog = qtw.QDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Mean-cut coverage by depth")
        layout = qtw.QVBoxLayout(dialog)
        table = CopyableTableWidget(len(self._coverage_counts), 2)
        table.setHorizontalHeaderLabels([self.selection.depth, "Finite cells"])
        table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, (depth, count) in enumerate(zip(self._coverage_depths, self._coverage_counts, strict=True)):
            table.setItem(row, 0, qtw.QTableWidgetItem(str(depth)))
            table.setItem(row, 1, qtw.QTableWidgetItem(str(count)))
        layout.addWidget(table)
        dialog.resize(400, 360)
        dialog.show()

    def _native_transform_labels_changed(self, _value=None):
        super()._native_transform_labels_changed(_value)
        if self.depth_marker is not None:
            self.depth_marker.setVisible(not self.line.opts.get("fftMode") and not self.line.opts.get("phasemapMode"))

    def closeEvent(self, event):
        if self in self.source._cuts:
            self.source._cuts.remove(self)
        if self.marker is not None:
            self.source.plot.removeItem(self.marker)
            self.marker = None
        remaining = self._registration.release()
        try:
            self.source.plane_changed.disconnect(self._plane_changed)
        except (TypeError, RuntimeError):
            pass
        if remaining == 0 and self.source._closed:
            self.source.monitor.stop()
            if (worker := self.source.__dict__.get("worker")) is not None:
                worker.cancel()
            self.source._release_volume()
        super().closeEvent(event)
        self.volume = None
        self.worker = None


def _append_annotation(window, text):
    label = window.__dict__.get("source_annotation")
    if label is not None:
        window._set_plot_annotation_visible(True)
        show_source = window.config.get("user_preference.show_source_annotation")
        prefix = label.text + "\n" if show_source else ""
        rich_prefix = (label.rich_text or "") + "<br>" if show_source else ""
        label.set_source_text(prefix + text, label.item.font(), label.item.defaultTextColor(),
                              rich_text=rich_prefix + escape(text), background=label.background)


class FrozenLoader(loader):
    def __init__(self, *args, frozen_result, **kwargs):
        kwargs["read_data"] = False
        super().__init__(*args, **kwargs)
        self.frozen_result = frozen_result

    def _run(self):
        try:
            self._check_cancelled()
            self._require_expected_source_current()
            original = self.frozen_result
            transposed = self.axes_dict["x"] != original.axis_param["x"].name
            self.axis_data = {axis: original.axis_data[other].copy() for axis, other in
                              (("x", "y" if transposed else "x"), ("y", "x" if transposed else "y"))}
            self.axis_param = {axis: original.axis_param[other] for axis, other in
                               (("x", "y" if transposed else "x"), ("y", "x" if transposed else "y"))}
            self.display_param = copy(original.display_param)
            self.complex_samples = original.complex_samples
            if self.complex_samples is not None and transposed:
                self.complex_samples = replace(
                    self.complex_samples,
                    coordinates=self.complex_samples.coordinates[::-1],
                    parameters=self.complex_samples.parameters[::-1],
                )
            if original.dataGrid is not None:
                self.dataGrid = (original.dataGrid.T if transposed else original.dataGrid).copy()
            operated = self.do_operations()
            if operated is not None:
                self.axis_data = {"x": operated[0], "y": operated[1]}
                if original.dataGrid is not None:
                    self.dataGrid = operated[2]
                self._apply_operation_metadata()
            if self.complex_samples is not None and original.dataGrid is not None:
                self.complex_samples = self.complex_samples.bind_grid(
                    self.axis_data["x"], self.axis_data["y"], check=self._check_cancelled,
                )
            self.dataset_completed = True
            self.loaded_from_sql_heatmap = True
            self.heatmap_full_resolution = True
            self.dataset_length_at_start = original.dataset_length_at_start
            self.loaded_point_count = original.loaded_point_count
            self._check_cancelled()
            self._emit_finished(True, capture_plot_result(self))
        except Exception as error:
            self.emitter.errorOccurred.emit(error)
            self._emit_finished(False)


class FrozenSlice(plot2d):
    """A detached, already processed plane with normal 2D appearance/export."""

    def __init__(self, dataset_key, param, source, config, threadPool, dataset_holder, **kwargs):
        axes = source.axis_options
        synthetic = ParamSpec(param.name, "numeric", label=source.display_param.label,
                              unit=source.display_param.unit, depends_on=[axes["y"], axes["x"]])
        self._frozen_result = replace(source._displayed_result, dataset_completed=True, slice_volume=None,
                                      slice_frame=None, axis_data={k: v.copy() for k, v in source.axis_data.items()},
                                      dataGrid=source.dataGrid.copy())
        self._frozen_description = "Frozen · " + source._slice_description()
        self._frozen_metadata = source._slice_export_metadata()
        self._frozen_levels = tuple(source.bar.levels()) if "bar" in source.__dict__ else None
        super().__init__(dataset_key, synthetic, config, threadPool, dataset_holder, **kwargs)
        self._colorbar_manual_levels = self._frozen_levels

    def _create_plot_loader(self, *args, **kwargs):
        return FrozenLoader(*args, frozen_result=self._frozen_result, **kwargs)

    def _refresh_monitor_required(self, dataset=None):
        return False

    def _update_large_heatmap_state(self, worker):
        self._large_heatmap_sql_mode = False
        self._update_heatmap_downsample_state(worker)

    def _update_plot_annotation(self):
        super()._update_plot_annotation()
        _append_annotation(self, self._frozen_description)

    def _capture_heatmap_csv(self):
        return replace(super()._capture_heatmap_csv(), constants=self._frozen_metadata)

    def openSweep(self, side):
        if self.__dict__.get("dataGrid") is None or not self.dataGrid.size:
            return
        i, j = self.__dict__.get("z_index") or (len(self.axis_data["x"])//2, len(self.axis_data["y"])//2)
        role = "x" if side == "h" else "y"
        values = self.dataGrid[j, :] if side == "h" else self.dataGrid[:, i]
        fixed = self.axis_param["y" if side == "h" else "x"]
        coordinate = self.axis_data["y"][j] if side == "h" else self.axis_data["x"][i]
        param = ParamSpec(self.param.name, "numeric", label=self.display_param.label,
                         unit=self.display_param.unit, depends_on=[self.axis_param[role].name])
        result = replace(self.worker.result, dataGrid=None,
                         axis_data={"x": self.axis_data[role].copy(), "y": values.copy()},
                         axis_param={"x": self.axis_param[role], "y": copy(self.display_param)})
        description = f"{self._frozen_description} · {fixed.name} = {coordinate} {fixed.unit}"
        self.open_subplot.emit(FrozenLine, self._dataset_key,
                               (param, result, description, self._frozen_metadata))


class FrozenLine(plot1d):
    def __init__(self, dataset_key, param, result, description, metadata, config,
                 threadPool, dataset_holder, **kwargs):
        self._frozen_result = result
        self._frozen_description = description
        self._frozen_metadata = metadata
        super().__init__(dataset_key, param, config, threadPool, dataset_holder, **kwargs)

    def _create_plot_loader(self, *args, **kwargs):
        return FrozenLoader(*args, frozen_result=self._frozen_result, **kwargs)

    def initFrame(self):
        super().initFrame()
        self.line.setData(connect="finite")

    def _refresh_monitor_required(self, dataset=None):
        return False

    def _update_plot_annotation(self):
        super()._update_plot_annotation()
        _append_annotation(self, self._frozen_description)

    def _capture_line_csv(self, exporter, *, transformed=False):
        return replace(super()._capture_line_csv(exporter, transformed=transformed), constants=self._frozen_metadata)
