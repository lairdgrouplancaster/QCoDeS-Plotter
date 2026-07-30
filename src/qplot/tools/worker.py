import math
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6 import QtCore

from qplot.datahandling import load_param_data_from_db
from qplot.datahandling.qcodes_cache import (
    cache_data,
    cache_database_path,
    cache_parameter_data,
    cache_read_status,
    cache_rundescriber,
    cache_table_name,
    cache_write_status,
    set_parameter_complete,
)
from qplot.datahandling.readonly import (
    qcodes_read_only_connection,
    sqlite_read_only_connection,
)
from qplot.diagnostics import log_exception

from . import data2matrix
from .heatmap_geometry import canonicalize_heatmap_data

if TYPE_CHECKING:
    import qcodes

MAX_FULL_HEATMAP_POINTS = 2_000_000
MAX_SQL_HEATMAP_SOURCE_ROWS = 250_000
MAX_SQL_HEATMAP_GRID_SIDE = 800
MAX_SQL_HEATMAP_GRID_CELLS = 250_000
SQL_HEATMAP_SAMPLES_PER_CELL = 4
SQL_HEATMAP_ROWID_CHUNK = 900


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
        
        # Required working data
        self.cache = cache
        self.table_name = cache_table_name(cache)
        self.param = param
        self.param_dict = param_dict
        
        self.axes_dict = axes
        self.read_data = read_data
        self.operations = [] if operations is None else operations
        self.force_sql_heatmap = force_sql_heatmap
        self.max_full_heatmap_points = max(1, int(max_full_heatmap_points))
        self.heatmap_axis_ranges = heatmap_axis_ranges
        self.heatmap_full_axis_ranges = heatmap_full_axis_ranges
        self.sampled_heatmap_source = False
        self.loaded_from_sql_sample = False
        self.loaded_point_count: int | None = None
        self.heatmap_downsample_info: dict[str, Any] | None = None
        self.heatmap_source_grid_shape: tuple[int, int] | None = None
        
    
    def run(self):
        try:
            cache = self.cache

            if self.read_data and self._should_use_sql_heatmap():
                set_parameter_complete(self.param, False)
                self._load_large_heatmap_from_sql()

            else:
                if self.read_data:
                    conn = qcodes_read_only_connection(cache_database_path(cache))
                    try:
                        (
                            self.updated_read_status,
                            self.updated_write_status,
                            self.cache_data
                        ) = load_param_data_from_db(
                            conn,
                            self.table_name,
                            cache_rundescriber(cache),
                            self.param.name,
                            cache_write_status(cache),
                            cache_read_status(cache),
                            cache_data(cache)
                        )
                    finally:
                        conn.close()

                    data = self.cache_data[self.param.name]

                else:
                    data = cache_parameter_data(cache, self.param.name)

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

        except Exception as err: # Raise error in main thread
            log_exception("Plot worker failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False) # False: Failed
            return

        # Run additional operations
        results = self.do_operations()

        # Update based on operations
        if results is not None:
            (
                self.axis_data["x"],
                self.axis_data["y"]
            ) = results[:2]
            if hasattr(self, "dataGrid"):
                self.dataGrid = results[2]

        try:
            self._canonicalize_heatmap()
        except Exception as err:
            log_exception("Invalid heatmap geometry", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False)
            return

        # Callback
        self.emitter.finished.emit(True)


    def _canonicalize_heatmap(self) -> None:
        """Keep worker indices consistent with increasing heatmap axes."""

        if not hasattr(self, "dataGrid"):
            return
        x_data = np.asarray(self.axis_data.get("x", []))
        y_data = np.asarray(self.axis_data.get("y", []))
        data_grid = np.asarray(self.dataGrid)
        if x_data.size == 0 or y_data.size == 0 or data_grid.size == 0:
            return

        x_data, y_data, data_grid = canonicalize_heatmap_data(
            x_data,
            y_data,
            data_grid,
            )
        self.axis_data["x"] = x_data
        self.axis_data["y"] = y_data
        self.dataGrid = data_grid


    def _should_use_sql_heatmap(self):
        if len(getattr(self.param, "depends_on_", ())) <= 1:
            return False

        if self.force_sql_heatmap:
            return True

        setpoint_count = self._large_heatmap_point_count()
        if setpoint_count is None:
            return False

        limit = max(1, int(getattr(
            self,
            "max_full_heatmap_points",
            MAX_FULL_HEATMAP_POINTS,
            )))
        return setpoint_count > limit


    def _large_heatmap_point_count(self):
        source_grid_shape = self._heatmap_source_grid_shape_from_metadata()
        if source_grid_shape is not None:
            source_grid_rows, source_grid_columns = source_grid_shape
            setpoint_count = int(source_grid_rows * source_grid_columns)
            self.heatmap_source_grid_shape = source_grid_shape
            self.total_point_count_estimate = setpoint_count
            return setpoint_count

        conn = sqlite_read_only_connection(cache_database_path(self.cache))
        try:
            setpoint_count = self._selected_parameter_row_count(conn)
        finally:
            conn.close()

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
        try:
            rowid_min, rowid_max = self._rowid_span(conn)
            self.heatmap_source_grid_shape = (
                self._heatmap_source_grid_shape_from_metadata()
                )
            x_data, y_data, z_data = self._read_heatmap_arrays(
                conn,
                rowid_min,
                rowid_max,
                )
        finally:
            conn.close()

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
        self.loaded_from_sql_sample = True
        self.loaded_point_count = int(z_data.size)
        self.heatmap_downsample_info = self._heatmap_downsample_info()

        # The direct SQL path deliberately does not populate QCoDeS' full
        # in-memory cache. Keep future refreshes on the database path.
        self.read_data = False
        set_parameter_complete(self.param, False)


    def _rowid_span(self, conn):
        table = _sqlite_identifier(self.table_name)
        row = conn.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None, None

        return int(row[0]), int(row[1])


    def _read_heatmap_arrays(self, conn, rowid_min, rowid_max):
        if rowid_min is None or rowid_max is None:
            self._heatmap_source_info = {
                "row_count": 0,
                "estimated_range_rows": None,
                "sampled": False,
                "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
                "sample_stride": 1,
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
            "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
            "sample_stride": 1,
            "strategy": "all",
            "axis_ranges": axis_ranges,
            }

        axis_where_sql, parameters = self._heatmap_where_clause()
        if axis_where_sql:
            where_sql = f"{selected_where_sql} AND {axis_where_sql}"
            sample_sql, sample_parameters = self._range_sample_clause(row_count)
            self.sampled_heatmap_source = bool(sample_sql)
            self._heatmap_source_info.update({
                "estimated_range_rows": getattr(
                    self,
                    "_heatmap_estimated_range_rows",
                    None,
                    ),
                "sampled": bool(sample_sql),
                "sample_stride": getattr(self, "_heatmap_source_sample_stride", 1),
                "strategy": "visible-range stride" if sample_sql else "visible range",
                })
            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {where_sql}{sample_sql} "
                    "ORDER BY rowid LIMIT ?"
                    ),
                (*parameters, *sample_parameters, MAX_SQL_HEATMAP_SOURCE_ROWS),
                )
            arrays = self._arrays_from_cursor(cursor)
            if arrays[2].size > 0 or not sample_sql:
                return arrays

            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {where_sql} "
                    "ORDER BY rowid LIMIT ?"
                    ),
                (*parameters, MAX_SQL_HEATMAP_SOURCE_ROWS),
                )
            self.sampled_heatmap_source = False
            self._heatmap_source_info.update({
                "sampled": False,
                "sample_stride": 1,
                "strategy": "visible range fallback",
                })
            return self._arrays_from_cursor(cursor)

        if row_count <= MAX_SQL_HEATMAP_SOURCE_ROWS:
            self.sampled_heatmap_source = False
            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {selected_where_sql} ORDER BY rowid"
                    ),
                )
            return self._arrays_from_cursor(cursor)

        self.sampled_heatmap_source = True
        stride = max(1, math.ceil(row_count / MAX_SQL_HEATMAP_SOURCE_ROWS))
        self._heatmap_source_sample_stride = stride
        self._heatmap_source_info.update({
            "sampled": True,
            "sample_stride": stride,
            "strategy": "uniform selected-row stride",
            })
        cursor = conn.execute(
            (
                f"SELECT {columns} FROM {table} "
                f"WHERE {selected_where_sql} AND ((rowid - ?) % ?) = 0 "
                "ORDER BY rowid LIMIT ?"
                ),
            (1, stride, MAX_SQL_HEATMAP_SOURCE_ROWS),
            )
        arrays = self._arrays_from_cursor(cursor)
        if arrays[2].size > 0:
            return arrays

        cursor = conn.execute(
            (
                f"SELECT {columns} FROM {table} "
                f"WHERE {selected_where_sql} ORDER BY rowid LIMIT ?"
                ),
            (MAX_SQL_HEATMAP_SOURCE_ROWS,),
            )
        self._heatmap_source_info.update({
            "sample_stride": 1,
            "strategy": "selected-row fallback",
            })
        return self._arrays_from_cursor(cursor)


    def _selected_parameter_row_count(self, conn):
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


    def _range_sample_clause(self, row_count):
        stride = self._range_sample_stride(row_count)
        self._heatmap_source_sample_stride = stride
        if stride <= 1:
            return "", ()

        return " AND ((rowid - ?) % ?) = 0", (1, stride)


    def _range_sample_stride(self, row_count):
        ranges = self.heatmap_axis_ranges or {}
        full_ranges = self.heatmap_full_axis_ranges or {}
        fraction = 1.0

        for axis in ("x", "y"):
            axis_range = ranges.get(axis)
            full_axis_range = full_ranges.get(axis)
            if axis_range is None or full_axis_range is None:
                continue

            try:
                low, high = sorted(float(value) for value in axis_range)
                full_low, full_high = sorted(float(value) for value in full_axis_range)
            except (TypeError, ValueError):
                continue

            full_width = full_high - full_low
            if (
                    not np.isfinite(full_width)
                    or full_width <= 0
                    or not np.isfinite(low)
                    or not np.isfinite(high)
                    ):
                continue

            fraction *= min(max((high - low) / full_width, 0.0), 1.0)

        estimated_rows = max(1, int(row_count * fraction))
        self._heatmap_estimated_range_rows = estimated_rows
        return max(1, math.ceil(estimated_rows / MAX_SQL_HEATMAP_SOURCE_ROWS))


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


    def _sample_rowids(self, rowid_min, rowid_max):
        span = rowid_max - rowid_min + 1
        count = min(MAX_SQL_HEATMAP_SOURCE_ROWS, span)
        if count <= 0:
            return np.array([], dtype=np.int64)

        starts = (np.arange(count, dtype=np.int64) * span) // count
        ends = ((np.arange(1, count + 1, dtype=np.int64) * span) // count) - 1
        widths = np.maximum(ends - starts + 1, 1)
        jitter = (
            np.arange(count, dtype=np.int64) * 1_103_515_245 + 12_345
            ) % widths
        rowids = rowid_min + starts + jitter
        rowids[0] = rowid_min
        rowids[-1] = rowid_max
        return np.unique(rowids)


    def _arrays_from_cursor(self, cursor):
        x_values = []
        y_values = []
        z_values = []

        for x_value, y_value, z_value in cursor:
            x_values.append(x_value)
            y_values.append(y_value)
            z_values.append(z_value)

        return self._arrays_from_values(x_values, y_values, z_values)


    def _arrays_from_values(self, x_values, y_values, z_values):
        x_data = np.asarray(x_values, dtype=float)
        y_data = np.asarray(y_values, dtype=float)
        z_data = np.asarray(z_values, dtype=float)
        finite = np.isfinite(x_data) & np.isfinite(y_data) & np.isfinite(z_data)

        return x_data[finite], y_data[finite], z_data[finite]


    def _heatmap_grid_from_arrays(self, x_data, y_data, z_data):
        if z_data.size == 0:
            self._heatmap_grid_info = {
                "unique_x_count": 0,
                "unique_y_count": 0,
                "exact_cell_count": 0,
                "grid_columns": 0,
                "grid_rows": 0,
                "grid_cell_count": 0,
                "grid_binned": False,
                "grid_cell_limit": MAX_SQL_HEATMAP_GRID_CELLS,
                "empty_bins_filled": False,
                }
            return (
                np.array([], dtype=float),
                np.array([], dtype=float),
                np.empty((0, 0), dtype=float),
                )

        unique_x = np.unique(x_data)
        unique_y = np.unique(y_data)
        exact_cells = unique_x.size * unique_y.size
        source_grid_shape = getattr(self, "heatmap_source_grid_shape", None)
        if source_grid_shape is None:
            source_grid_rows = int(unique_y.size)
            source_grid_columns = int(unique_x.size)
        else:
            source_grid_rows, source_grid_columns = source_grid_shape
        source_grid_cell_count = int(source_grid_rows * source_grid_columns)
        if (
                not getattr(self, "sampled_heatmap_source", False)
                and exact_cells <= MAX_SQL_HEATMAP_GRID_CELLS
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
                "grid_cell_limit": MAX_SQL_HEATMAP_GRID_CELLS,
                "empty_bins_filled": False,
                }
            return x_axis, y_axis, data_grid

        max_cells = MAX_SQL_HEATMAP_GRID_CELLS
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


    def _heatmap_downsample_info(self):
        source_info = getattr(self, "_heatmap_source_info", None) or {}
        grid_info = getattr(self, "_heatmap_grid_info", None) or {}
        source_sampled = bool(source_info.get("sampled", False))
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
        if not source_sampled and not grid_reduced:
            return None

        return {
            "source_row_count": source_info.get("row_count"),
            "estimated_range_rows": source_info.get("estimated_range_rows"),
            "loaded_point_count": getattr(self, "loaded_point_count", None),
            "source_sampled": source_sampled,
            "source_sample_limit": source_info.get("sample_limit"),
            "source_sample_stride": source_info.get("sample_stride"),
            "source_sample_strategy": source_info.get("strategy"),
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
        x_index = np.searchsorted(unique_x, x_data)
        y_index = np.searchsorted(unique_y, y_data)
        grid_sum = np.zeros((unique_y.size, unique_x.size), dtype=float)
        grid_count = np.zeros((unique_y.size, unique_x.size), dtype=np.int32)
        np.add.at(grid_sum, (y_index, x_index), z_data)
        np.add.at(grid_count, (y_index, x_index), 1)

        data_grid = np.full(grid_sum.shape, np.nan, dtype=np.float32)
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

        grid_sum = np.zeros((y_centres.size, x_centres.size), dtype=float)
        grid_count = np.zeros((y_centres.size, x_centres.size), dtype=np.int32)
        np.add.at(grid_sum, (y_index, x_index), z_data)
        np.add.at(grid_count, (y_index, x_index), 1)

        data_grid = np.full(grid_sum.shape, np.nan, dtype=np.float32)
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


    @staticmethod
    def _fill_empty_heatmap_bins(data_grid):
        if data_grid.size == 0 or np.all(np.isfinite(data_grid)):
            return data_grid
        if not np.any(np.isfinite(data_grid)):
            return data_grid

        filled = np.array(data_grid, dtype=np.float32, copy=True)
        row_positions = np.arange(filled.shape[0], dtype=float)
        column_positions = np.arange(filled.shape[1], dtype=float)

        for column in range(filled.shape[1]):
            values = filled[:, column]
            finite = np.isfinite(values)
            if np.any(finite) and not np.all(finite):
                values[~finite] = np.interp(
                    row_positions[~finite],
                    row_positions[finite],
                    values[finite],
                    )

        for row in range(filled.shape[0]):
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
        indices = np.floor(scaled * bin_count).astype(np.int64)
        indices = np.clip(indices, 0, bin_count - 1)
        step = (upper - lower) / bin_count
        centres = lower + (np.arange(bin_count, dtype=float) + 0.5) * step

        return centres, indices


    def for_1d(self, data, valid_rows):
        axis_data = {}
        axis_param = {}
        dict_labels = list(data.keys())
        
        x_name =  self.axes_dict["x"]
        axis_data["x"] = data[x_name][valid_rows]
        axis_param["x"] = self.param_dict[x_name]
        
        # get other value
        index = 1 if dict_labels[0] == x_name else 0
        axis_data["y"] = data[dict_labels[index]][valid_rows]
        axis_param["y"] = self.param_dict[dict_labels[index]]
        
        return axis_data, axis_param
        
    
    def for_shaped_2d(self, data, depvarData):
        axis_data = {}
        axis_param = {}
        axis_dimension = {}
        valid = {}
        depvarData = np.asarray(depvarData, dtype=float)
        
        # Find correct data for each axis
        for axis in ["x", "y"]:
            name = self.axes_dict[axis]
            param = self.param_dict[name]

            param_data = np.asarray(data[name], dtype=float)
            dimension = self._shaped_axis_dimension(name, param_data, depvarData)
            param_data = self._shaped_axis_values(param_data, dimension)

            valid[axis] = np.isfinite(param_data)
            axis_data[axis] = param_data[valid[axis]]
            axis_param[axis] = param
            axis_dimension[axis] = dimension

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
        moved = np.moveaxis(param_data, dimension, 0)
        rows = moved.reshape(moved.shape[0], -1)
        values = np.full(rows.shape[0], np.nan, dtype=float)

        for index, row in enumerate(rows):
            finite = np.flatnonzero(np.isfinite(row))
            if finite.size:
                values[index] = row[finite[0]]

        return values


    def _shaped_axis_residual(self, param_data, dimension):
        values = self._shaped_axis_values(param_data, dimension)
        shape = [1] * param_data.ndim
        shape[dimension] = values.size
        expected = np.broadcast_to(values.reshape(shape), param_data.shape)
        valid = np.isfinite(param_data) & np.isfinite(expected)
        if not np.any(valid):
            return np.inf

        return float(np.nanmax(np.abs(param_data[valid] - expected[valid])))


    def _shaped_data_grid(self, data, depvarData, axis_dimension, valid):
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
        axis_data = {}
        axis_param = {}
        for axis in ["x", "y"]:
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
        
        # remove duplicates
        axis_data["y"] = dataGrid.index.to_numpy(float)
        axis_data["x"] = dataGrid.columns.to_numpy(float)
        
        dataGrid = dataGrid.to_numpy(float)
        
        return axis_data, axis_param, dataGrid
        
    
    def do_operations(self):
        """
        Runs through all functions in self.operations and performs those on the
        data.
        Copies data to allow a fall

        Returns
        -------
        data_dict["x"], data_dict["y"], data_dict["z"] : np.ndarray
            The updated data after all operations have been performed
        None : NoneType
            No operations to be perform or all failed.
    
        """
        operations = self.operations
        if len(operations) == 0:
            return None
        
        one_succeeded = False
        
        data_dict = {
            "x" : self.axis_data["x"].copy(),
            "y" : self.axis_data["y"].copy(),
            "z" : self.dataGrid.copy() if hasattr(self, "dataGrid") else None # Only give dataGrid if it exists
            }
        
        for func in operations:
            try:
                results = func(data_dict)
                for key in results.keys():
                    data_dict[key] = results[key]
                one_succeeded = True
                
            except Exception as err:
                log_exception("Plot operation failed", err, __name__)
                self.emitter.errorOccurred.emit(err)
                
        if one_succeeded:
            return data_dict["x"], data_dict["y"], data_dict["z"]
        else: # If all failed, go back to before operations data
            return None

        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
