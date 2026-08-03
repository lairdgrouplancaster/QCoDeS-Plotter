import math
import threading
from copy import copy
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6 import QtCore

from qplot.datahandling import load_param_data_from_db, load_param_data_from_db_prep
from qplot.datahandling.dimensions import ensure_supported_plot_dimensions
from qplot.datahandling.qcodes_cache import (
    cache_database_path,
    cache_is_live,
    cache_parameter_data,
    cache_rundescriber,
    cache_table_name,
    set_parameter_complete,
    snapshot_cache_parameter_state,
)
from qplot.datahandling.readonly import (
    qcodes_read_only_connection,
    sqlite_read_only_connection,
)
from qplot.diagnostics import log_exception
from qplot.tools.operation_registry import OperationCall, OperationExecutionError

from . import data2matrix
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
                 heatmap_axis_ranges: dict | None = None,
                 heatmap_full_axis_ranges: dict | None = None,
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
        self._sql_connection = None
        
        # Required working data
        self.cache = cache
        self.table_name = cache_table_name(cache)
        self.param = param
        self.display_param = copy(param)
        self.param_dict = param_dict
        
        self.axes_dict = axes
        self.read_data = read_data
        self.operations = [] if operations is None else operations
        self.force_sql_heatmap = force_sql_heatmap
        self.max_full_heatmap_points = max(1, int(max_full_heatmap_points))
        self.heatmap_axis_ranges = heatmap_axis_ranges
        self.heatmap_full_axis_ranges = heatmap_full_axis_ranges
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


    def cancel(self) -> None:
        """Request cooperative cancellation and interrupt an active SQL read."""

        self._ensure_cancel_state()
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


    def _emit_finished(self, finished: bool) -> None:
        """Emit completion unless Qt already deleted the receiver at shutdown."""

        try:
            self.emitter.finished.emit(finished)
        except RuntimeError as err:
            message = str(err)
            if not ("wrapped C/C++ object" in message and "has been deleted" in message):
                raise


    def _finish_cancelled(self) -> None:
        self.running = False
        self._emit_finished(False)


    def run(self):
        try:
            self._check_cancelled()
            ensure_supported_plot_dimensions(
                getattr(self.param, "name", "Measurement"),
                getattr(self.param, "depends_on_", ()),
            )
            cache = self.cache
            cache_live = cache_is_live(cache)

            # Completion checks and cache preparation can query SQLite, so do
            # them here rather than on the GUI thread. In-memory live caches
            # are already authoritative and should never be read via SQLite.
            if self.read_data:
                if cache_live:
                    self.read_data = False
                else:
                    completion_conn = qcodes_read_only_connection(
                        cache_database_path(cache)
                        )
                    self._set_sql_connection(completion_conn)
                    try:
                        self._check_cancelled()
                        complete = load_param_data_from_db_prep(
                            cache,
                            self.param,
                            connection=completion_conn,
                            )
                    finally:
                        self._close_sql_connection(completion_conn)
                    self._check_cancelled()
                    self.read_data = not complete

            self._check_cancelled()
            use_sql_heatmap = (
                self.read_data
                and not cache_live
                and self._should_use_sql_heatmap()
                )
            if use_sql_heatmap:
                set_parameter_complete(self.param, False)
                self._load_large_heatmap_from_sql()

            else:
                if self.read_data:
                    write_status, read_status, existing_data = (
                        snapshot_cache_parameter_state(cache, self.param.name)
                        )
                    conn = qcodes_read_only_connection(cache_database_path(cache))
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
                        )
                    finally:
                        self._close_sql_connection(conn)

                    self._check_cancelled()
                    data = self.cache_data[self.param.name]

                else:
                    data = cache_parameter_data(cache, self.param.name)

                self._check_cancelled()
                depvarData = data[self.param.name]

                # for shaped 2d plots
                if len(depvarData.shape) == 2:
                    (
                        axis_data,
                        axis_param,
                        dataGrid
                    ) = self.for_shaped_2d(
                        data,
                        depvarData
                        )

                else:
                    #Remove nan values
                    valid_rows = ~np.isnan(depvarData)

                    # for 1d plots
                    if len(self.param.depends_on_) == 1:
                        (
                            axis_data,
                            axis_param
                        ) = self.for_1d(
                            data,
                            valid_rows
                            )
                    # for >2d plots/unshaped 2d
                    else:
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
            self._aggregate_operated_heatmap_if_needed()
            self._check_cancelled()
            self._canonicalize_heatmap()
            self._check_cancelled()
        except PlotWorkCancelled:
            self._finish_cancelled()
            return
        except Exception as err:
            if self.is_cancelled():
                self._finish_cancelled()
                return
            log_exception("Plot processing failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return

        # Callback
        self._emit_finished(True)


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

        limit = max(1, int(getattr(
            self,
            "max_full_heatmap_points",
            MAX_FULL_HEATMAP_POINTS,
            )))
        requires_bounded_load = setpoint_count > limit
        if requires_bounded_load and getattr(self, "operations", None):
            raise OperationExecutionError(
                f"This heatmap has approximately {setpoint_count:,} points, "
                f"which exceeds the full-resolution operation limit of {limit:,}. "
                "Disable operations or increase max_full_heatmap_points."
                )
        return requires_bounded_load


    def _large_heatmap_point_count(self):
        source_grid_shape = self._heatmap_source_grid_shape_from_metadata()
        if source_grid_shape is not None:
            source_grid_rows, source_grid_columns = source_grid_shape
            setpoint_count = int(source_grid_rows * source_grid_columns)
            self.heatmap_source_grid_shape = source_grid_shape
            self.total_point_count_estimate = setpoint_count
            return setpoint_count

        conn = sqlite_read_only_connection(cache_database_path(self.cache))
        self._set_sql_connection(conn)
        try:
            self._check_cancelled()
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
        conn = sqlite_read_only_connection(cache_database_path(self.cache))
        self._set_sql_connection(conn)
        try:
            self._check_cancelled()
            rowid_min, rowid_max = self._rowid_span(conn)
            self._check_cancelled()
            self.heatmap_source_grid_shape = (
                self._heatmap_source_grid_shape_from_metadata()
                )
            x_data, y_data, z_data = self._read_heatmap_arrays(
                conn,
                rowid_min,
                rowid_max,
                )
        finally:
            self._close_sql_connection(conn)

        self._check_cancelled()
        x_axis, y_axis, data_grid = self._heatmap_grid_from_arrays(
            x_data,
            y_data,
            z_data,
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
        set_parameter_complete(self.param, False)


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

        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        z_column = _sqlite_identifier(self.param.name)
        selected_where_sql = f"{z_column} IS NOT NULL"
        selected_count = self._selected_parameter_row_count(conn)
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
            where_sql = f"{selected_where_sql} AND {axis_where_sql}"
            spatial_where_sql = (
                f"{where_sql} AND {x_column} IS NOT NULL "
                f"AND {y_column} IS NOT NULL"
                )
            range_summary = self._heatmap_spatial_summary(
                conn,
                spatial_where_sql,
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
                    spatial_where_sql,
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
                    f"WHERE {selected_where_sql} ORDER BY rowid"
                    ),
                )
            return self._arrays_from_cursor(cursor)

        spatial_where_sql = (
            f"{selected_where_sql} AND {x_column} IS NOT NULL "
            f"AND {y_column} IS NOT NULL"
            )
        summary = self._heatmap_spatial_summary(conn, spatial_where_sql, ())
        return self._spatially_aggregated_heatmap_arrays(
            conn,
            spatial_where_sql,
            (),
            summary,
            )


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
        x_centres, x_lower_edge, x_scale = self._spatial_axis_bins(
            float(x_min),
            float(x_max),
            x_count,
            x_bins,
            )
        y_centres, y_lower_edge, y_scale = self._spatial_axis_bins(
            float(y_min),
            float(y_max),
            y_count,
            y_bins,
            )
        if x_centres.size == 0 or y_centres.size == 0:
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            return self._arrays_from_values([], [], [])

        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        z_column = _sqlite_identifier(self.param.name)
        x_bin_sql = f"MIN(CAST(({x_column} - ?) * ? AS INTEGER), ?)"
        y_bin_sql = f"MIN(CAST(({y_column} - ?) * ? AS INTEGER), ?)"
        cursor = conn.execute(
            (
                "SELECT x_bin, y_bin, AVG(z_value), COUNT(*) FROM ("
                f"SELECT {x_bin_sql} AS x_bin, {y_bin_sql} AS y_bin, "
                f"{z_column} AS z_value FROM {table} WHERE {where_sql}"
                ") GROUP BY x_bin, y_bin ORDER BY y_bin, x_bin"
                ),
            (
                x_lower_edge,
                x_scale,
                x_bins - 1,
                y_lower_edge,
                y_scale,
                y_bins - 1,
                *parameters,
                ),
            )
        x_indices = []
        y_indices = []
        z_values = []
        aggregated_source_rows = 0
        for row_number, (x_index, y_index, z_value, bin_rows) in enumerate(cursor):
            if row_number % 1024 == 0:
                self._check_cancelled()
            if z_value is None:
                continue
            x_indices.append(int(x_index))
            y_indices.append(int(y_index))
            z_values.append(z_value)
            aggregated_source_rows += int(bin_rows)

        x_index_data = np.asarray(x_indices, dtype=np.int64)
        y_index_data = np.asarray(y_indices, dtype=np.int64)
        z_data = np.asarray(z_values, dtype=float)
        finite = np.isfinite(z_data)
        x_index_data = x_index_data[finite]
        y_index_data = y_index_data[finite]
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


    def _arrays_from_values(self, x_values, y_values, z_values):
        self._check_cancelled()
        x_data = np.asarray(x_values, dtype=float)
        self._check_cancelled()
        y_data = np.asarray(y_values, dtype=float)
        self._check_cancelled()
        z_data = np.asarray(z_values, dtype=float)
        self._check_cancelled()
        finite = np.isfinite(x_data) & np.isfinite(y_data) & np.isfinite(z_data)
        self._check_cancelled()

        return x_data[finite], y_data[finite], z_data[finite]


    def _heatmap_grid_from_arrays(
            self,
            x_data,
            y_data,
            z_data,
            *,
            max_cells=MAX_SQL_HEATMAP_GRID_CELLS,
            ):
        self._check_cancelled()
        max_cells = max(1, int(max_cells))
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
                and exact_cells <= max_cells
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

        fill_empty = False
        if getattr(self, "sampled_heatmap_source", False):
            max_cells = min(
                max_cells,
                max(1, int(z_data.size) // SQL_HEATMAP_SAMPLES_PER_CELL),
                )
            fill_empty = True

        x_axis, y_axis, data_grid = self._binned_heatmap_grid(
            x_data,
            y_data,
            z_data,
            unique_x,
            unique_y,
            max_cells=max_cells,
            fill_empty=fill_empty,
            )
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
            "empty_bins_filled": fill_empty,
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
        empty_bins_filled = bool(np.any(~np.isfinite(data_grid)))
        if empty_bins_filled:
            data_grid = self._fill_empty_heatmap_bins(data_grid)

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
            "grid_cell_limit": MAX_SQL_HEATMAP_GRID_CELLS,
            "empty_bins_filled": empty_bins_filled,
            }
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
        grid_sum = np.zeros((unique_y.size, unique_x.size), dtype=float)
        grid_count = np.zeros((unique_y.size, unique_x.size), dtype=np.int32)
        for start in range(0, z_data.size, CANCELLATION_CHUNK_SIZE):
            self._check_cancelled()
            stop = start + CANCELLATION_CHUNK_SIZE
            indices = (y_index[start:stop], x_index[start:stop])
            np.add.at(grid_sum, indices, z_data[start:stop])
            np.add.at(grid_count, indices, 1)

        self._check_cancelled()
        data_grid = np.full(grid_sum.shape, np.nan, dtype=float)
        np.divide(
            grid_sum,
            grid_count,
            out=data_grid,
            where=grid_count > 0,
            casting="unsafe",
            )

        return unique_x, unique_y, data_grid


    def _binned_heatmap_grid(
            self,
            x_data,
            y_data,
            z_data,
            unique_x,
            unique_y,
            max_cells=None,
            fill_empty=False,
            ):
        self._check_cancelled()
        x_bins, y_bins = self._bounded_grid_shape(
            unique_x.size,
            unique_y.size,
            max_cells=max_cells,
            )
        x_centres, x_index = self._scaled_axis_indices(
            x_data,
            x_bins,
            self._grid_axis_bounds("x"),
            )
        y_centres, y_index = self._scaled_axis_indices(
            y_data,
            y_bins,
            self._grid_axis_bounds("y"),
            )
        self._check_cancelled()

        grid_sum = np.zeros((y_centres.size, x_centres.size), dtype=float)
        grid_count = np.zeros((y_centres.size, x_centres.size), dtype=np.int32)
        for start in range(0, z_data.size, CANCELLATION_CHUNK_SIZE):
            self._check_cancelled()
            stop = start + CANCELLATION_CHUNK_SIZE
            indices = (y_index[start:stop], x_index[start:stop])
            np.add.at(grid_sum, indices, z_data[start:stop])
            np.add.at(grid_count, indices, 1)

        self._check_cancelled()
        data_grid = np.full(grid_sum.shape, np.nan, dtype=float)
        np.divide(
            grid_sum,
            grid_count,
            out=data_grid,
            where=grid_count > 0,
            casting="unsafe",
            )

        if fill_empty:
            data_grid = self._fill_empty_heatmap_bins(data_grid)

        return x_centres, y_centres, data_grid


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
        ranges = self.heatmap_axis_ranges or {}
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
        max_cells = int(max_cells or MAX_SQL_HEATMAP_GRID_CELLS)
        x_bins = max(1, min(int(x_count), MAX_SQL_HEATMAP_GRID_SIDE))
        y_bins = max(1, min(int(y_count), MAX_SQL_HEATMAP_GRID_SIDE))

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


    def for_1d(self, data, valid_rows):
        self._check_cancelled()
        axis_data = {}
        axis_param = {}
        dict_labels = list(data.keys())
        
        x_name =  self.axes_dict["x"]
        axis_data["x"] = data[x_name][valid_rows]
        self._check_cancelled()
        axis_param["x"] = self.param_dict[x_name]
        
        # get other value
        index = 1 if dict_labels[0] == x_name else 0
        axis_data["y"] = data[dict_labels[index]][valid_rows]
        self._check_cancelled()
        axis_param["y"] = self.param_dict[dict_labels[index]]
        
        return axis_data, axis_param
        
    
    def for_shaped_2d(self, data, depvarData):
        self._check_cancelled()
        axis_data = {}
        axis_param = {}
        axis_dimension = {}
        valid = {}
        shaped_axes_are_rectilinear = True
        depvarData = np.asarray(depvarData, dtype=float)
        self._check_cancelled()
        
        # Find correct data for each axis
        for axis in ["x", "y"]:
            self._check_cancelled()
            name = self.axes_dict[axis]
            param = self.param_dict[name]

            param_data = np.asarray(data[name], dtype=float)
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

        # QCoDeS shaped data can still contain a serpentine (snake) scan.  In
        # that case alternate rows of the fast coordinate run in the opposite
        # direction and the raw result array is not a rectilinear image.  Map
        # values by their recorded coordinates instead of silently mirroring
        # those rows.
        if (
                not shaped_axes_are_rectilinear
                or axis_dimension["x"] == axis_dimension["y"]
                ):
            valid_rows = np.isfinite(depvarData)
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

        if x_dimension == 1 and y_dimension == 0:
            return depvarData[np.ix_(valid["y"], valid["x"])]

        if x_dimension == 0 and y_dimension == 1:
            return depvarData[np.ix_(valid["x"], valid["y"])].transpose()

        valid_rows = np.isfinite(depvarData)
        for axis in ["x", "y"]:
            name = self.axes_dict[axis]
            valid_rows = valid_rows & np.isfinite(np.asarray(data[name], dtype=float))

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
            
        dataGrid = data2matrix(
                axis_data["y"], 
                axis_data["x"], 
                depvarData[valid_rows]
            )
        self._check_cancelled()
        
        # remove duplicates
        axis_data["y"] = dataGrid.index.to_numpy(float)
        axis_data["x"] = dataGrid.columns.to_numpy(float)
        
        dataGrid = dataGrid.to_numpy(float)
        
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
            MAX_SQL_HEATMAP_GRID_CELLS,
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
    
