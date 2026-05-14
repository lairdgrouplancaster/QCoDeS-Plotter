import unittest

import numpy as np

from qplot.configuration.scripts import try_as_num
from qplot.windows import _plotWin as plotwin_module
from qplot.windows._plotWin import plotWidget
from qplot.tools.general import data2matrix
from qplot.tools.plot_tools import differentiate, pass_filter, subtract_mean


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
        window._dataset_holder = {
            "guid": {
                "dataset": Dataset(),
                "del_timer": None,
                }
            }
        window.param = Param()

        try:
            self.assertTrue(str(window).startswith("example.db | Run ID: 12"))
        finally:
            plotwin_module.get_DB_location = old_get_db_location


