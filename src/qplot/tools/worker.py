import math
import threading
from contextlib import ExitStack, closing
from copy import copy
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6 import QtCore

from qplot.datahandling import load_param_data_from_db, load_param_data_from_db_prep
from qplot.datahandling.dimensions import ensure_supported_plot_dimensions
from qplot.datahandling.file_identity import (
    canonical_database_path,
    database_file_identity,
    database_sidecar_identities,
)
from qplot.datahandling.parameter_data import (
    concatenate_record_samples,
    integer_preserving_dtype,
    numeric_isfinite,
    numeric_isnan,
)
from qplot.datahandling.qcodes_cache import (
    cache_database_path,
    cache_dataset_completed,
    cache_is_live,
    cache_rundescriber,
    cache_table_name,
    snapshot_cache_parameter_state,
)
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    qcodes_read_only_connection,
    sqlite_read_only_connection,
)
from qplot.diagnostics import log_exception
from qplot.tools.operation_registry import OperationCall, OperationExecutionError

from . import data2matrix
from .finite_means import FiniteBinMeans
from .heatmap_geometry import canonicalize_heatmap_data

if TYPE_CHECKING:
    import qcodes

MAX_FULL_HEATMAP_POINTS = 2_000_000
MAX_SQL_HEATMAP_SOURCE_ROWS = 250_000
MAX_SQL_HEATMAP_GRID_SIDE = 800
MAX_SQL_HEATMAP_GRID_CELLS = 250_000
SQL_HEATMAP_SAMPLES_PER_CELL = 4
CANCELLATION_CHUNK_SIZE = 65_536


class PlotWorkCancelled(InterruptedError):
    """Internal control flow used to unwind cancelled plot work safely."""


def _sqlite_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


class _HeatmapArrayReader:
    """Read numeric NPY slices from a physically read-only SQLite BLOB."""

    def __init__(self, blob):
        self.blob = blob
        version = np.lib.format.read_magic(blob)
        # QCoDeS writes NPY v3: its length field matches v2 and its numeric
        # dtype headers are ASCII (v3 only changes the text encoding).
        if version == (1, 0):
            read_header = np.lib.format.read_array_header_1_0
        elif version in ((2, 0), (3, 0)):
            read_header = np.lib.format.read_array_header_2_0
        else:
            raise ValueError("Unsupported heatmap array format.")
        self.shape, self.fortran_order, self.dtype = read_header(blob)
        self.offset = blob.tell()
        self.size = math.prod(self.shape)
        if self.dtype.kind not in "biufc":
            raise ValueError("Heatmap arrays must contain numeric values.")
        if self.offset + self.size * self.dtype.itemsize != len(blob):
            raise ValueError("Heatmap array payload does not match its shape.")

    def read(self, start, stop, check_cancelled):
        check_cancelled()
        if not self.fortran_order or len(self.shape) <= 1:
            self.blob.seek(self.offset + start * self.dtype.itemsize)
            return np.frombuffer(
                self.blob.read((stop - start) * self.dtype.itemsize),
                dtype=self.dtype,
            )

        # Flatten every column in the same logical (C) order, even when
        # signal and setpoints were saved with different memory layouts.
        positions = np.ravel_multi_index(
            np.unravel_index(np.arange(start, stop), self.shape),
            self.shape, order="F",
        )
        order = np.argsort(positions)
        ordered_positions = positions[order]
        boundaries = np.r_[0, np.flatnonzero(np.diff(ordered_positions) != 1) + 1,
                           stop - start]
        values = np.empty(stop - start, dtype=self.dtype)
        for first, last in zip(boundaries[:-1], boundaries[1:], strict=True):
            check_cancelled()
            self.blob.seek(self.offset + int(ordered_positions[first]) * self.dtype.itemsize)
            values[order[first:last]] = np.frombuffer(
                self.blob.read(int(last - first) * self.dtype.itemsize),
                dtype=self.dtype,
            )
        return values


class loader(QtCore.QRunnable):
    """
    A Worker to be placed inside a QThreadPool.
    It handles fetched data from the dataset cache and performs necessary work
    before rerending data
    
    """
    def __init__(self,
                 cache : "qcodes.dataset.data_set_cache.DataSetCacheWithDBBackend",
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict,
                 axes : dict,
                 read_data : bool = True,
                 operations: list | None = None,
                 force_sql_heatmap: bool = False,
                 max_full_heatmap_points: int = MAX_FULL_HEATMAP_POINTS,
                 max_heatmap_grid_cells: int = MAX_SQL_HEATMAP_GRID_CELLS,
                 max_heatmap_grid_side: int = MAX_SQL_HEATMAP_GRID_SIDE,
                 heatmap_axis_ranges: dict | None = None,
                 heatmap_full_axis_ranges: dict | None = None,
                 database_identity=None,
                 deadline=None,
                 ):
        """
        Sets up worker with required data for run()
        
        Please note that self.__init__ is run in main thread, self.run() is ran
        in the worker thread.

        Parameters
        ----------
        cache : qcodes.dataset.data_set_cache.DataSetCacheWithDBBackend
            The cache for the dataset that is being refreshed.
        param : qcodes.dataset.descriptions.param_spec.ParamSpec
            The parameter being updated.
        param_dict : dict{str: ParamSpec}
            List of all parameter data inside the dataset.
        axes : dict{str: str}
            The selected parameter for the axes.
        read_data : bool
            Whether to read the database for new data or use current data.
            The default is True.
        operations: list
            A list containing functions to perform on the refreshed data
            before returning

        """
        super().__init__()
        self.running = True
        self.emitter = _emitter() # For signals
        self._cancelled = threading.Event()
        self._sql_connection_lock = threading.Lock()
        self._publication_lock = threading.RLock()
        self._sql_connection = None
        
        # Required working data
        self.cache = cache
        self.table_name = cache_table_name(cache)
        self.sidecar_identities = database_sidecar_identities(
            cache_database_path(cache)
        )
        self.param = param
        self.dataset_completed: bool | None = None
        self.display_param = copy(param)
        self.param_dict = param_dict
        
        self.axes_dict = axes
        self.read_data = read_data
        self.operations = [] if operations is None else operations
        self.force_sql_heatmap = force_sql_heatmap
        self.max_full_heatmap_points = max(1, int(max_full_heatmap_points))
        self.max_heatmap_grid_cells = max(1, int(max_heatmap_grid_cells))
        self.max_heatmap_grid_side = max(1, int(max_heatmap_grid_side))
        self.heatmap_axis_ranges = heatmap_axis_ranges
        self.heatmap_full_axis_ranges = heatmap_full_axis_ranges
        self.database_identity = database_identity
        self.deadline = deadline
        self.database_replaced = False
        self.sampled_heatmap_source = False
        self.aggregated_heatmap_source = False
        self.loaded_from_sql_heatmap = False
        self.loaded_point_count: int | None = None
        self.heatmap_downsample_info: dict[str, Any] | None = None
        self.heatmap_source_grid_shape: tuple[int, int] | None = None
        self.heatmap_source_axis_ranges: (
            dict[str, tuple[float, float]] | None
            ) = None
        
    
    def _ensure_cancel_state(self) -> None:
        """Initialise cancellation state for legacy/tests using ``__new__``."""

        if not hasattr(self, "_cancelled"):
            self._cancelled = threading.Event()
        if not hasattr(self, "_sql_connection_lock"):
            self._sql_connection_lock = threading.Lock()
            self._sql_connection = None
        if not hasattr(self, "_publication_lock"):
            self._publication_lock = threading.RLock()


    def cancel(self) -> None:
        """Request cooperative cancellation and interrupt an active SQL read."""

        self._ensure_cancel_state()
        with self._publication_lock:
            self._cancelled.set()
        with self._sql_connection_lock:
            connection = self._sql_connection
        if connection is not None:
            try:
                connection.interrupt()
            except Exception:
                # The worker may be closing the connection at the same time.
                pass


    def is_cancelled(self) -> bool:
        self._ensure_cancel_state()
        return self._cancelled.is_set()


    def _check_cancelled(self) -> None:
        if self.is_cancelled():
            raise PlotWorkCancelled("Plot load cancelled.")


    def _read_only_open_kwargs(self) -> dict[str, Any]:
        """Return identity and abort controls for snapshot preparation."""
        self._require_expected_source_current()
        kwargs: dict[str, Any] = {
            "cancelled_callback": self.is_cancelled,
        }
        database_identity = getattr(self, "database_identity", None)
        if database_identity is not None:
            kwargs["expected_database_identity"] = database_identity
        deadline = getattr(self, "deadline", None)
        if deadline is not None:
            kwargs["deadline"] = deadline
        return kwargs


    def _require_expected_source_current(self) -> None:
        """Validate the exact retained-plot main and sidecar identities."""

        expected_sidecars = getattr(self, "expected_sidecar_identities", None)
        if expected_sidecars is None:
            return
        database_path = getattr(self, "expected_database_path", None)
        if database_path is None:
            database_path = cache_database_path(self.cache)
        expected_resolved_path = getattr(
            self,
            "expected_resolved_database_path",
            None,
        )
        if (
                expected_resolved_path is not None
                and canonical_database_path(database_path)
                != expected_resolved_path
                ):
            raise DatabaseInstanceChangedError(
                "The retained plot database path resolved to another source."
            )
        expected_identity = getattr(self, "database_identity", None)
        if (
                expected_identity is not None
                and database_file_identity(database_path) != expected_identity
                ):
            raise DatabaseInstanceChangedError(
                "The retained plot database was replaced."
            )
        if database_sidecar_identities(
                database_path,
                expected_resolved_path,
                ) != frozenset(expected_sidecars):
            raise DatabaseInstanceChangedError(
                "A retained plot SQLite sidecar was replaced."
            )


    def _set_sql_connection(self, connection) -> None:
        self._ensure_cancel_state()
        with self._sql_connection_lock:
            self._sql_connection = connection
        if connection is not None and self._cancelled.is_set():
            try:
                connection.interrupt()
            except Exception:
                pass


    def _close_sql_connection(self, connection) -> None:
        try:
            connection.close()
        finally:
            self._set_sql_connection(None)
        self._require_expected_source_current()


    def _emit_finished(self, finished: bool) -> None:
        """Emit completion unless Qt already deleted the receiver at shutdown."""

        self._ensure_cancel_state()
        with self._publication_lock:
            if finished and self._cancelled.is_set():
                finished = False
                self.running = False
            try:
                self.emitter.finished.emit(finished)
            except RuntimeError as err:
                message = str(err)
                if not (
                        "wrapped C/C++ object" in message
                        and "has been deleted" in message
                        ):
                    raise


    def _finish_cancelled(self) -> None:
        self.running = False
        self._emit_finished(False)


    def run(self):
        try:
            self._check_cancelled()
            self._require_expected_source_current()
            ensure_supported_plot_dimensions(
                getattr(self.param, "name", "Measurement"),
                getattr(self.param, "depends_on_", ()),
            )
            if len(getattr(self.param, "depends_on_", ())) > 1:
                self._validate_heatmap_parameter_types()
            elif len(self.param.depends_on_) == 1:
                for name in (self.param.name, *self.param.depends_on_):
                    if getattr(self.param_dict[name], "type", None) == "complex":
                        self._reject_complex_line(name)
            cache = self.cache
            cache_live = cache_is_live(cache)

            # Completion checks and cache preparation can query SQLite, so do
            # them here rather than on the GUI thread. In-memory live caches
            # are already authoritative and should never be read via SQLite.
            if self.read_data:
                if cache_live:
                    self.dataset_completed = cache_dataset_completed(cache)
                    self.read_data = False
                else:
                    completion_conn = qcodes_read_only_connection(
                        cache_database_path(cache),
                        **self._read_only_open_kwargs(),
                        )
                    self._set_sql_connection(completion_conn)
                    try:
                        self._check_cancelled()
                        (
                            parameter_complete,
                            self.dataset_completed,
                        ) = load_param_data_from_db_prep(
                            cache,
                            self.param,
                            connection=completion_conn,
                            )
                    finally:
                        self._close_sql_connection(completion_conn)
                    self._check_cancelled()
                    self.read_data = not parameter_complete

            self._check_cancelled()
            use_sql_heatmap = (
                self.read_data
                and not cache_live
                and self._should_use_sql_heatmap()
                )
            if use_sql_heatmap:
                self._load_large_heatmap_from_sql()

            else:
                if self.read_data:
                    write_status, read_status, existing_data = (
                        snapshot_cache_parameter_state(cache, self.param.name)
                        )
                    conn = qcodes_read_only_connection(
                        cache_database_path(cache),
                        **self._read_only_open_kwargs(),
                    )
                    self._set_sql_connection(conn)
                    try:
                        self._check_cancelled()
                        (
                            self.updated_read_status,
                            self.updated_write_status,
                            self.cache_data
                        ) = load_param_data_from_db(
                            conn,
                            self.table_name,
                            cache_rundescriber(cache),
                            self.param.name,
                            write_status,
                            read_status,
                            existing_data,
                            check_cancelled=self._check_cancelled,
                        )
                    finally:
                        self._close_sql_connection(conn)

                    self._check_cancelled()
                    data = self.cache_data[self.param.name]
                    write_status = self.updated_write_status

                else:
                    # Capture the acquired extent with its arrays, including
                    # cache-only processing after a run's final publication.
                    write_status, _, cached_data = snapshot_cache_parameter_state(
                        cache, self.param.name,
                    )
                    data = cached_data[self.param.name]

                self._check_cancelled()
                data = self._normalise_array_records(data)
                depvarData = data[self.param.name]
                acquired = self._acquired_sample_mask(depvarData, write_status)

                # A QCoDeS array record adds a storage dimension, not an
                # independent setpoint. Select the plot by declared axes.
                if len(self.param.depends_on_) == 1:
                    axis_data, axis_param = self.for_1d(data, acquired & ~numeric_isnan(depvarData))

                # for shaped 2d plots
                elif len(depvarData.shape) == 2:
                    (
                        axis_data,
                        axis_param,
                        dataGrid
                    ) = self.for_shaped_2d(
                        data,
                        depvarData,
                        acquired=acquired,
                        )

                else:
                    #Remove nan values
                    valid_rows = acquired & ~numeric_isnan(depvarData)

                    # for unshaped 2d plots
                    (
                        axis_data,
                        axis_param,
                        dataGrid
                    ) = self.for_unshaped_2d(
                        data,
                        valid_rows,
                        depvarData
                        )

                # Allow main to fetch data
                self.axis_data = axis_data
                self.axis_param = axis_param
                if len(self.param.depends_on_) != 1:
                    self.dataGrid = dataGrid

        except PlotWorkCancelled:
            self._finish_cancelled()
            return
        except DatabaseInstanceChangedError as err:
            self.database_replaced = True
            if self.is_cancelled():
                self._finish_cancelled()
                return
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return
        except Exception as err: # Raise error in main thread
            if self.is_cancelled():
                self._finish_cancelled()
                return
            log_exception("Plot worker failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False) # False: Failed
            return

        try:
            self._check_cancelled()
            # Operations are an ordered, atomic pipeline. Large heatmaps are
            # reduced for display only after this pipeline has completed.
            # Normalize heatmap geometry first so coordinate-dependent
            # operations see spatial order rather than acquisition order.
            if self.operations:
                self._canonicalize_heatmap()
                self._check_cancelled()
            results = self.do_operations()
            if results is not None:
                (
                    self.axis_data["x"],
                    self.axis_data["y"]
                ) = results[:2]
                if hasattr(self, "dataGrid"):
                    self.dataGrid = results[2]
                self._apply_operation_metadata()

            self._check_cancelled()
            if len(self.param.depends_on_) == 1:
                # Operations can replace either axis. Validate their output
                # before finished(True) permits any GUI/cache publication.
                for axis in ("x", "y"):
                    self._real_line_values(
                        self.axis_data[axis], self.axis_param[axis].name,
                    )
                    self._check_cancelled()
            self._aggregate_operated_heatmap_if_needed()
            self._check_cancelled()
            # Operations may return replacement coordinates or grids, so keep
            # the final geometry validation and normalization as well.
            self._canonicalize_heatmap()
            self._check_cancelled()
        except PlotWorkCancelled:
            self._finish_cancelled()
            return
        except DatabaseInstanceChangedError as err:
            self.database_replaced = True
            if self.is_cancelled():
                self._finish_cancelled()
                return
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return
        except Exception as err:
            if self.is_cancelled():
                self._finish_cancelled()
                return
            log_exception("Plot processing failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return

        try:
            self._require_expected_source_current()
        except DatabaseInstanceChangedError as err:
            self.database_replaced = True
            if self.is_cancelled():
                self._finish_cancelled()
                return
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return

        # Callback
        self._emit_finished(True)


    def _acquired_sample_mask(self, values, write_status):
        """Separate acquired samples from dtype-dependent planned padding."""
        self._check_cancelled()
        if self._parameter_shape() is None:
            # Unshaped caches have no planned padding; their write status
            # does not count all acquired samples.
            acquired = np.ones(values.shape, dtype=bool)
        else:
            acquired = np.zeros(values.shape, dtype=bool)
            count = write_status.get(self.param.name)
            if count is not None:
                acquired.ravel()[:count] = True
        self._check_cancelled()
        return acquired


    def _normalise_array_records(self, data):
        """Flatten ragged QCoDeS records together, without touching the cache."""

        heatmap = len(self.param.depends_on_) > 1
        setpoints = (
            [self.axes_dict[axis] for axis in ("x", "y")]
            if heatmap else self.param.depends_on_
        )
        names = list(dict.fromkeys((self.param.name, *setpoints)))
        columns = [np.asarray(data[name]) for name in names]
        if not any(
            column.dtype.kind == "O" and any(np.ndim(value) for value in column.flat)
            for column in columns
        ):
            return data

        # Nested object cells represent acquisition records. Dense numeric
        # object cells instead preserve mixed signed/unsigned sample values.
        # Other columns may still hold one scalar or a dense array per record.
        record_count = len(columns[0])
        if any(column.ndim == 0 or len(column) != record_count for column in columns):
            raise ValueError("Setpoint and measurement record counts do not match.")

        def records():
            for index in range(record_count):
                self._check_cancelled()
                values = [np.asarray(column[index]) for column in columns]
                size = values[0].size
                for name, value in zip(names, values, strict=True):
                    if heatmap:
                        self._real_heatmap_values(
                            value, name, coordinate=name != self.param.name,
                        )
                    else:
                        self._real_line_values(value, name)
                    if value.dtype.kind not in "biufcO":
                        raise ValueError("Array records must contain numeric values.")
                    if value.size not in (1, size):
                        raise ValueError("Setpoint and measurement shapes do not match.")
                yield values, size

        # Count actual samples first: padding to the longest record can grow
        # quadratically. Reject complex records above before allocating or
        # converting any numeric output; the raw cache remains untouched.
        total = 0
        dtypes: list[np.dtype | None] = [None for _ in names]
        for values, size in records():
            total += size
            dtypes = [value.dtype if dtype is None else integer_preserving_dtype((dtype, value.dtype))
                      for dtype, value in zip(dtypes, values, strict=True)]
        self._check_cancelled()
        if heatmap:
            self._requires_bounded_heatmap(total)
        normalised = {name: np.empty(total, dtype=dtype)
                      for name, dtype in zip(names, dtypes, strict=True)}
        offset = 0
        chunk_size = max(1, CANCELLATION_CHUNK_SIZE)
        for values, size in records():
            for start in range(0, size, chunk_size):
                stop = min(size, start + chunk_size)
                for name, value in zip(names, values, strict=True):
                    self._check_cancelled()
                    # flat slices use logical C order and copy at most one
                    # chunk, even when a record has Fortran storage order.
                    normalised[name][offset + start:offset + stop] = (
                        value.item() if value.size == 1 else value.flat[start:stop]
                    )
            offset += size
        self._check_cancelled()
        return normalised


    def _canonicalize_heatmap(self) -> None:
        """Keep worker indices consistent with increasing heatmap axes."""

        self._check_cancelled()
        if not hasattr(self, "dataGrid"):
            return
        x_data = np.asarray(self.axis_data.get("x", []))
        self._check_cancelled()
        y_data = np.asarray(self.axis_data.get("y", []))
        self._check_cancelled()
        data_grid = np.asarray(self.dataGrid)
        if x_data.size == 0 or y_data.size == 0 or data_grid.size == 0:
            return

        x_data, y_data, data_grid = canonicalize_heatmap_data(
            x_data,
            y_data,
            data_grid,
            )
        self._check_cancelled()
        self.axis_data["x"] = x_data
        self.axis_data["y"] = y_data
        self.dataGrid = data_grid


    def _should_use_sql_heatmap(self):
        if len(getattr(self.param, "depends_on_", ())) <= 1:
            return False

        if self.force_sql_heatmap:
            if getattr(self, "operations", None):
                raise OperationExecutionError(
                    "Operations cannot be applied to a heatmap detail reload."
                    )
            return True

        setpoint_count = self._large_heatmap_point_count()
        if setpoint_count is None:
            return False

        requires_bounded_load = self._requires_bounded_heatmap(setpoint_count)
        return requires_bounded_load or getattr(
            self, "_variable_multidimensional_array_records", False,
        )


    def _requires_bounded_heatmap(self, point_count):
        """Reject full-resolution operations before allocating a large grid."""

        self._check_cancelled()
        limit = max(1, int(getattr(
            self,
            "max_full_heatmap_points",
            MAX_FULL_HEATMAP_POINTS,
            )))
        requires_bounded_load = point_count > limit
        if requires_bounded_load and getattr(self, "operations", None):
            raise OperationExecutionError(
                f"This heatmap requires {point_count:,} full-resolution points, "
                f"which exceeds the full-resolution operation limit of {limit:,}. "
                "Disable operations or increase max_full_heatmap_points."
                )
        return requires_bounded_load


    def _large_heatmap_point_count(self):
        self._variable_multidimensional_array_records = False
        source_grid_shape = self._heatmap_source_grid_shape_from_metadata()
        planned_count = 0
        if source_grid_shape is not None:
            source_grid_rows, source_grid_columns = source_grid_shape
            planned_count = int(source_grid_rows * source_grid_columns)
            self.heatmap_source_grid_shape = source_grid_shape
            # Small planned shapes still need array-header inspection: the
            # stored record shapes need not be identical to the scan shape.
            if planned_count > self.max_full_heatmap_points or not self._has_array_heatmap_columns():
                self.total_point_count_estimate = planned_count
                return planned_count

        conn = sqlite_read_only_connection(
            cache_database_path(self.cache),
            **self._read_only_open_kwargs(),
        )
        self._set_sql_connection(conn)
        try:
            self._check_cancelled()
            if self._has_array_heatmap_columns():
                # A storage row can contain millions of samples. Inspect
                # bounded NPY headers, stopping as soon as the limit is crossed.
                setpoint_count = 0
                first_shapes = None
                with closing(self._array_heatmap_records(conn)) as records:
                    for values, size in records:
                        shapes = tuple(value.shape if isinstance(value, _HeatmapArrayReader)
                                       else () for value in values)
                        if first_shapes is None:
                            first_shapes = shapes
                        elif any(shape != first and max(len(shape), len(first)) > 1
                                 for first, shape in zip(first_shapes, shapes, strict=True)):
                            # QCoDeS' np.array(records, dtype=object) retry can
                            # also broadcast/fail for ragged multidimensional
                            # records, before our normaliser ever receives data.
                            self._variable_multidimensional_array_records = True
                        setpoint_count += size
                        if setpoint_count > self.max_full_heatmap_points:
                            break
                setpoint_count = max(planned_count, setpoint_count)
            else:
                setpoint_count = self._selected_parameter_row_count(conn)
        finally:
            self._close_sql_connection(conn)

        if setpoint_count is not None:
            self.total_point_count_estimate = setpoint_count
        return setpoint_count


    def _parameter_shape(self):
        shapes = getattr(cache_rundescriber(self.cache), "shapes", None)
        if not isinstance(shapes, dict):
            return None

        return shapes.get(self.param.name)


    def _shape_size(self, shape):
        if shape is None:
            return None

        try:
            dimensions = [int(dimension) for dimension in shape]
        except (TypeError, ValueError):
            return None

        if not dimensions or any(dimension <= 0 for dimension in dimensions):
            return None

        return math.prod(dimensions)


    def _load_large_heatmap_from_sql(self):
        # SQLite aggregates treat QCoDeS complex BLOBs as numbers, so reject
        # every declared complex column before the bounded SQL path can
        # compute a misleading grid.
        self._validate_heatmap_parameter_types()
        conn = sqlite_read_only_connection(
            cache_database_path(self.cache),
            **self._read_only_open_kwargs(),
        )
        self._set_sql_connection(conn)
        full_arrays = None
        try:
            self._check_cancelled()
            rowid_min, rowid_max = self._rowid_span(conn)
            self._check_cancelled()
            self.heatmap_source_grid_shape = (
                self._heatmap_source_grid_shape_from_metadata()
                )
            if (
                getattr(self, "_variable_multidimensional_array_records", False)
                and self.total_point_count_estimate <= self.max_full_heatmap_points
            ):
                full_arrays = self._read_full_array_heatmap_arrays(conn)
            x_data, y_data, z_data = (
                full_arrays if full_arrays is not None
                else self._read_heatmap_arrays(conn, rowid_min, rowid_max)
            )
        finally:
            self._close_sql_connection(conn)

        self._check_cancelled()
        if full_arrays is not None:
            # Keep normal full-resolution pivot/operation semantics for the
            # compatibility decoder, including sparse-grid size checks.
            data = dict(zip(
                (self.axes_dict["x"], self.axes_dict["y"], self.param.name),
                full_arrays, strict=True,
            ))
            axes, _params, data_grid = self.for_unshaped_2d(
                data, np.ones(z_data.size, dtype=bool), z_data,
            )
            x_axis, y_axis = axes["x"], axes["y"]
        else:
            x_axis, y_axis, data_grid = self._heatmap_grid_from_arrays(
                x_data, y_data, z_data,
            )
        self.axis_data = {
            "x": x_axis,
            "y": y_axis,
            }
        self.axis_param = {
            "x": self.param_dict[self.axes_dict["x"]],
            "y": self.param_dict[self.axes_dict["y"]],
            }
        self.dataGrid = data_grid
        self.loaded_from_sql_heatmap = True
        self.loaded_point_count = int(z_data.size)
        self.heatmap_downsample_info = self._heatmap_downsample_info()

        # The direct SQL path deliberately does not populate QCoDeS' full
        # in-memory cache. Keep future refreshes on the database path.
        self.read_data = False


    def _read_full_array_heatmap_arrays(self, conn):
        """Decode compatible small records in order, with bounded BLOB reads."""
        buffered = []
        source_count = 0
        with closing(self._array_heatmap_chunks(conn)) as chunks:
            for size, arrays in chunks:
                self._check_cancelled()
                source_count += size
                # A live acquisition may grow after the header preflight.
                # Switch to bounded rendering rather than grow the buffer;
                # operations instead raise their usual resolution-limit error.
                if self._requires_bounded_heatmap(source_count):
                    return None
                buffered.append(arrays)
        self._check_cancelled()
        self.total_point_count_estimate = source_count
        return tuple(
            concatenate_record_samples([arrays[axis] for arrays in buffered])
            if buffered else np.array([], dtype=float)
            for axis in range(3)
        )


    def _rowid_span(self, conn):
        self._check_cancelled()
        table = _sqlite_identifier(self.table_name)
        row = conn.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None, None

        return int(row[0]), int(row[1])


    def _read_heatmap_arrays(self, conn, rowid_min, rowid_max):
        self._check_cancelled()
        if rowid_min is None or rowid_max is None:
            self._heatmap_source_info = {
                "row_count": 0,
                "estimated_range_rows": None,
                "sampled": False,
                "aggregated": False,
                "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
                "sample_stride": None,
                "strategy": "empty",
                "axis_ranges": self._normalised_heatmap_axis_ranges(
                    self.heatmap_axis_ranges
                    ),
                }
            return (
                np.array([], dtype=float),
                np.array([], dtype=float),
                np.array([], dtype=float),
                )

        if self._has_array_heatmap_columns():
            return self._read_array_heatmap_arrays(conn)

        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        z_column = _sqlite_identifier(self.param.name)
        # Match _arrays_from_values: an observation contributes only when
        # both coordinates and its signal are finite. Apply this before
        # MIN/MAX, cardinalities and AVG so invalid samples cannot distort
        # bounds or poison a bin containing valid samples. BETWEEN also
        # excludes missing values, including SQLite NULLs.
        max_finite = float(np.finfo(float).max)
        finite_where_sql = " AND ".join(
            f"{column} BETWEEN {-max_finite} AND {max_finite}"
            for column in (x_column, y_column, z_column)
            )
        selected_count = self._selected_parameter_row_count(conn)
        # Retain the acquired row count as a conservative loading bound;
        # summaries and returned arrays count only valid observations.
        row_count = selected_count
        if row_count is None:
            row_count = rowid_max - rowid_min + 1
        columns = f"{x_column}, {y_column}, {z_column}"
        axis_ranges = self._normalised_heatmap_axis_ranges(self.heatmap_axis_ranges)
        self.total_point_count_estimate = row_count
        self._heatmap_source_info = {
            "row_count": int(row_count),
            "estimated_range_rows": int(row_count),
            "sampled": False,
            "aggregated": False,
            "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
            "sample_stride": None,
            "strategy": "all",
            "axis_ranges": axis_ranges,
            }

        axis_where_sql, parameters = self._heatmap_where_clause()
        if axis_where_sql:
            where_sql = f"{finite_where_sql} AND {axis_where_sql}"
            range_summary = self._heatmap_spatial_summary(
                conn,
                where_sql,
                parameters,
                )
            range_row_count = range_summary[0]
            self._heatmap_estimated_range_rows = range_row_count
            self._heatmap_source_info.update({
                "estimated_range_rows": range_row_count,
                "strategy": "visible range",
                })
            if range_row_count > MAX_SQL_HEATMAP_SOURCE_ROWS:
                return self._spatially_aggregated_heatmap_arrays(
                    conn,
                    where_sql,
                    parameters,
                    range_summary,
                    )

            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {where_sql} ORDER BY rowid"
                    ),
                parameters,
                )
            return self._arrays_from_cursor(cursor)

        if row_count <= MAX_SQL_HEATMAP_SOURCE_ROWS:
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {finite_where_sql} ORDER BY rowid"
                    ),
                )
            arrays = self._arrays_from_cursor(cursor)
            self._heatmap_source_info["estimated_range_rows"] = int(arrays[2].size)
            return arrays

        summary = self._heatmap_spatial_summary(conn, finite_where_sql, ())
        return self._spatially_aggregated_heatmap_arrays(
            conn,
            finite_where_sql,
            (),
            summary,
            )


    def _has_array_heatmap_columns(self):
        return any(
            getattr(param, "type", None) == "array"
            for param in (
                self.param,
                self.param_dict[self.axes_dict["x"]],
                self.param_dict[self.axes_dict["y"]],
            )
        )


    def _array_heatmap_records(self, conn):
        """Yield one aligned record, without materialising any array BLOB."""
        names = (self.axes_dict["x"], self.axes_dict["y"], self.param.name)
        array_columns = [
            getattr(self.param_dict[name], "type", None) == "array"
            for name in names
        ]
        columns = ", ".join(
            "NULL" if is_array else _sqlite_identifier(name)
            for name, is_array in zip(names, array_columns, strict=True)
        )
        table = _sqlite_identifier(self.table_name)
        selected = f"{_sqlite_identifier(self.param.name)} IS NOT NULL"
        last_rowid = 0
        while True:
            self._check_cancelled()
            # Finish each bounded row query before decoding. Array payloads
            # are opened separately and always with readonly=True.
            rows = conn.execute(
                f"SELECT rowid, {columns} FROM {table} "
                f"WHERE rowid > ? AND {selected} ORDER BY rowid LIMIT 128",
                (last_rowid,),
            ).fetchall()
            if not rows:
                return
            for rowid, *values in rows:
                self._check_cancelled()
                with ExitStack() as stack:
                    for index, (name, is_array) in enumerate(zip(names, array_columns, strict=True)):
                        self._check_cancelled()
                        if is_array:
                            blob = stack.enter_context(conn.blobopen(
                                self.table_name, name, rowid, readonly=True,
                            ))
                            values[index] = _HeatmapArrayReader(blob)
                            if values[index].dtype.kind == "c":
                                self._reject_complex_heatmap(
                                    name,
                                    coordinate=name != self.param.name,
                                )
                    sizes = [value.size if isinstance(value, _HeatmapArrayReader) else 1
                             for value in values]
                    size = sizes[2]
                    if any(value_size not in (1, size) for value_size in sizes):
                        raise ValueError("Heatmap setpoint and measurement shapes do not match.")
                    yield values, size
            last_rowid = rows[-1][0]


    def _array_heatmap_chunks(self, conn):
        chunk_size = max(1, min(CANCELLATION_CHUNK_SIZE, MAX_SQL_HEATMAP_SOURCE_ROWS))
        with closing(self._array_heatmap_records(conn)) as records:
            for values, size in records:
                for start in range(0, size, chunk_size):
                    self._check_cancelled()
                    stop = min(size, start + chunk_size)
                    decoded = [
                        value.read(0 if value.size == 1 else start,
                                   1 if value.size == 1 else stop,
                                   self._check_cancelled)
                        if isinstance(value, _HeatmapArrayReader) else value
                        for value in values
                    ]
                    yield stop - start, self._arrays_from_values(*decoded)


    def _array_heatmap_visible_values(self, arrays):
        x_data, y_data, z_data = arrays
        valid = np.ones(z_data.size, dtype=bool)
        for axis, values in (("x", x_data), ("y", y_data)):
            bounds = self._grid_axis_bounds(axis)
            if bounds is not None:
                valid &= (values >= bounds[0]) & (values <= bounds[1])
        return x_data[valid], y_data[valid], z_data[valid]


    def _read_array_heatmap_arrays(self, conn):
        """Keep a bounded sample buffer, or stream two passes into a mean grid."""
        buffered: list[tuple[np.ndarray, np.ndarray, np.ndarray]] | None = []
        source_count = matching_count = 0
        # Keep exact coordinates only while an axis could fit on screen.
        # Once saturated, retain its extrema and bin it on the second pass.
        axis_limit = max(1, self.max_heatmap_grid_side)
        unique: list[set[float] | None] = [set(), set()]
        precision_sensitive = [False, False]
        lower = [np.inf, np.inf]
        upper = [-np.inf, -np.inf]
        with closing(self._array_heatmap_chunks(conn)) as chunks:
            for size, arrays in chunks:
                self._check_cancelled()
                source_count += size
                arrays = self._array_heatmap_visible_values(arrays)
                matching_count += arrays[2].size
                if buffered is not None:
                    if matching_count <= MAX_SQL_HEATMAP_SOURCE_ROWS:
                        if arrays[2].size:
                            buffered.append(arrays)
                    else:
                        buffered = None
                for axis, values in enumerate(arrays[:2]):
                    if not values.size:
                        continue
                    lower[axis] = min(lower[axis], float(np.min(values)))
                    upper[axis] = max(upper[axis], float(np.max(values)))
                    axis_unique = unique[axis]
                    precision_sensitive[axis] |= self._coordinate_conversion_may_round(values)
                    if axis_unique is not None:
                        axis_unique.update(np.unique(values).tolist())
                        # Check accumulated exact coordinates before the set
                        # is bounded: a collision may span acquisition records.
                        if precision_sensitive[axis]:
                            self._float_heatmap_coordinates(
                                np.array(list(axis_unique), dtype=object),
                                self.axes_dict[("x", "y")[axis]],
                            )
                        if len(axis_unique) > axis_limit:
                            unique[axis] = None
                    elif precision_sensitive[axis]:
                        self._float_heatmap_coordinates(
                            values, self.axes_dict[("x", "y")[axis]],
                        )

        self.total_point_count_estimate = source_count
        self.sampled_heatmap_source = False
        self.aggregated_heatmap_source = buffered is None
        self._heatmap_source_info = {
            "row_count": source_count,
            "estimated_range_rows": matching_count,
            "sampled": False,
            "aggregated": buffered is None,
            "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
            "sample_stride": None,
            "strategy": "spatial mean" if buffered is None else "all",
            "axis_ranges": self._normalised_heatmap_axis_ranges(self.heatmap_axis_ranges),
        }
        if buffered is not None:
            self._check_cancelled()
            return tuple(
                concatenate_record_samples([arrays[axis] for arrays in buffered])
                if buffered else np.array([], dtype=float)
                for axis in range(3)
            )

        counts = tuple(len(values) if values is not None else axis_limit + 1
                       for values in unique)
        bins = self._bounded_grid_shape(*counts)
        exact = [values is not None and len(values) <= count
                 for values, count in zip(unique, bins, strict=True)]
        axes = [
            np.array(sorted(values), dtype=float) if exact[axis] and values is not None
            else self._scaled_axis_indices(
                np.array([lower[axis], upper[axis]]), bins[axis],
                (lower[axis], upper[axis]),
            )[0]
            for axis, values in enumerate(unique)
        ]
        means = FiniteBinMeans((axes[1].size, axes[0].size), self._check_cancelled)

        def accumulate(*, replay=False):
            # Both passes use the same private snapshot, frozen bin geometry,
            # and visible-value filtering. Close each iterator (including its
            # read-only BLOBs) before starting the optional exact replay.
            with closing(self._array_heatmap_chunks(conn)) as chunks:
                for _size, arrays in chunks:
                    self._check_cancelled()
                    x_data, y_data, z_data = self._array_heatmap_visible_values(arrays)
                    indices = [
                        np.searchsorted(axes[axis], values) if exact[axis]
                        else self._scaled_axis_indices(
                            values, bins[axis], (lower[axis], upper[axis]),
                        )[1]
                        for axis, values in enumerate((x_data, y_data))
                    ]
                    if replay:
                        means.replay((indices[1], indices[0]), z_data)
                    else:
                        means.add((indices[1], indices[0]), z_data)

        accumulate()
        if means.begin_exact_replay():
            accumulate(replay=True)

        self._check_cancelled()
        y_indices, x_indices = np.nonzero(means.counts)
        z_data = means.means()[y_indices, x_indices]
        self._spatial_heatmap_axes: tuple[np.ndarray, np.ndarray] = (axes[0], axes[1])
        self._spatial_heatmap_indices: tuple[np.ndarray, np.ndarray] = (x_indices, y_indices)
        self._spatial_heatmap_source_unique_counts: tuple[int, int] = (counts[0], counts[1])
        self._array_heatmap_cardinality_exact = tuple(values is not None for values in unique)
        self._heatmap_aggregated_source_rows: int = matching_count
        full_ranges = self._normalised_heatmap_axis_ranges(self.heatmap_full_axis_ranges)
        if full_ranges is not None:
            self.heatmap_source_axis_ranges = full_ranges
        elif self.heatmap_axis_ranges is None:
            self.heatmap_source_axis_ranges = {
                axis: (lower[index], upper[index]) for index, axis in enumerate(("x", "y"))
            }
        return axes[0][x_indices], axes[1][y_indices], z_data


    def _heatmap_spatial_summary(self, conn, where_sql, parameters):
        self._check_cancelled()
        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        row = conn.execute(
            (
                f"SELECT COUNT(*), MIN({x_column}), MAX({x_column}), "
                f"COUNT(DISTINCT {x_column}), MIN({y_column}), "
                f"MAX({y_column}), COUNT(DISTINCT {y_column}) "
                f"FROM {table} WHERE {where_sql}"
                ),
            parameters,
            ).fetchone()
        if row is None or row[0] is None:
            return (0, None, None, 0, None, None, 0)

        return (
            int(row[0]),
            row[1],
            row[2],
            int(row[3] or 0),
            row[4],
            row[5],
            int(row[6] or 0),
            )


    def _spatially_aggregated_heatmap_arrays(
            self,
            conn,
            where_sql,
            parameters,
            summary,
            ):
        (
            matching_rows,
            x_min,
            x_max,
            x_count,
            y_min,
            y_max,
            y_count,
            ) = summary
        if (
                matching_rows <= 0
                or x_min is None
                or x_max is None
                or y_min is None
                or y_max is None
                or x_count <= 0
                or y_count <= 0
                ):
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            return self._arrays_from_values([], [], [])

        x_bins, y_bins = self._bounded_grid_shape(x_count, y_count)
        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        z_column = _sqlite_identifier(self.param.name)

        x_exact = x_bins >= x_count
        y_exact = y_bins >= y_count
        query_parameters: list[float | int] = []
        if x_exact:
            x_centres = np.array([], dtype=float)
            x_group_sql = x_column
        else:
            x_centres, x_lower_edge, x_scale = self._spatial_axis_bins(
                float(x_min),
                float(x_max),
                x_count,
                x_bins,
                )
            x_group_sql = f"MIN(CAST(({x_column} - ?) * ? AS INTEGER), ?)"
            query_parameters.extend((x_lower_edge, x_scale, x_bins - 1))

        if y_exact:
            y_centres = np.array([], dtype=float)
            y_group_sql = y_column
        else:
            y_centres, y_lower_edge, y_scale = self._spatial_axis_bins(
                float(y_min),
                float(y_max),
                y_count,
                y_bins,
                )
            y_group_sql = f"MIN(CAST(({y_column} - ?) * ? AS INTEGER), ?)"
            query_parameters.extend((y_lower_edge, y_scale, y_bins - 1))

        if (
                (not x_exact and x_centres.size == 0)
                or (not y_exact and y_centres.size == 0)
                ):
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            return self._arrays_from_values([], [], [])

        grouped_source_sql = (
            f"SELECT {x_group_sql} AS x_group, {y_group_sql} AS y_group, "
            f"{z_column} AS z_value FROM {table} WHERE {where_sql}"
        )
        cursor = conn.execute(
            (
                "SELECT x_group, y_group, AVG(z_value), COUNT(*), "
                "MIN(z_value), MAX(z_value) FROM ("
                + grouped_source_sql +
                ") GROUP BY x_group, y_group ORDER BY y_group, x_group"
                ),
            (*query_parameters, *parameters),
            )
        x_groups = []
        y_groups = []
        z_values: list[float | None] = []
        failed_means = {}
        aggregated_source_rows = 0
        for row_number, (x_group, y_group, z_value, bin_rows, z_min, z_max) in enumerate(cursor):
            if row_number % 1024 == 0:
                self._check_cancelled()
            largest = max(abs(float(z_min)), abs(float(z_max)))
            safe_sample = (np.finfo(float).max / 2) / int(bin_rows)
            if z_value is None or not math.isfinite(z_value) or largest > safe_sample:
                failed_means[(x_group, y_group)] = (len(z_values), int(bin_rows))
            x_groups.append(x_group)
            y_groups.append(y_group)
            z_values.append(z_value)
            aggregated_source_rows += int(bin_rows)

        if failed_means:
            # SQLite AVG can lose a cancellation residual before a risky sum
            # overflows. Replay all risky groups from the original values in
            # one bounded pass, even when AVG happened to return finite data.
            totals = dict.fromkeys(failed_means, Fraction())
            cursor = conn.execute(grouped_source_sql, (*query_parameters, *parameters))
            for row_number, (x_group, y_group, value) in enumerate(cursor):
                if row_number % 1024 == 0:
                    self._check_cancelled()
                key = (x_group, y_group)
                if key in totals:
                    totals[key] += Fraction(value)
            for index, (key, total) in enumerate(totals.items()):
                if index % 1024 == 0:
                    self._check_cancelled()
                position, count = failed_means[key]
                z_values[position] = float(total / count)

        x_group_data = np.asarray(x_groups, dtype=float)
        y_group_data = np.asarray(y_groups, dtype=float)
        z_data = np.asarray(z_values, dtype=float)
        finite = (
            np.isfinite(x_group_data)
            & np.isfinite(y_group_data)
            & np.isfinite(z_data)
            )

        if x_exact:
            x_centres = np.unique(x_group_data[np.isfinite(x_group_data)])
            x_index_data = np.searchsorted(x_centres, x_group_data[finite])
        else:
            x_index_data = x_group_data[finite].astype(np.int64)
        if y_exact:
            y_centres = np.unique(y_group_data[np.isfinite(y_group_data)])
            y_index_data = np.searchsorted(y_centres, y_group_data[finite])
        else:
            y_index_data = y_group_data[finite].astype(np.int64)

        z_data = z_data[finite]
        self._spatial_heatmap_axes = (x_centres, y_centres)
        self._spatial_heatmap_indices = (x_index_data, y_index_data)
        self._spatial_heatmap_source_unique_counts = (x_count, y_count)
        self._heatmap_aggregated_source_rows = aggregated_source_rows
        full_axis_ranges = self._normalised_heatmap_axis_ranges(
            self.heatmap_full_axis_ranges
            )
        if full_axis_ranges is not None:
            self.heatmap_source_axis_ranges = full_axis_ranges
        elif self.heatmap_axis_ranges is None:
            self.heatmap_source_axis_ranges = {
                "x": (float(x_min), float(x_max)),
                "y": (float(y_min), float(y_max)),
                }
        self.sampled_heatmap_source = False
        self.aggregated_heatmap_source = True
        self._heatmap_source_info.update({
            "estimated_range_rows": matching_rows,
            "sampled": False,
            "aggregated": True,
            "sample_limit": None,
            "sample_stride": None,
            "strategy": "spatial mean",
            })
        return x_centres[x_index_data], y_centres[y_index_data], z_data


    @staticmethod
    def _spatial_axis_bins(lower, upper, source_count, bin_count):
        if not np.isfinite(lower) or not np.isfinite(upper):
            return np.array([], dtype=float), 0.0, 1.0
        if source_count <= 1 or bin_count <= 1 or lower == upper:
            return np.array([lower], dtype=float), lower, 1.0

        source_step = (upper - lower) / (source_count - 1)
        lower_edge = lower - source_step / 2
        upper_edge = upper + source_step / 2
        bin_width = (upper_edge - lower_edge) / bin_count
        centres = lower_edge + (np.arange(bin_count, dtype=float) + 0.5) * bin_width
        return centres, lower_edge, 1.0 / bin_width


    def _selected_parameter_row_count(self, conn):
        self._check_cancelled()
        table = _sqlite_identifier(self.table_name)
        z_column = _sqlite_identifier(self.param.name)
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {z_column} IS NOT NULL"
            ).fetchone()
        if row is None or row[0] is None:
            return None

        return int(row[0])


    def _heatmap_where_clause(self):
        ranges = self.heatmap_axis_ranges or {}
        clauses: list[str] = []
        parameters: list[float] = []

        for axis in ("x", "y"):
            axis_range = ranges.get(axis)
            if axis_range is None:
                continue

            try:
                low, high = sorted(float(value) for value in axis_range)
            except (TypeError, ValueError):
                continue

            if not (np.isfinite(low) and np.isfinite(high)) or low == high:
                continue

            column = _sqlite_identifier(self.axes_dict[axis])
            clauses.append(f"{column} BETWEEN ? AND ?")
            parameters.extend((low, high))

        return " AND ".join(clauses), parameters


    def _normalised_heatmap_axis_ranges(self, ranges):
        if not ranges:
            return None

        normalised = {}
        for axis in ("x", "y"):
            axis_range = ranges.get(axis)
            if axis_range is None:
                return None

            try:
                low, high = sorted(float(value) for value in axis_range)
            except (TypeError, ValueError):
                return None

            if not (np.isfinite(low) and np.isfinite(high)) or low == high:
                return None

            normalised[axis] = (low, high)

        return normalised


    def _arrays_from_cursor(self, cursor):
        x_values = []
        y_values = []
        z_values = []

        for row_number, (x_value, y_value, z_value) in enumerate(cursor):
            if row_number % 1024 == 0:
                self._check_cancelled()
            x_values.append(x_value)
            y_values.append(y_value)
            z_values.append(z_value)

        return self._arrays_from_values(x_values, y_values, z_values)


    def _reject_complex_heatmap(self, parameter_name=None, *, coordinate=False):
        if parameter_name is None:
            parameter_name = getattr(self.param, "name", "measurement")
        detail = "a coordinate" if coordinate else "measurement data"
        raise ValueError(
            "Complex-valued heatmaps are not supported: "
            f"parameter '{parameter_name}' contains {detail}."
        )


    def _validate_heatmap_parameter_types(self):
        """Reject declared complex columns before any SQLite arithmetic."""

        for axis in ("x", "y"):
            name = self.axes_dict[axis]
            if getattr(self.param_dict[name], "type", None) == "complex":
                self._reject_complex_heatmap(name, coordinate=True)
        if getattr(self.param, "type", None) == "complex":
            self._reject_complex_heatmap()


    def _real_heatmap_values(self, values, parameter_name, *, coordinate=False):
        """Return an array only when conversion to float cannot lose phase."""

        result = np.asarray(values)
        if np.iscomplexobj(result) or (
            result.dtype.kind == "O" and any(np.iscomplexobj(value) for value in result.flat)
        ):
            self._reject_complex_heatmap(
                parameter_name,
                coordinate=coordinate,
            )
        if coordinate and result.dtype.kind == "O":
            # Mixed record dtypes need Python's exact int/float comparisons;
            # NumPy scalar comparisons may promote a large integer to float.
            result = np.frompyfunc(
                lambda value: value.item() if isinstance(value, np.generic) else value,
                1, 1,
            )(result)
        return result


    def _arrays_from_values(self, x_values, y_values, z_values):
        self._check_cancelled()
        x_values = self._real_heatmap_values(
            x_values,
            self.axes_dict["x"],
            coordinate=True,
        )
        self._check_cancelled()
        y_values = self._real_heatmap_values(
            y_values,
            self.axes_dict["y"],
            coordinate=True,
        )
        self._check_cancelled()
        z_values = self._real_heatmap_values(
            z_values,
            getattr(self.param, "name", "measurement"),
        )
        # Broadcast within a decoded record/chunk, before flattening or
        # masking, so a scalar slow setpoint accompanies every array sample.
        x_values, y_values, z_values = np.broadcast_arrays(x_values, y_values, z_values)
        # Keep source coordinates exact until complete records are assembled
        # and their float representation can be checked for merged positions.
        x_data = np.asarray(x_values).reshape(-1)
        self._check_cancelled()
        y_data = np.asarray(y_values).reshape(-1)
        self._check_cancelled()
        z_data = np.asarray(z_values).reshape(-1)
        self._check_cancelled()
        finite = numeric_isfinite(x_data) & numeric_isfinite(y_data) & numeric_isfinite(z_data)
        self._check_cancelled()

        return x_data[finite], y_data[finite], z_data[finite]


    @staticmethod
    def _coordinate_conversion_may_round(values):
        if values.dtype.kind == "O" or values.dtype.itemsize > 8:
            return True
        if values.dtype.kind in "iu" and values.dtype.itemsize == 8:
            return bool(np.any(values > 2**53) or (
                values.dtype.kind == "i" and np.any(values < -(2**53))
            ))
        return False


    def _float_heatmap_coordinates(self, values, parameter_name):
        """Reject loss of recorded coordinates before grouping or operations."""
        self._check_cancelled()
        values = np.asarray(values)
        if values.dtype.kind == "O":
            values = np.frompyfunc(
                lambda value: value.item() if isinstance(value, np.generic) else value,
                1, 1,
            )(values)
        converted = np.asarray(values, dtype=float)
        if self._coordinate_conversion_may_round(values):
            unique = np.unique(values[numeric_isfinite(values)])
            self._check_cancelled()
            displayed = np.asarray(unique, dtype=float)
            if displayed.size > 1 and np.any(displayed[1:] == displayed[:-1]):
                raise ValueError(
                    f"Heatmap coordinate precision is insufficient: distinct values "
                    f"of parameter '{parameter_name}' collapse at floating-point precision."
                )
            # Distinct cells can remain distinct while every centre rounds.
            # That would silently change operations, cursor values and CSV.
            # Require lossless conversion even before streaming sets saturate.
            for index, (original, display) in enumerate(zip(unique, displayed, strict=True)):
                if index % 1024 == 0:
                    self._check_cancelled()
                original = original.item() if isinstance(original, np.generic) else original
                if original != float(display):
                    raise ValueError(
                        f"Heatmap coordinate precision is insufficient: parameter "
                        f"'{parameter_name}' cannot be represented exactly as "
                        "heatmap coordinates."
                    )
        self._check_cancelled()
        return converted


    def _heatmap_grid_from_arrays(
            self,
            x_data,
            y_data,
            z_data,
            *,
            max_cells=None,
            ):
        self._check_cancelled()
        axes = getattr(self, "axes_dict", {})
        x_data = self._float_heatmap_coordinates(x_data, axes.get("x", "x"))
        y_data = self._float_heatmap_coordinates(y_data, axes.get("y", "y"))
        # This path intentionally aggregates a bounded display grid. Preserve
        # exact samples in the full-resolution path until operations finish.
        z_data = np.asarray(z_data, dtype=float)
        display_limit = max(1, int(getattr(
            self, "max_heatmap_grid_cells", MAX_SQL_HEATMAP_GRID_CELLS,
            )))
        max_cells = (
            display_limit if max_cells is None
            else max(1, min(int(max_cells), display_limit))
            )
        if z_data.size == 0:
            self._heatmap_grid_info = {
                "unique_x_count": 0,
                "unique_y_count": 0,
                "exact_cell_count": 0,
                "grid_columns": 0,
                "grid_rows": 0,
                "grid_cell_count": 0,
                "grid_binned": False,
                "grid_cell_limit": max_cells,
                "empty_bins_filled": False,
                }
            return (
                np.array([], dtype=float),
                np.array([], dtype=float),
                np.empty((0, 0), dtype=float),
                )

        unique_x = np.unique(x_data)
        self._check_cancelled()
        unique_y = np.unique(y_data)
        self._check_cancelled()
        exact_cells = unique_x.size * unique_y.size
        source_grid_shape = getattr(self, "heatmap_source_grid_shape", None)
        if source_grid_shape is None:
            if getattr(self, "aggregated_heatmap_source", False):
                source_x_count, source_y_count = (
                    self._spatial_heatmap_source_unique_counts
                    )
                source_grid_rows = int(source_y_count)
                source_grid_columns = int(source_x_count)
            else:
                source_grid_rows = int(unique_y.size)
                source_grid_columns = int(unique_x.size)
        else:
            source_grid_rows, source_grid_columns = source_grid_shape
        source_grid_cell_count = int(source_grid_rows * source_grid_columns)
        if getattr(self, "aggregated_heatmap_source", False):
            return self._spatial_aggregation_grid(
                z_data,
                source_grid_rows,
                source_grid_columns,
                )

        if (
                not getattr(self, "sampled_heatmap_source", False)
                and self._bounded_grid_shape(
                    unique_x.size, unique_y.size, max_cells=max_cells,
                    ) == (unique_x.size, unique_y.size)
                ):
            x_axis, y_axis, data_grid = self._unique_heatmap_grid(
                x_data,
                y_data,
                z_data,
                unique_x,
                unique_y,
                )
            self._heatmap_grid_info = {
                "unique_x_count": int(unique_x.size),
                "unique_y_count": int(unique_y.size),
                "exact_cell_count": int(exact_cells),
                "source_grid_columns": int(source_grid_columns),
                "source_grid_rows": int(source_grid_rows),
                "source_grid_cell_count": int(source_grid_cell_count),
                "grid_columns": int(unique_x.size),
                "grid_rows": int(unique_y.size),
                "grid_cell_count": int(exact_cells),
                "grid_binned": False,
                "grid_cell_limit": max_cells,
                "empty_bins_filled": False,
                }
            return x_axis, y_axis, data_grid

        if getattr(self, "sampled_heatmap_source", False):
            max_cells = min(
                max_cells,
                max(1, int(z_data.size) // SQL_HEATMAP_SAMPLES_PER_CELL),
                )
            (
                x_axis,
                y_axis,
                data_grid,
                empty_bins_filled,
            ) = self._sampled_overview_grid(
                x_data,
                y_data,
                z_data,
                unique_x,
                unique_y,
                max_cells=max_cells,
                )
        else:
            x_axis, y_axis, data_grid = self._binned_heatmap_grid(
                x_data,
                y_data,
                z_data,
                unique_x,
                unique_y,
                max_cells=max_cells,
                )
            empty_bins_filled = False
        self._heatmap_grid_info = {
            "unique_x_count": int(unique_x.size),
            "unique_y_count": int(unique_y.size),
            "exact_cell_count": int(exact_cells),
            "source_grid_columns": int(source_grid_columns),
            "source_grid_rows": int(source_grid_rows),
            "source_grid_cell_count": int(source_grid_cell_count),
            "grid_columns": int(x_axis.size),
            "grid_rows": int(y_axis.size),
            "grid_cell_count": int(data_grid.size),
            "grid_binned": True,
            "grid_cell_limit": int(max_cells),
            "empty_bins_filled": empty_bins_filled,
            }
        return x_axis, y_axis, data_grid


    def _spatial_aggregation_grid(
            self,
            z_data,
            source_grid_rows,
            source_grid_columns,
            ):
        self._check_cancelled()
        x_axis, y_axis = self._spatial_heatmap_axes
        x_indices, y_indices = self._spatial_heatmap_indices
        data_grid = np.full(
            (y_axis.size, x_axis.size),
            np.nan,
            dtype=float,
            )
        data_grid[y_indices, x_indices] = z_data
        self._check_cancelled()

        source_x_count, source_y_count = self._spatial_heatmap_source_unique_counts
        source_grid_cell_count = int(source_grid_rows * source_grid_columns)
        self._heatmap_grid_info = {
            "unique_x_count": int(source_x_count),
            "unique_y_count": int(source_y_count),
            "exact_cell_count": int(source_x_count * source_y_count),
            "source_grid_columns": int(source_grid_columns),
            "source_grid_rows": int(source_grid_rows),
            "source_grid_cell_count": source_grid_cell_count,
            "grid_columns": int(x_axis.size),
            "grid_rows": int(y_axis.size),
            "grid_cell_count": int(data_grid.size),
            "grid_binned": (
                x_axis.size < source_x_count or y_axis.size < source_y_count
                ),
            "grid_cell_limit": getattr(
                self,
                "max_heatmap_grid_cells",
                MAX_SQL_HEATMAP_GRID_CELLS,
                ),
            # Spatial aggregation averages only observations assigned to each
            # cell.  Empty cells represent coordinate pairs that were never
            # measured and must remain missing.
            "empty_bins_filled": False,
            }
        # Saturated array axes carry only a lower cardinality bound for
        # choosing bins. Do not present that bound as an exact source size.
        cardinality_exact = getattr(self, "_array_heatmap_cardinality_exact", (True, True))
        for axis, dimension, is_exact in zip(
                ("x", "y"), ("columns", "rows"), cardinality_exact, strict=True):
            if not is_exact:
                self._heatmap_grid_info[f"unique_{axis}_count"] = None
                self._heatmap_grid_info["exact_cell_count"] = None
                if self.heatmap_source_grid_shape is None:
                    self._heatmap_grid_info[f"source_grid_{dimension}"] = None
                    self._heatmap_grid_info["source_grid_cell_count"] = None
        return x_axis, y_axis, data_grid


    def _heatmap_downsample_info(self):
        source_info = getattr(self, "_heatmap_source_info", None) or {}
        grid_info = getattr(self, "_heatmap_grid_info", None) or {}
        source_sampled = bool(source_info.get("sampled", False))
        source_aggregated = bool(source_info.get("aggregated", False))
        grid_reduced = bool(grid_info.get("grid_binned", False))
        source_grid_columns = grid_info.get("source_grid_columns")
        source_grid_rows = grid_info.get("source_grid_rows")
        grid_columns = grid_info.get("grid_columns")
        grid_rows = grid_info.get("grid_rows")
        if (
                source_grid_columns is not None
                and source_grid_rows is not None
                and grid_columns is not None
                and grid_rows is not None
                ):
            grid_reduced = grid_reduced or (
                int(grid_columns) < int(source_grid_columns)
                or int(grid_rows) < int(source_grid_rows)
                )
        if not source_sampled and not source_aggregated and not grid_reduced:
            return None

        return {
            "source_row_count": source_info.get("row_count"),
            "estimated_range_rows": source_info.get("estimated_range_rows"),
            "loaded_point_count": getattr(self, "loaded_point_count", None),
            "source_sampled": source_sampled,
            "source_aggregated": source_aggregated,
            "aggregated_source_row_count": getattr(
                self,
                "_heatmap_aggregated_source_rows",
                None,
                ),
            "source_sample_limit": source_info.get("sample_limit"),
            "source_sample_stride": source_info.get("sample_stride"),
            "source_sample_strategy": (
                source_info.get("strategy") if source_sampled else None
                ),
            "source_aggregation_strategy": (
                source_info.get("strategy") if source_aggregated else None
                ),
            "axis_ranges": source_info.get("axis_ranges"),
            "unique_x_count": grid_info.get("unique_x_count"),
            "unique_y_count": grid_info.get("unique_y_count"),
            "exact_cell_count": grid_info.get("exact_cell_count"),
            "source_grid_columns": source_grid_columns,
            "source_grid_rows": source_grid_rows,
            "source_grid_cell_count": grid_info.get("source_grid_cell_count"),
            "grid_columns": grid_info.get("grid_columns"),
            "grid_rows": grid_info.get("grid_rows"),
            "grid_cell_count": grid_info.get("grid_cell_count"),
            "grid_binned": grid_reduced,
            "grid_cell_limit": grid_info.get("grid_cell_limit"),
            "full_resolution_point_limit": getattr(
                self,
                "max_full_heatmap_points",
                MAX_FULL_HEATMAP_POINTS,
                ),
            "empty_bins_filled": bool(grid_info.get("empty_bins_filled", False)),
            }


    def _heatmap_source_grid_shape_from_metadata(self):
        try:
            shape = self._parameter_shape()
        except (AttributeError, TypeError, ValueError):
            return None

        if shape is None:
            return None

        try:
            dimensions = [int(dimension) for dimension in shape]
        except (TypeError, ValueError):
            return None

        depends_on = list(getattr(self.param, "depends_on_", ()))
        if len(dimensions) != len(depends_on):
            return None

        try:
            x_dimension = depends_on.index(self.axes_dict["x"])
            y_dimension = depends_on.index(self.axes_dict["y"])
        except (KeyError, ValueError):
            return None

        if (
                x_dimension >= len(dimensions)
                or y_dimension >= len(dimensions)
                or dimensions[x_dimension] <= 0
                or dimensions[y_dimension] <= 0
                ):
            return None

        return dimensions[y_dimension], dimensions[x_dimension]


    def _unique_heatmap_grid(self, x_data, y_data, z_data, unique_x, unique_y):
        self._check_cancelled()
        x_index = np.searchsorted(unique_x, x_data)
        self._check_cancelled()
        y_index = np.searchsorted(unique_y, y_data)
        means = FiniteBinMeans((unique_y.size, unique_x.size), self._check_cancelled)
        for start in range(0, z_data.size, CANCELLATION_CHUNK_SIZE):
            self._check_cancelled()
            stop = start + CANCELLATION_CHUNK_SIZE
            indices = (y_index[start:stop], x_index[start:stop])
            means.add(indices, z_data[start:stop])
        if means.begin_exact_replay():
            for start in range(0, z_data.size, CANCELLATION_CHUNK_SIZE):
                self._check_cancelled()
                stop = start + CANCELLATION_CHUNK_SIZE
                means.replay((y_index[start:stop], x_index[start:stop]), z_data[start:stop])
        data_grid = means.means()

        return unique_x, unique_y, data_grid


    def _binned_heatmap_grid(
            self,
            x_data,
            y_data,
            z_data,
            unique_x,
            unique_y,
            max_cells=None,
            ):
        self._check_cancelled()
        x_bins, y_bins = self._bounded_grid_shape(
            unique_x.size,
            unique_y.size,
            max_cells=max_cells,
            )
        x_centres, x_index = self._heatmap_axis_indices(
            x_data,
            unique_x,
            x_bins,
            self._grid_axis_bounds("x"),
            )
        y_centres, y_index = self._heatmap_axis_indices(
            y_data,
            unique_y,
            y_bins,
            self._grid_axis_bounds("y"),
            )
        self._check_cancelled()

        means = FiniteBinMeans((y_centres.size, x_centres.size), self._check_cancelled)
        for start in range(0, z_data.size, CANCELLATION_CHUNK_SIZE):
            self._check_cancelled()
            stop = start + CANCELLATION_CHUNK_SIZE
            indices = (y_index[start:stop], x_index[start:stop])
            means.add(indices, z_data[start:stop])

        if means.begin_exact_replay():
            for start in range(0, z_data.size, CANCELLATION_CHUNK_SIZE):
                self._check_cancelled()
                stop = start + CANCELLATION_CHUNK_SIZE
                means.replay((y_index[start:stop], x_index[start:stop]), z_data[start:stop])

        self._check_cancelled()
        data_grid = means.means()

        return x_centres, y_centres, data_grid


    def _heatmap_axis_indices(self, values, unique_values, bin_count, bounds=None):
        """Preserve an axis exactly unless its own cardinality needs binning."""

        self._check_cancelled()
        if unique_values.size <= bin_count:
            indices = np.searchsorted(unique_values, values)
            self._check_cancelled()
            return unique_values, indices

        return self._scaled_axis_indices(values, bin_count, bounds)


    def _sampled_overview_grid(
            self,
            x_data,
            y_data,
            z_data,
            unique_x,
            unique_y,
            *,
            max_cells,
            ):
        """Build a sampled overview whose display-only gaps are interpolated."""

        x_axis, y_axis, data_grid = self._binned_heatmap_grid(
            x_data,
            y_data,
            z_data,
            unique_x,
            unique_y,
            max_cells=max_cells,
            )
        empty_bins_filled = bool(
            np.any(~np.isfinite(data_grid))
            and np.any(np.isfinite(data_grid))
            )
        if empty_bins_filled:
            data_grid = self._fill_empty_heatmap_bins(data_grid)

        return x_axis, y_axis, data_grid, empty_bins_filled


    def _fill_empty_heatmap_bins(self, data_grid):
        self._check_cancelled()
        if data_grid.size == 0 or np.all(np.isfinite(data_grid)):
            return data_grid
        if not np.any(np.isfinite(data_grid)):
            return data_grid

        filled = np.array(data_grid, dtype=float, copy=True)
        row_positions = np.arange(filled.shape[0], dtype=float)
        column_positions = np.arange(filled.shape[1], dtype=float)

        for column in range(filled.shape[1]):
            self._check_cancelled()
            values = filled[:, column]
            finite = np.isfinite(values)
            if np.any(finite) and not np.all(finite):
                values[~finite] = np.interp(
                    row_positions[~finite],
                    row_positions[finite],
                    values[finite],
                    )

        for row in range(filled.shape[0]):
            self._check_cancelled()
            values = filled[row, :]
            finite = np.isfinite(values)
            if np.any(finite) and not np.all(finite):
                values[~finite] = np.interp(
                    column_positions[~finite],
                    column_positions[finite],
                    values[finite],
                    )

        return filled


    def _grid_axis_bounds(self, axis):
        ranges = getattr(self, "heatmap_axis_ranges", None) or {}
        axis_range = ranges.get(axis)
        if axis_range is None:
            return None

        try:
            low, high = sorted(float(value) for value in axis_range)
        except (TypeError, ValueError):
            return None

        if not (np.isfinite(low) and np.isfinite(high)) or low == high:
            return None

        return low, high


    def _bounded_grid_shape(self, x_count, y_count, max_cells=None):
        max_cells = (
            getattr(self, "max_heatmap_grid_cells", MAX_SQL_HEATMAP_GRID_CELLS)
            if max_cells is None
            else int(max_cells)
            )
        max_cells = max(1, max_cells)
        max_side = getattr(
            self,
            "max_heatmap_grid_side",
            MAX_SQL_HEATMAP_GRID_SIDE,
            )
        x_bins = max(1, min(int(x_count), max_side))
        y_bins = max(1, min(int(y_count), max_side))

        if x_bins * y_bins <= max_cells:
            return x_bins, y_bins

        scale = math.sqrt(max_cells / (x_bins * y_bins))
        x_bins = max(1, int(x_bins * scale))
        y_bins = max(1, int(y_bins * scale))

        while x_bins * y_bins > max_cells:
            if x_bins >= y_bins and x_bins > 1:
                x_bins -= 1
            elif y_bins > 1:
                y_bins -= 1
            else:
                break

        return x_bins, y_bins


    def _scaled_axis_indices(self, values, bin_count, bounds=None):
        self._check_cancelled()
        if bounds is None:
            lower = float(np.nanmin(values))
            upper = float(np.nanmax(values))
        else:
            lower, upper = bounds

        if not np.isfinite(lower) or not np.isfinite(upper):
            return np.array([], dtype=float), np.array([], dtype=np.int64)

        if lower == upper or bin_count <= 1:
            return (
                np.array([lower], dtype=float),
                np.zeros(values.size, dtype=np.int64),
                )

        scaled = (values - lower) / (upper - lower)
        self._check_cancelled()
        indices = np.floor(scaled * bin_count).astype(np.int64)
        self._check_cancelled()
        indices = np.clip(indices, 0, bin_count - 1)
        step = (upper - lower) / bin_count
        centres = lower + (np.arange(bin_count, dtype=float) + 0.5) * step

        return centres, indices


    def _reject_complex_line(self, parameter_name):
        detail = (
            "measurement data" if parameter_name == self.param.name
            else "a coordinate"
        )
        raise ValueError(
            "Complex-valued 1D plots are not supported: "
            f"parameter '{parameter_name}' contains {detail}."
        )


    def _real_line_values(self, values, parameter_name):
        """Reject complex samples without converting or changing raw data."""

        result = np.asarray(values)
        if np.iscomplexobj(result) or (
            result.dtype.kind == "O" and any(np.iscomplexobj(value) for value in result.flat)
        ):
            self._reject_complex_line(parameter_name)
        return result


    def for_1d(self, data, valid_rows):
        self._check_cancelled()
        axis_data = {}
        axis_param = {}
        x_name = self.axes_dict["x"]
        y_name = (
            self.param.name
            if x_name != self.param.name
            else self.param.depends_on_[0]
        )
        # Flatten both arrays with the same mask in QCoDeS record order.
        # Sorting or uniquing either axis would break sample alignment.
        x_values = self._real_line_values(data[x_name], x_name)
        y_values = self._real_line_values(data[y_name], y_name)
        if x_values.shape != y_values.shape:
            raise ValueError("1D setpoint and measurement shapes do not match")
        valid_rows = (
            np.asarray(valid_rows)
            & ~numeric_isnan(x_values)
            & ~numeric_isnan(y_values)
        )
        axis_data["x"] = x_values[valid_rows].reshape(-1)
        self._check_cancelled()
        axis_param["x"] = self.param_dict[x_name]
        axis_data["y"] = y_values[valid_rows].reshape(-1)
        self._check_cancelled()
        axis_param["y"] = self.param_dict[y_name]
        
        return axis_data, axis_param
        
    
    def for_shaped_2d(self, data, depvarData, *, acquired=None):
        self._check_cancelled()
        axis_data = {}
        axis_param = {}
        axis_dimension = {}
        valid = {}
        shaped_axes_are_rectilinear = True
        depvarData = self._real_heatmap_values(
            depvarData,
            getattr(self.param, "name", "measurement"),
        )
        if acquired is not None and not np.all(acquired):
            # Plotting-only missing values preserve the planned shape and
            # source dtypes. Never infer acquisition from zeros or completion.
            depvarData = depvarData.astype(
                object if depvarData.dtype.kind in "iuO" else depvarData.dtype,
                copy=True,
            )
            depvarData[~acquired] = np.nan
        self._check_cancelled()
        
        # Find correct data for each axis
        for axis in ["x", "y"]:
            self._check_cancelled()
            name = self.axes_dict[axis]
            param = self.param_dict[name]

            param_data = self._real_heatmap_values(
                data[name],
                name,
                coordinate=True,
            )
            if acquired is not None and not np.all(acquired):
                # Unwritten coordinates must not choose axis representatives
                # or make a rectilinear acquired prefix appear serpentine.
                if param_data.dtype.kind in "iuO":
                    param_data = param_data.astype(object)
                param_data = np.where(acquired, param_data, np.nan)
            param_data = self._float_heatmap_coordinates(param_data, name)
            dimension = self._shaped_axis_dimension(name, param_data, depvarData)
            shaped_axes_are_rectilinear &= self._shaped_axis_is_rectilinear(
                param_data,
                dimension,
                )
            param_data = self._shaped_axis_values(param_data, dimension)

            valid[axis] = np.isfinite(param_data)
            axis_data[axis] = param_data[valid[axis]]
            axis_param[axis] = param
            axis_dimension[axis] = dimension

        # Snake scans and repeated setpoints need coordinate-based assembly.
        # In particular, repeated cells must retain the unshaped path's mean.
        if (
                not shaped_axes_are_rectilinear
                or axis_dimension["x"] == axis_dimension["y"]
                or self._requires_bounded_heatmap(
                    int(axis_data["x"].size) * int(axis_data["y"].size)
                    )
                or any(
                    np.unique(axis_data[axis]).size != axis_data[axis].size
                    for axis in ("x", "y")
                    )
                ):
            valid_rows = numeric_isfinite(depvarData)
            for axis in ("x", "y"):
                name = self.axes_dict[axis]
                valid_rows &= np.isfinite(np.asarray(data[name], dtype=float))
            return self.for_unshaped_2d(data, valid_rows, depvarData)

        dataGrid = self._shaped_data_grid(
            data,
            depvarData,
            axis_dimension,
            valid,
            )
        
        return axis_data, axis_param, dataGrid


    def _shaped_axis_dimension(self, name, param_data, depvarData):
        depends_on = list(getattr(self.param, "depends_on_", ()))
        if (
                param_data.shape == depvarData.shape
                and len(depends_on) == depvarData.ndim
                and name in depends_on
                ):
            return depends_on.index(name)

        residuals = [
            self._shaped_axis_residual(param_data, dimension)
            for dimension in range(depvarData.ndim)
            ]
        return int(np.nanargmin(residuals))


    def _shaped_axis_values(self, param_data, dimension):
        """Choose axis representatives without implying validity of samples."""
        self._check_cancelled()
        moved = np.moveaxis(param_data, dimension, 0)
        rows = moved.reshape(moved.shape[0], -1)
        values = np.full(rows.shape[0], np.nan, dtype=float)

        for index, row in enumerate(rows):
            if index % 1024 == 0:
                self._check_cancelled()
            finite = np.flatnonzero(np.isfinite(row))
            if finite.size:
                values[index] = row[finite[0]]

        return values


    def _shaped_axis_residual(self, param_data, dimension):
        self._check_cancelled()
        values = self._shaped_axis_values(param_data, dimension)
        shape = [1] * param_data.ndim
        shape[dimension] = values.size
        expected = np.broadcast_to(values.reshape(shape), param_data.shape)
        valid = np.isfinite(param_data) & np.isfinite(expected)
        if not np.any(valid):
            return np.inf

        return float(np.nanmax(np.abs(param_data[valid] - expected[valid])))


    def _shaped_axis_is_rectilinear(self, param_data, dimension):
        """Check finite coordinates; missing samples can still be rectilinear."""
        self._check_cancelled()
        values = self._shaped_axis_values(param_data, dimension)
        shape = [1] * param_data.ndim
        shape[dimension] = values.size
        expected = np.broadcast_to(values.reshape(shape), param_data.shape)
        valid = np.isfinite(param_data) & np.isfinite(expected)
        if not np.any(valid):
            return True

        # Compare positions relative to the sweep itself.  Using a relative
        # tolerance on the raw coordinates makes the tolerance grow with an
        # arbitrary offset (for example, a GHz carrier), and can therefore
        # hide a genuine sub-Hz serpentine reversal.
        finite_values = values[np.isfinite(values)]
        origin = finite_values[0]
        centred_values = finite_values - origin
        span = float(np.max(np.abs(centred_values)))
        if not np.isfinite(span) or span == 0:
            span = 1.0

        return bool(np.all(np.isclose(
            (param_data[valid] - origin) / span,
            (expected[valid] - origin) / span,
            rtol=1e-10,
            atol=1e-12,
            )))


    def _shaped_data_grid(self, data, depvarData, axis_dimension, valid):
        self._check_cancelled()
        x_dimension = axis_dimension["x"]
        y_dimension = axis_dimension["y"]

        # Axis representatives may come from another row or column. Retain
        # each sample's recorded coordinate validity before using those axes
        # to publish the grid or pass it to operations.
        valid_rows = numeric_isfinite(depvarData)
        for axis in ["x", "y"]:
            self._check_cancelled()
            name = self.axes_dict[axis]
            valid_rows &= np.isfinite(np.asarray(data[name], dtype=float))
        self._check_cancelled()

        if {x_dimension, y_dimension} == {0, 1}:
            selection = (
                np.ix_(valid["y"], valid["x"])
                if x_dimension == 1
                else np.ix_(valid["x"], valid["y"])
                )
            # Advanced indexing creates a private grid, leaving the QCoDeS
            # cache intact for later loads and different axis selections.
            data_grid = depvarData[selection]
            self._check_cancelled()
            invalid = ~valid_rows[selection]
            if np.any(invalid):
                if data_grid.dtype.kind in "iu":
                    data_grid = data_grid.astype(object)
                data_grid[invalid] = np.nan
            self._check_cancelled()
            return data_grid if x_dimension == 1 else data_grid.transpose()

        return self.for_unshaped_2d(data, valid_rows, depvarData)[2]
    
    
    def for_unshaped_2d(self, data, valid_rows, depvarData):
        self._check_cancelled()
        axis_data = {}
        axis_param = {}
        for axis in ["x", "y"]:
            self._check_cancelled()
            # Get specific parameter
            name = self.axes_dict[axis]
            param = self.param_dict[name]
            
            # Update data
            axis_data[axis] = data[name][valid_rows]
            axis_param[axis] = param

        x_data, y_data, z_data = self._arrays_from_values(
            axis_data["x"], axis_data["y"], depvarData[valid_rows],
            )
        x_data = self._float_heatmap_coordinates(x_data, self.axes_dict["x"])
        y_data = self._float_heatmap_coordinates(y_data, self.axes_dict["y"])
        # Source rows and planned shapes do not bound a pivot: even a short
        # diagonal scan creates unique-X * unique-Y cells. Count only paired,
        # valid samples, including when a shaped scan falls back to this path.
        x_count = int(np.unique(x_data).size)
        self._check_cancelled()
        y_count = int(np.unique(y_data).size)
        self._check_cancelled()
        if self._requires_bounded_heatmap(x_count * y_count):
            self.heatmap_source_grid_shape = (y_count, x_count)
            self.heatmap_source_axis_ranges = {
                "x": (float(np.min(x_data)), float(np.max(x_data))),
                "y": (float(np.min(y_data)), float(np.max(y_data))),
                }
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            x_axis, y_axis, data_grid = self._heatmap_grid_from_arrays(
                x_data, y_data, z_data,
                max_cells=getattr(
                    self, "max_full_heatmap_points", MAX_FULL_HEATMAP_POINTS,
                    ),
                )
            self.loaded_point_count = int(z_data.size)
            self._heatmap_aggregated_source_rows = int(z_data.size)
            self._heatmap_source_info = {
                "row_count": int(z_data.size),
                "estimated_range_rows": int(z_data.size),
                "sampled": False,
                "aggregated": True,
                "strategy": "spatial mean",
                "axis_ranges": None,
                }
            self.heatmap_downsample_info = self._heatmap_downsample_info()
            return {"x": x_axis, "y": y_axis}, axis_param, data_grid

        dataGrid = data2matrix(
                y_data,
                x_data,
                z_data,
                check_cancelled=self._check_cancelled,
            )
        self._check_cancelled()
        
        # remove duplicates
        axis_data["y"] = dataGrid.index.to_numpy(float)
        axis_data["x"] = dataGrid.columns.to_numpy(float)
        
        dataGrid = dataGrid.to_numpy()
        
        return axis_data, axis_param, dataGrid
        
    
    def do_operations(self):
        """
        Runs through all functions in self.operations and performs those on the
        data.
        Work is performed on copies so a failure cannot return partial output.

        Returns
        -------
        data_dict["x"], data_dict["y"], data_dict["z"] : np.ndarray
            The updated data after all operations have been performed
        None : NoneType
            No operations to perform.
    
        """
        operations = self.operations
        if len(operations) == 0:
            return None

        self._check_cancelled()
        data_dict = {
            "x" : self.axis_data["x"].copy(),
            "y" : None,
            "z" : None,
            }
        self._check_cancelled()
        data_dict["y"] = self.axis_data["y"].copy()
        self._check_cancelled()
        if hasattr(self, "dataGrid"):
            data_dict["z"] = self.dataGrid.copy()
        self._check_cancelled()

        for operation in operations:
            self._check_cancelled()
            try:
                if isinstance(operation, OperationCall):
                    results = operation.execute(data_dict, self.is_cancelled)
                else:
                    # Backwards compatibility: arbitrary operations continue
                    # to receive exactly one data dictionary argument. Such a
                    # call can only be cancelled after it returns.
                    results = operation(data_dict)
                self._check_cancelled()
                for key in results.keys():
                    data_dict[key] = results[key]
            except Exception as err:
                if self.is_cancelled():
                    raise PlotWorkCancelled("Plot load cancelled.") from err
                name = getattr(operation, "name", None)
                description = f' "{name}"' if name else ""
                raise OperationExecutionError(
                    f"Operation{description} failed: {err}"
                    ) from err

        return data_dict["x"], data_dict["y"], data_dict["z"]


    def _apply_operation_metadata(self):
        """Update the dependent-variable label and unit after derivatives."""

        is_line_plot = len(getattr(self.param, "depends_on_", ())) == 1
        if is_line_plot:
            self.display_param = copy(self.axis_param["y"])

        for operation in self.operations:
            axis_name = getattr(operation, "derivative_axis", None)
            if axis_name not in ("x", "y"):
                continue

            axis_param = self.axis_param[axis_name]
            value_label = (
                getattr(self.display_param, "label", "")
                or getattr(self.display_param, "name", "")
                )
            axis_label = (
                getattr(axis_param, "label", "")
                or getattr(axis_param, "name", axis_name)
                )
            self.display_param.label = f"d({value_label})/d({axis_label})"

            value_unit = getattr(self.display_param, "unit", "")
            axis_unit = getattr(axis_param, "unit", "")
            if value_unit and axis_unit:
                self.display_param.unit = f"{value_unit}/{axis_unit}"
            elif axis_unit:
                self.display_param.unit = f"1/{axis_unit}"

        if is_line_plot:
            self.axis_param["y"] = self.display_param


    def _aggregate_operated_heatmap_if_needed(self):
        """Reduce an operated raw heatmap to the configured display limit."""

        self._check_cancelled()
        if not self.operations or not hasattr(self, "dataGrid"):
            return

        data_grid = np.asarray(self.dataGrid, dtype=float)
        self._check_cancelled()
        if data_grid.size <= self.max_full_heatmap_points:
            return

        x_axis = np.asarray(self.axis_data["x"], dtype=float)
        self._check_cancelled()
        y_axis = np.asarray(self.axis_data["y"], dtype=float)
        if data_grid.shape != (y_axis.size, x_axis.size):
            raise ValueError(
                "Operated heatmap dimensions do not match its coordinate axes."
                )

        source_rows, source_columns = data_grid.shape
        self.heatmap_source_grid_shape = (source_rows, source_columns)
        self.heatmap_source_axis_ranges = {
            "x": (float(np.nanmin(x_axis)), float(np.nanmax(x_axis))),
            "y": (float(np.nanmin(y_axis)), float(np.nanmax(y_axis))),
            }
        x_data = np.tile(x_axis, y_axis.size)
        self._check_cancelled()
        y_data = np.repeat(y_axis, x_axis.size)
        self._check_cancelled()
        z_data = data_grid.reshape(-1)
        finite = np.isfinite(x_data) & np.isfinite(y_data) & np.isfinite(z_data)
        self._check_cancelled()
        x_data = x_data[finite]
        y_data = y_data[finite]
        z_data = z_data[finite]

        max_cells = min(
            self.max_full_heatmap_points,
            getattr(
                self,
                "max_heatmap_grid_cells",
                MAX_SQL_HEATMAP_GRID_CELLS,
                ),
            )
        x_axis, y_axis, data_grid = self._heatmap_grid_from_arrays(
            x_data,
            y_data,
            z_data,
            max_cells=max_cells,
            )
        source_count = int(source_rows * source_columns)
        self._heatmap_aggregated_source_rows = int(z_data.size)
        self._heatmap_source_info = {
            "row_count": source_count,
            "estimated_range_rows": source_count,
            "sampled": False,
            "aggregated": True,
            "sample_limit": None,
            "sample_stride": None,
            "strategy": "operations, then spatial mean",
            "axis_ranges": self.heatmap_source_axis_ranges,
            }
        self.axis_data["x"] = x_axis
        self.axis_data["y"] = y_axis
        self.dataGrid = data_grid
        self.loaded_point_count = int(data_grid.size)
        self.heatmap_downsample_info = self._heatmap_downsample_info()

        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
