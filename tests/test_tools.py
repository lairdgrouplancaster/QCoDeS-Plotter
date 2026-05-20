import sqlite3
import tempfile
import unittest

import numpy as np

import qplot.tools.worker as worker_module
from qplot.configuration.scripts import try_as_num
from qplot.tools.general import data2matrix
from qplot.tools.plot_tools import differentiate, pass_filter, subtract_mean
from qplot.tools.worker import loader
from qplot.windows import _plotWin as plotwin_module
from qplot.windows._dataset_handle import DatasetHandle
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

    def test_large_heatmap_sql_loader_uses_bounded_sample_and_grid(self):
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

            self.assertTrue(worker.loaded_from_sql_sample)
            self.assertFalse(worker.read_data)
            self.assertLessEqual(worker.loaded_point_count, 60)
            self.assertLessEqual(worker.dataGrid.size, 16)
            self.assertGreater(np.isfinite(worker.dataGrid).sum(), 0)

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

    def _create_heatmap_table(self, database_path):
        conn = sqlite3.connect(database_path)
        try:
            conn.execute("CREATE TABLE results (x REAL, y REAL, signal REAL)")
            conn.executemany(
                "INSERT INTO results (x, y, signal) VALUES (?, ?, ?)",
                [
                    (float(x), float(y), float(x + y * 100))
                    for y in range(30)
                    for x in range(40)
                    ],
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

        worker = loader.__new__(loader)
        worker.cache = object()
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

    def test_worker_operations_continue_after_one_operation_fails(self):
        class Signal:
            def __init__(self):
                self.values = []

            def emit(self, value):
                self.values.append(value)

        class Emitter:
            def __init__(self):
                self.errorOccurred = Signal()

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
        worker.emitter = Emitter()

        result = loader.do_operations(worker)

        self.assertEqual(len(worker.emitter.errorOccurred.values), 1)
        np.testing.assert_array_equal(result[0], np.array([11.0, 12.0]))
        np.testing.assert_array_equal(result[1], np.array([6.0, 8.0]))
        self.assertIsNone(result[2])

    def test_subtract_mean_operates_by_axis(self):
        data = {
            "x": np.array([0.0, 1.0]),
            "y": np.array([0.0, 1.0]),
            "z": np.array([[1.0, 3.0], [2.0, 4.0]]),
        }

        result = subtract_mean("x", data)

        np.testing.assert_array_equal(result["z"], np.array([[-1.0, 1.0], [-1.0, 1.0]]))

    def test_plot_window_title_uses_database_basename(self):
        old_get_db_location = plotwin_module.get_DB_location
        plotwin_module.get_DB_location = lambda: "/tmp/qplot/example.db"

        class Dataset:
            run_id = 12

        class Param:
            name = "signal"
            label = "Signal"

        window = plotWidget.__new__(plotWidget)
        window._guid = "guid"
        window._dataset_holder = {"guid": DatasetHandle(Dataset())}
        window.param = Param()

        try:
            self.assertTrue(str(window).startswith("example.db | Run ID: 12"))
        finally:
            plotwin_module.get_DB_location = old_get_db_location
