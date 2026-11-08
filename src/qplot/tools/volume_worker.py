"""Bounded source decoding and cancellable slice processing off the GUI thread."""

import threading
from contextlib import ExitStack
from copy import copy
from dataclasses import replace
from typing import Any

import numpy as np
from PyQt6 import QtCore

from qplot.datahandling._numeric_arrays import NumericArrayReader
from qplot.datahandling._sqlite_identifiers import quote_identifier_text as identifier
from qplot.datahandling.numeric_samples import concatenate_record_samples
from qplot.datahandling.parameter_data import flatten_record_columns
from qplot.datahandling.plot_progress import PlotProgress
from qplot.datahandling.qcodes_cache import (
    cache_dataset_completed,
    cache_dataset_run_id,
    cache_is_live,
    cache_rundescriber,
    snapshot_cache_parameter_state,
)
from qplot.datahandling.volume import VolumeStore
from qplot.tools.operation_registry import OperationCall, OperationExecutionError
from qplot.tools.plot_result import capture_plot_result
from qplot.tools.worker import PlotWorkCancelled, loader


def processed_plane(frame, operations, param, axis_params, *, check=lambda: None,
                    cancelled=lambda: False):
    """The same plane pipeline backs images, all cuts, and colour statistics."""
    check()
    display_param = copy(param)
    if not operations or not frame.values.size:
        return frame, display_param
    data = {"x": frame.x.copy(), "y": frame.y.copy(), "z": frame.values.copy()}
    for operation in operations:
        check()
        try:
            result = (operation.execute(data, cancelled) if isinstance(operation, OperationCall)
                      else operation(data))
            data.update(result)
        except Exception as error:
            check()
            raise OperationExecutionError(f"Operation {getattr(operation, 'name', '')} failed: {error}") from error
        axis = getattr(operation, "derivative_axis", None)
        if axis in axis_params:
            coordinate = axis_params[axis]
            display_param.label = f"d({display_param.label or display_param.name})/d({coordinate.label or coordinate.name})"
            if coordinate.unit:
                display_param.unit = f"{display_param.unit or '1'}/{coordinate.unit}"
    check()
    return replace(frame, x=data["x"], y=data["y"], values=data["z"]), display_param


class VolumeLoader(loader):
    def __init__(self, *args, volume=None, depth_value=None, depth_index=0,
                 follow_latest=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.volume = volume
        self.depth_value = depth_value
        self.depth_index = depth_index
        self.follow_latest = follow_latest
        self.operation_state: Any = None
        if volume is not None:
            self.read_data = False

    def _validated_chunks(self, chunks):
        names = (*self.param.depends_on_, self.param.name)
        for columns in chunks:
            self._check_cancelled()
            if any(np.asarray(column).dtype.kind not in "biufO" for column in columns):
                raise ValueError("3D viewing requires real numeric measurements and coordinates.")
            yield tuple(self._float_heatmap_coordinates(np.asarray(column), name)
                        for name, column in zip(names[:3], columns[:3], strict=True)) + (np.asarray(columns[3]),)

    def _source_chunks(self, connection):
        names = (*self.param.depends_on_, self.param.name)
        table = identifier(self.table_name)
        columns = ", ".join(
            f"CASE WHEN typeof({identifier(name)})='blob' THEN NULL ELSE {identifier(name)} END, "
            f"typeof({identifier(name)})='blob'" for name in names
        )
        after = 0
        while True:
            self._check_cancelled()
            rows = connection.execute(
                f"SELECT id, {columns} FROM {table} WHERE id>? AND "
                f"{identifier(self.param.name)} IS NOT NULL ORDER BY id LIMIT 1024", (after,),
            ).fetchall()
            if not rows:
                return
            scalars = []
            for row in rows:
                self._check_cancelled()
                if not any(row[2::2]):
                    scalars.append(tuple(np.nan if value is None else value for value in row[1::2]))
                    continue
                if scalars:
                    yield _scalar_columns(scalars)
                    scalars = []
                with ExitStack() as stack:
                    values = []
                    for i, name in enumerate(names):
                        value, is_blob = row[1 + 2*i:3 + 2*i]
                        if is_blob:
                            blob = stack.enter_context(connection.blobopen(self.table_name, name, row[0], readonly=True))
                            value = NumericArrayReader(blob)
                            if value.dtype.kind == "c":
                                raise ValueError(f"Complex parameter '{name}' cannot be displayed in a 3D slice.")
                        values.append(np.nan if value is None else value)
                    array_sizes = {name: value.size for name, value in zip(names, values, strict=True)
                                   if isinstance(value, NumericArrayReader)}
                    first = next((array_sizes[name] for name in (self.param.name, *self.param.depends_on_)
                                  if name in array_sizes), 1)
                    size = 0 if first == 0 else max(array_sizes.values(), default=1)
                    if any(n not in (1, size) for n in array_sizes.values()):
                        raise ValueError("3D setpoint and measurement shapes do not match.")
                    for start in range(0, size, 16_384):
                        self._check_cancelled()
                        count = min(size - start, 16_384)
                        decoded = []
                        for value in values:
                            if isinstance(value, NumericArrayReader):
                                value = value.read(0 if value.size == 1 else start,
                                                   1 if value.size == 1 else start + count,
                                                   self._check_cancelled)
                            decoded.append(np.broadcast_to(np.asarray(value).reshape(-1), (count,)))
                        yield tuple(decoded)
            if scalars:
                yield _scalar_columns(scalars)
            after = rows[-1][0]
            self.dataset_length_at_start = max(getattr(self, "dataset_length_at_start", 0), after)

    def _cache_chunks(self):
        written, _, data = snapshot_cache_parameter_state(self.cache, self.param.name)
        columns = flatten_record_columns(data[self.param.name], self._check_cancelled)
        names = (*self.param.depends_on_, self.param.name)
        size = columns[self.param.name].size
        if self._parameter_shape() is not None and written.get(self.param.name) is not None:
            size = min(size, written[self.param.name])
        for start in range(0, size, 16_384):
            self._check_cancelled()
            yield tuple(columns[name].ravel()[start:min(size, start + 16_384)] for name in names)

    def _build_volume(self):
        names = tuple(self.param.depends_on_)
        if len(names) != 3:
            raise ValueError("3D slicing requires exactly three independent parameters.")
        for name in (*names, self.param.name):
            if self.param_dict[name].type not in ("numeric", "array"):
                raise ValueError(f"3D viewing requires real numeric parameter '{name}'.")
        self._prepare_trusted_prefix()
        self._progress = PlotProgress("Indexing 3D coordinates", stage=2)
        shapes = cache_rundescriber(self.cache).shapes or {}
        kwargs = dict(max_points=self.max_full_heatmap_points,
                      planned_shape=shapes.get(self.param.name),
                      check=self._check_cancelled,
                      progress=lambda n: setattr(self, "_progress", PlotProgress(
                          "Indexing 3D coordinates", completed=n, stage=2)))
        if cache_is_live(self.cache):
            self.dataset_completed = cache_dataset_completed(self.cache)
            volume = VolumeStore.from_chunks(names, self._validated_chunks(self._cache_chunks()),
                                            completed=self.dataset_completed, **kwargs)
        else:
            conn = self._plot_connection()
            self._set_sql_connection(conn)
            try:
                self.dataset_completed = bool(conn.execute(
                    "SELECT is_completed FROM runs WHERE run_id=?", (cache_dataset_run_id(self.cache),),
                ).fetchone()[0])
                volume = VolumeStore.from_chunks(names, self._validated_chunks(self._source_chunks(conn)),
                                                completed=self.dataset_completed, **kwargs)
            finally:
                self._close_sql_connection(conn)
        volume.revision = getattr(self, "dataset_length_at_start", 0)
        return volume

    def _run(self):
        try:
            self._check_cancelled()
            self._require_expected_source_current()
            volume = self.volume or self._build_volume()
            self.volume = volume
            self.dataset_completed = volume.completed
            self.dataset_length_at_start = volume.revision
            x, y = self.axes_dict["x"], self.axes_dict["y"]
            depth_name = volume.depth_name(x, y)
            value = volume.latest.get(depth_name) if self.follow_latest else self.depth_value
            index = volume.nearest_index(depth_name, value) if value is not None else self.depth_index
            self._progress = PlotProgress("Loading 2D slice", stage=2)
            frame = volume.plane(x, y, index, check=self._check_cancelled)
            self.axis_param = {axis: self.param_dict[name] for axis, name in (("x", x), ("y", y))}
            frame, self.display_param = processed_plane(
                frame, self.operations, self.param, self.axis_param,
                check=self._check_cancelled, cancelled=self.is_cancelled,
            )
            self.axis_data = {"x": frame.x, "y": frame.y}
            self.dataGrid = frame.values
            self.loaded_point_count = frame.samples
            self.heatmap_full_resolution = True
            self.loaded_from_sql_heatmap = True  # independent of the QCoDeS cache
            self.read_data = False
            self._check_cancelled()
            self._require_expected_source_current()
            result = replace(capture_plot_result(self), slice_volume=volume, slice_frame=frame)
            self._emit_finished(True, result)
        except Exception as error:
            if self.is_cancelled() or isinstance(error, PlotWorkCancelled):
                self._finish_cancelled()
            else:
                self.emitter.errorOccurred.emit(error)
                self._emit_finished(False)


class _TaskSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(object)


def _scalar_columns(rows):
    # NumPy's usual mixed int/float inference would round before the
    # coordinate validator or duplicate-mean accumulator could inspect them.
    return tuple(concatenate_record_samples([np.asarray([value]) for value in column])
                 for column in zip(*rows, strict=True))


class VolumeTask(QtCore.QRunnable):
    """Secondary analysis with bounded lifetime and no source database handle."""

    def __init__(self, function):
        super().__init__()
        self.emitter = _TaskSignals()
        self.function = function
        self._cancelled = threading.Event()
        self.running = True
        self.progress = PlotProgress("Processing volume", stage=2)

    def cancel(self):
        self._cancelled.set()

    def is_cancelled(self):
        return self._cancelled.is_set()

    def check(self):
        if self.is_cancelled():
            raise PlotWorkCancelled("Volume processing cancelled.")

    def run(self):
        result = None
        try:
            self.check()
            result = self.function(self)
            self.check()
        except Exception as error:
            if not self.is_cancelled():
                self.emitter.error.emit(error)
        finally:
            self.running = False
            self.emitter.finished.emit(None if self.is_cancelled() else result)
