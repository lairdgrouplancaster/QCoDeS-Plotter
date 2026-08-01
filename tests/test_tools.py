import sqlite3
import tempfile
import unittest

import numpy as np

import qplot.tools.worker as worker_module
from qplot.configuration.scripts import try_as_num
from qplot.tools.general import data2matrix
from qplot.tools.heatmap_geometry import HeatmapGeometry
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import (
    differentiate,
    fill_heatmap,
    pass_filter,
    subtract_mean,
)
from qplot.tools.worker import OperationExecutionError, loader
from qplot.windows._dataset_handle import DatasetHandle, DatasetKey
from qplot.windows._plotWin import plotWidget


class ToolFunctionTestCase(unittest.TestCase):
    def test_try_as_num_handles_int_float_scientific_and_string(self):
        self.assertEqual(try_as_num("4"), 4)
        self.assertEqual(try_as_num("4.5"), 4.5)
        self.assertEqual(try_as_num("1e-3"), 1e-3)
        self.assertEqual(try_as_num("dark"), "dark")

    def test_data2matrix_pivots_flat_scan_data(self):
        matrix = data2matrix(
            np.array([0, 0, 1, 1]),
            np.array([0, 1, 0, 1]),
            np.array([10, 11, 12, 13]),
        )

        self.assertEqual(matrix.loc[0, 0], 10)
        self.assertEqual(matrix.loc[1, 1], 13)

    def test_shaped_2d_loader_handles_sparse_live_grids(self):
        worker = loader.__new__(loader)
        worker.axes_dict = {"x": "fast", "y": "slow"}
        worker.param = type("Param", (), {"depends_on_": ("slow", "fast")})()
        worker.param_dict = {
            "slow": type("Param", (), {"name": "slow"})(),
            "fast": type("Param", (), {"name": "fast"})(),
            }

        slow = np.full((10, 100), np.nan)
        fast = np.full((10, 100), np.nan)
        signal = np.full((10, 100), np.nan)
        slow[0, :2] = 0.0
        fast[0, :2] = [0.0, 1.0]
        signal[0, :2] = [42.0, 43.0]

        axis_data, _axis_param, data_grid = loader.for_shaped_2d(
            worker,
            {"slow": slow, "fast": fast},
            signal,
            )

        np.testing.assert_array_equal(axis_data["x"], np.array([0.0, 1.0]))
        np.testing.assert_array_equal(axis_data["y"], np.array([0.0]))
        np.testing.assert_array_equal(data_grid, np.array([[42.0, 43.0]]))

    def test_shaped_2d_loader_transposes_when_axes_are_swapped(self):
        worker = loader.__new__(loader)
        worker.axes_dict = {"x": "slow", "y": "fast"}
        worker.param = type("Param", (), {"depends_on_": ("slow", "fast")})()
        worker.param_dict = {
            "slow": type("Param", (), {"name": "slow"})(),
            "fast": type("Param", (), {"name": "fast"})(),
            }

        slow = np.full((10, 100), np.nan)
        fast = np.full((10, 100), np.nan)
        signal = np.full((10, 100), np.nan)
        slow[0, :2] = 0.0
        fast[0, :2] = [0.0, 1.0]
        signal[0, :2] = [42.0, 43.0]

        axis_data, _axis_param, data_grid = loader.for_shaped_2d(
            worker,
            {"slow": slow, "fast": fast},
            signal,
            )

        np.testing.assert_array_equal(axis_data["x"], np.array([0.0]))
        np.testing.assert_array_equal(axis_data["y"], np.array([0.0, 1.0]))
        np.testing.assert_array_equal(data_grid, np.array([[42.0], [43.0]]))

    def test_worker_canonicalizes_descending_heatmap_axes_and_grid(self):
        worker = loader.__new__(loader)
        worker.axis_data = {
            "x": np.array([4.0, 1.0, 0.0]),
            "y": np.array([13.0, 10.0]),
            }
        worker.dataGrid = np.array([
            [6.0, 5.0, 4.0],
            [3.0, 2.0, 1.0],
            ])

        loader._canonicalize_heatmap(worker)

        np.testing.assert_array_equal(worker.axis_data["x"], [0.0, 1.0, 4.0])
        np.testing.assert_array_equal(worker.axis_data["y"], [10.0, 13.0])
        np.testing.assert_array_equal(
            worker.dataGrid,
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
            )

    def test_large_heatmap_sql_loader_uses_bounded_spatial_aggregation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = f"{tmpdir}/heatmap.db"
            self._create_heatmap_table(database_path)
            worker = self._sql_heatmap_worker(database_path)

            old_database_path = worker_module.cache_database_path
            old_set_complete = worker_module.set_parameter_complete
            old_source_rows = worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS
            old_grid_cells = worker_module.MAX_SQL_HEATMAP_GRID_CELLS
            old_grid_side = worker_module.MAX_SQL_HEATMAP_GRID_SIDE
            try:
                worker_module.cache_database_path = lambda _cache: database_path
                worker_module.set_parameter_complete = (
                    lambda param, complete=False: setattr(param, "_complete", complete)
                    )
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = 60
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = 16
                worker_module.MAX_SQL_HEATMAP_GRID_SIDE = 4

                loader._load_large_heatmap_from_sql(worker)
            finally:
                worker_module.cache_database_path = old_database_path
                worker_module.set_parameter_complete = old_set_complete
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = old_source_rows
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = old_grid_cells
                worker_module.MAX_SQL_HEATMAP_GRID_SIDE = old_grid_side

            self.assertTrue(worker.loaded_from_sql_heatmap)
            self.assertFalse(worker.read_data)
            self.assertLessEqual(worker.loaded_point_count, 16)
            self.assertLessEqual(worker.dataGrid.size, 16)
            self.assertGreater(np.isfinite(worker.dataGrid).sum(), 0)
            self.assertIsNotNone(worker.heatmap_downsample_info)
            self.assertFalse(worker.heatmap_downsample_info["source_sampled"])
            self.assertTrue(worker.heatmap_downsample_info["source_aggregated"])
            self.assertTrue(worker.heatmap_downsample_info["grid_binned"])
            self.assertEqual(worker.heatmap_downsample_info["source_row_count"], 1200)
            self.assertIsNone(worker.heatmap_downsample_info["source_sample_limit"])
            self.assertIsNone(worker.heatmap_downsample_info["source_sample_stride"])
            self.assertEqual(
                worker.heatmap_downsample_info["source_aggregation_strategy"],
                "spatial mean",
                )
            self.assertEqual(
                worker.heatmap_downsample_info["aggregated_source_row_count"],
                1200,
                )
            self.assertEqual(worker.heatmap_downsample_info["source_grid_columns"], 40)
            self.assertEqual(worker.heatmap_downsample_info["source_grid_rows"], 30)
            self.assertEqual(
                worker.heatmap_downsample_info["source_grid_cell_count"],
                1200,
                )
            self.assertEqual(
                worker.heatmap_downsample_info["grid_cell_count"],
                worker.dataGrid.size,
                )

    def test_large_heatmap_sql_output_is_independent_of_insertion_order(self):
        rows = [
            (float(x), float(y), float(x + y * 100))
            for y in range(30)
            for x in range(40)
            ]

        with tempfile.TemporaryDirectory() as tmpdir:
            results = []
            old_database_path = worker_module.cache_database_path
            old_set_complete = worker_module.set_parameter_complete
            old_source_rows = worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS
            old_grid_cells = worker_module.MAX_SQL_HEATMAP_GRID_CELLS
            old_grid_side = worker_module.MAX_SQL_HEATMAP_GRID_SIDE
            try:
                worker_module.set_parameter_complete = (
                    lambda param, complete=False: setattr(param, "_complete", complete)
                    )
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = 60
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = 16
                worker_module.MAX_SQL_HEATMAP_GRID_SIDE = 4

                for name, insertion_order in (
                        ("forward", rows),
                        ("reverse", list(reversed(rows))),
                        ):
                    database_path = f"{tmpdir}/{name}.db"
                    self._create_heatmap_table_from_rows(
                        database_path,
                        insertion_order,
                    )
                    worker = self._sql_heatmap_worker(database_path)
                    worker.cache.rundescriber.shapes = None
                    worker_module.cache_database_path = (
                        lambda _cache, path=database_path: path
                        )

                    loader._load_large_heatmap_from_sql(worker)
                    loader._canonicalize_heatmap(worker)
                    self.assertEqual(
                        worker.heatmap_source_axis_ranges,
                        {"x": (0.0, 39.0), "y": (0.0, 29.0)},
                        )
                    results.append((
                        worker.axis_data["x"],
                        worker.axis_data["y"],
                        worker.dataGrid,
                        worker.heatmap_downsample_info,
                        ))
            finally:
                worker_module.cache_database_path = old_database_path
                worker_module.set_parameter_complete = old_set_complete
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = old_source_rows
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = old_grid_cells
                worker_module.MAX_SQL_HEATMAP_GRID_SIDE = old_grid_side

        forward_x, forward_y, forward_grid, forward_info = results[0]
        reverse_x, reverse_y, reverse_grid, reverse_info = results[1]
        np.testing.assert_array_equal(forward_x, reverse_x)
        np.testing.assert_array_equal(forward_y, reverse_y)
        np.testing.assert_array_equal(forward_grid, reverse_grid)
        self.assertEqual(
            forward_info["source_aggregation_strategy"],
            "spatial mean",
            )
        self.assertEqual(
            reverse_info["source_aggregation_strategy"],
            "spatial mean",
            )
        self.assertEqual(forward_info["source_grid_columns"], 40)
        self.assertEqual(forward_info["source_grid_rows"], 30)
        np.testing.assert_array_equal(
            forward_grid,
            [[304.5, 314.5, 324.5, 334.5],
             [1054.5, 1064.5, 1074.5, 1084.5],
             [1804.5, 1814.5, 1824.5, 1834.5],
             [2554.5, 2564.5, 2574.5, 2584.5]],
            )

        geometry = HeatmapGeometry.from_centres(forward_x, forward_y)
        np.testing.assert_allclose(
            [geometry.x.edges[0], geometry.x.edges[-1]],
            [-0.5, 39.5],
            )
        np.testing.assert_allclose(
            [geometry.y.edges[0], geometry.y.edges[-1]],
            [-0.5, 29.5],
            )

    def test_small_heatmap_sql_loader_preserves_exact_grid(self):
        rows = [
            (float(x), float(y), float(x + y * 10))
            for y in range(3)
            for x in range(4)
            ]

        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = f"{tmpdir}/small.db"
            self._create_heatmap_table_from_rows(database_path, list(reversed(rows)))
            worker = self._sql_heatmap_worker(database_path)
            worker.cache.rundescriber.shapes = {"signal": (3, 4)}

            old_database_path = worker_module.cache_database_path
            old_set_complete = worker_module.set_parameter_complete
            old_source_rows = worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS
            old_grid_cells = worker_module.MAX_SQL_HEATMAP_GRID_CELLS
            try:
                worker_module.cache_database_path = lambda _cache: database_path
                worker_module.set_parameter_complete = (
                    lambda param, complete=False: setattr(param, "_complete", complete)
                    )
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = 12
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = 12

                loader._load_large_heatmap_from_sql(worker)
                loader._canonicalize_heatmap(worker)
            finally:
                worker_module.cache_database_path = old_database_path
                worker_module.set_parameter_complete = old_set_complete
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = old_source_rows
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = old_grid_cells

        np.testing.assert_array_equal(worker.axis_data["x"], [0.0, 1.0, 2.0, 3.0])
        np.testing.assert_array_equal(worker.axis_data["y"], [0.0, 1.0, 2.0])
        np.testing.assert_array_equal(
            worker.dataGrid,
            [[0.0, 1.0, 2.0, 3.0],
             [10.0, 11.0, 12.0, 13.0],
             [20.0, 21.0, 22.0, 23.0]],
            )
        self.assertFalse(worker.sampled_heatmap_source)

    def test_large_heatmap_sql_loader_can_reload_visible_axis_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = f"{tmpdir}/heatmap.db"
            self._create_heatmap_table(database_path)
            worker = self._sql_heatmap_worker(database_path)
            worker.heatmap_axis_ranges = {
                "x": (10.0, 20.0),
                "y": (5.0, 10.0),
                }
            worker.heatmap_full_axis_ranges = {
                "x": (0.0, 39.0),
                "y": (0.0, 29.0),
                }

            old_database_path = worker_module.cache_database_path
            old_set_complete = worker_module.set_parameter_complete
            old_source_rows = worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS
            old_grid_cells = worker_module.MAX_SQL_HEATMAP_GRID_CELLS
            try:
                worker_module.cache_database_path = lambda _cache: database_path
                worker_module.set_parameter_complete = (
                    lambda param, complete=False: setattr(param, "_complete", complete)
                    )
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = 1_000
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = 1_000

                loader._load_large_heatmap_from_sql(worker)
            finally:
                worker_module.cache_database_path = old_database_path
                worker_module.set_parameter_complete = old_set_complete
                worker_module.MAX_SQL_HEATMAP_SOURCE_ROWS = old_source_rows
                worker_module.MAX_SQL_HEATMAP_GRID_CELLS = old_grid_cells

            self.assertGreater(worker.loaded_point_count, 0)
            self.assertGreaterEqual(worker.axis_data["x"].min(), 10.0)
            self.assertLessEqual(worker.axis_data["x"].max(), 20.0)
            self.assertGreaterEqual(worker.axis_data["y"].min(), 5.0)
            self.assertLessEqual(worker.axis_data["y"].max(), 10.0)

    def test_sampled_large_heatmap_sql_loader_bins_instead_of_sparse_exact_grid(self):
        worker = self._sql_heatmap_worker("")
        worker.sampled_heatmap_source = True
        x = np.tile(np.arange(10, dtype=float), 2)
        y = np.repeat(np.arange(2, dtype=float), 10)
        z = np.arange(20, dtype=float)

        old_grid_cells = worker_module.MAX_SQL_HEATMAP_GRID_CELLS
        old_samples_per_cell = worker_module.SQL_HEATMAP_SAMPLES_PER_CELL
        try:
            worker_module.MAX_SQL_HEATMAP_GRID_CELLS = 100
            worker_module.SQL_HEATMAP_SAMPLES_PER_CELL = 4

            _axis_x, _axis_y, data_grid = loader._heatmap_grid_from_arrays(
                worker,
                x,
                y,
                z,
                )
        finally:
            worker_module.MAX_SQL_HEATMAP_GRID_CELLS = old_grid_cells
            worker_module.SQL_HEATMAP_SAMPLES_PER_CELL = old_samples_per_cell

        self.assertLessEqual(data_grid.size, 5)

    def test_sampled_large_heatmap_sql_loader_fills_empty_overview_bins(self):
        worker = self._sql_heatmap_worker("")
        worker.sampled_heatmap_source = True
        x = np.arange(100, dtype=float)
        y = np.mod(np.arange(100, dtype=float) * 17, 100)
        z = np.arange(100, dtype=float)

        old_grid_cells = worker_module.MAX_SQL_HEATMAP_GRID_CELLS
        old_grid_side = worker_module.MAX_SQL_HEATMAP_GRID_SIDE
        old_samples_per_cell = worker_module.SQL_HEATMAP_SAMPLES_PER_CELL
        try:
            worker_module.MAX_SQL_HEATMAP_GRID_CELLS = 100
            worker_module.MAX_SQL_HEATMAP_GRID_SIDE = 20
            worker_module.SQL_HEATMAP_SAMPLES_PER_CELL = 1

            _axis_x, _axis_y, data_grid = loader._heatmap_grid_from_arrays(
                worker,
                x,
                y,
                z,
                )
        finally:
            worker_module.MAX_SQL_HEATMAP_GRID_CELLS = old_grid_cells
            worker_module.MAX_SQL_HEATMAP_GRID_SIDE = old_grid_side
            worker_module.SQL_HEATMAP_SAMPLES_PER_CELL = old_samples_per_cell

        self.assertEqual(data_grid.shape, (10, 10))
        self.assertTrue(np.isfinite(data_grid).all())

    def test_large_heatmap_sql_mode_uses_configured_full_resolution_limit(self):
        worker = self._sql_heatmap_worker("")
        worker.force_sql_heatmap = False
        worker.max_full_heatmap_points = 10
        worker._large_heatmap_point_count = lambda: 10

        self.assertFalse(loader._should_use_sql_heatmap(worker))

        worker._large_heatmap_point_count = lambda: 11

        self.assertTrue(loader._should_use_sql_heatmap(worker))

    def test_large_heatmap_sql_mode_uses_selected_parameter_count_not_run_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = f"{tmpdir}/heatmap.db"
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE results (x REAL, y REAL, signal REAL, other REAL)"
                    )
                signal_rows = [
                    (float(index), 0.0, float(index), None)
                    for index in range(80)
                    ]
                other_rows = [
                    (float(index), 1.0, None, float(index))
                    for index in range(80)
                    ]
                conn.executemany(
                    "INSERT INTO results (x, y, signal, other) VALUES (?, ?, ?, ?)",
                    signal_rows + other_rows,
                    )
                conn.commit()
            finally:
                conn.close()

            worker = self._sql_heatmap_worker(database_path)
            worker.cache.rundescriber.shapes = None
            worker.max_full_heatmap_points = 100

            old_database_path = worker_module.cache_database_path
            try:
                worker_module.cache_database_path = lambda _cache: database_path

                self.assertEqual(loader._large_heatmap_point_count(worker), 80)
                self.assertFalse(loader._should_use_sql_heatmap(worker))
            finally:
                worker_module.cache_database_path = old_database_path

    def _create_heatmap_table(self, database_path):
        self._create_heatmap_table_from_rows(
            database_path,
            [
                (float(x), float(y), float(x + y * 100))
                for y in range(30)
                for x in range(40)
                ],
            )

    def _create_heatmap_table_from_rows(self, database_path, rows):
        conn = sqlite3.connect(database_path)
        try:
            conn.execute("CREATE TABLE results (x REAL, y REAL, signal REAL)")
            conn.executemany(
                "INSERT INTO results (x, y, signal) VALUES (?, ?, ?)",
                rows,
                )
            conn.commit()
        finally:
            conn.close()

    def _sql_heatmap_worker(self, database_path):
        del database_path

        class Param:
            def __init__(self, name):
                self.name = name
                self.depends_on_ = ("y", "x") if name == "signal" else ()
                self._complete = False

        class Rundescriber:
            shapes = {"signal": (30, 40)}

        class Cache:
            rundescriber = Rundescriber()

        worker = loader.__new__(loader)
        worker.cache = Cache()
        worker.table_name = "results"
        worker.param = Param("signal")
        worker.param_dict = {
            "x": Param("x"),
            "y": Param("y"),
            "signal": worker.param,
            }
        worker.axes_dict = {"x": "x", "y": "y"}
        worker.read_data = True
        worker.force_sql_heatmap = False
        worker.max_full_heatmap_points = worker_module.MAX_FULL_HEATMAP_POINTS
        worker.heatmap_axis_ranges = None
        worker.heatmap_full_axis_ranges = None
        return worker

    def test_plot_operations_return_updated_arrays(self):
        data = {
            "x": np.array([1.0, 2.0, 4.0]),
            "y": np.array([2.0, 4.0, 8.0]),
            "z": None,
        }

        filtered = pass_filter("low", 5.0, data)
        differentiated = differentiate("x", data)

        np.testing.assert_array_equal(filtered["y"], np.array([2.0, 4.0, 5.0]))
        np.testing.assert_allclose(differentiated["y"], np.array([2.0, 2.0, 2.0]))

    def test_derivative_rejects_duplicate_coordinates(self):
        data = {
            "x": np.array([0.0, 1.0, 1.0]),
            "y": np.array([0.0, 1.0, 2.0]),
            "z": None,
        }

        with self.assertRaisesRegex(ValueError, "must not repeat"):
            differentiate("x", data)

    def test_fill_below_handles_bounded_leading_trailing_and_over_limit_gaps(self):
        data_grid = np.array([
            [1.0, np.nan, 1.0, 1.0],
            [np.nan, np.nan, 2.0, np.nan],
            [np.nan, 3.0, 3.0, np.nan],
            [4.0, 4.0, 4.0, np.nan],
            [5.0, 5.0, 5.0, 5.0],
            [6.0, 6.0, np.nan, 6.0],
            [7.0, 7.0, np.nan, 7.0],
        ])

        result = fill_heatmap("below", {"z": data_grid}, max_depth=2)

        np.testing.assert_array_equal(
            result["z"],
            np.array([
                [1.0, np.nan, 1.0, 1.0],
                [1.0, np.nan, 2.0, np.nan],
                [1.0, 3.0, 3.0, np.nan],
                [4.0, 4.0, 4.0, np.nan],
                [5.0, 5.0, 5.0, 5.0],
                [6.0, 6.0, np.nan, 6.0],
                [7.0, 7.0, np.nan, 7.0],
            ]),
        )

    def test_fill_right_handles_bounded_leading_trailing_and_over_limit_gaps(self):
        data_grid = np.array([
            [1.0, np.nan, np.nan, 4.0, 5.0, 6.0, 7.0],
            [np.nan, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0],
            [1.0, 2.0, 3.0, 4.0, 5.0, np.nan, np.nan],
            [1.0, np.nan, np.nan, np.nan, 5.0, 6.0, 7.0],
        ])

        result = fill_heatmap("right", {"z": data_grid}, max_depth=2)

        np.testing.assert_array_equal(
            result["z"],
            np.array([
                [1.0, 1.0, 1.0, 4.0, 5.0, 6.0, 7.0],
                [np.nan, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0],
                [1.0, 2.0, 3.0, 4.0, 5.0, np.nan, np.nan],
                [1.0, np.nan, np.nan, np.nan, 5.0, 6.0, 7.0],
            ]),
        )

    def test_worker_operation_pipeline_fails_atomically(self):
        def failing_operation(_data):
            raise ValueError("bad operation")

        worker = loader.__new__(loader)
        worker.axis_data = {
            "x": np.array([1.0, 2.0]),
            "y": np.array([3.0, 4.0]),
            }
        worker.operations = [
            failing_operation,
            lambda data: {"y": data["y"] * 2},
            lambda data: {"x": data["x"] + 10},
            ]
        with self.assertRaisesRegex(RuntimeError, "bad operation"):
            loader.do_operations(worker)

        np.testing.assert_array_equal(worker.axis_data["x"], [1.0, 2.0])
        np.testing.assert_array_equal(worker.axis_data["y"], [3.0, 4.0])

    def test_large_heatmap_with_operations_is_rejected_before_full_load(self):
        worker = self._sql_heatmap_worker(None)
        worker.max_full_heatmap_points = 10
        worker.operations = [lambda data: data]

        with self.assertRaisesRegex(
                OperationExecutionError,
                "exceeds the full-resolution operation limit",
                ):
            loader._should_use_sql_heatmap(worker)

    def test_heatmap_gridding_preserves_float64_coordinate_and_value_precision(self):
        worker = loader.__new__(loader)
        worker.sampled_heatmap_source = False
        worker.aggregated_heatmap_source = False
        baseline = 1_000_000_000_000.0

        x_axis, _y_axis, data_grid = loader._heatmap_grid_from_arrays(
            worker,
            np.array([baseline, baseline + 1.0]),
            np.array([0.0, 0.0]),
            np.array([baseline + 2.0, baseline + 3.0]),
            max_cells=4,
            )

        self.assertEqual(x_axis.dtype, np.dtype(float))
        self.assertEqual(data_grid.dtype, np.dtype(float))
        self.assertEqual(x_axis[1] - x_axis[0], 1.0)
        self.assertEqual(data_grid[0, 1] - data_grid[0, 0], 1.0)

    def test_derivative_operation_updates_dependent_label_and_unit(self):
        class Param:
            def __init__(self, name, label, unit, depends_on=()):
                self.name = name
                self.label = label
                self.unit = unit
                self.depends_on_ = depends_on

        worker = loader.__new__(loader)
        worker.param = Param("current", "Current", "A", ("gate",))
        worker.display_param = Param("current", "Current", "A", ("gate",))
        worker.axis_param = {
            "x": Param("gate", "Gate voltage", "V"),
            "y": worker.param,
            }
        worker.operations = [
            OperationCall("dy/dx", lambda data: data, derivative_axis="x"),
            ]

        loader._apply_operation_metadata(worker)

        self.assertEqual(worker.display_param.label, "d(Current)/d(Gate voltage)")
        self.assertEqual(worker.display_param.unit, "A/V")
        self.assertIs(worker.axis_param["y"], worker.display_param)

    def test_operated_heatmap_is_aggregated_after_operations(self):
        worker = self._sql_heatmap_worker(None)
        worker.operations = [
            lambda data: {"z": np.gradient(data["z"], data["x"], axis=1)},
            ]
        worker.axis_data = {
            "x": np.array([0.0, 1.0, 2.0, 3.0]),
            "y": np.array([0.0]),
            }
        worker.dataGrid = np.array([[0.0, 1.0, 4.0, 9.0]])
        worker.max_full_heatmap_points = 2
        worker.sampled_heatmap_source = False
        worker.aggregated_heatmap_source = False

        results = loader.do_operations(worker)
        worker.dataGrid = results[2]
        loader._aggregate_operated_heatmap_if_needed(worker)

        np.testing.assert_allclose(worker.dataGrid, [[1.5, 4.5]])
        self.assertEqual(
            worker.heatmap_downsample_info["source_aggregation_strategy"],
            "operations, then spatial mean",
            )

    def test_subtract_mean_operates_by_axis(self):
        data = {
            "x": np.array([0.0, 1.0]),
            "y": np.array([0.0, 1.0]),
            "z": np.array([[1.0, 3.0], [2.0, 4.0]]),
        }

        result = subtract_mean("x", data)

        np.testing.assert_array_equal(result["z"], np.array([[-1.0, 1.0], [-1.0, 1.0]]))

    def test_plot_window_title_uses_database_basename(self):
        class Dataset:
            run_id = 12

        class Param:
            name = "signal"
            label = "Signal"

        window = plotWidget.__new__(plotWidget)
        window._guid = "guid"
        window._dataset_key = DatasetKey("/tmp/qplot/example.db", "guid")
        window._dataset_holder = {window._dataset_key: DatasetHandle(Dataset())}
        window.param = Param()

        self.assertTrue(str(window).startswith("example.db | Run ID: 12"))
