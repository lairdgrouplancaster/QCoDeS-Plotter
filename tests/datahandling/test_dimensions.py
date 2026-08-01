import unittest

from qplot.datahandling.dimensions import (
    UnsupportedPlotDimensionError,
    ensure_supported_plot_dimensions,
    unsupported_plot_message,
)


class PlotDimensionTestCase(unittest.TestCase):
    def test_one_and_two_dimensional_measurements_are_supported(self):
        self.assertEqual(ensure_supported_plot_dimensions("line", ["x"]), ("x",))
        self.assertEqual(
            ensure_supported_plot_dimensions("map", ["x", "y"]),
            ("x", "y"),
            )

    def test_higher_dimensional_measurement_is_rejected_with_axes(self):
        with self.assertRaisesRegex(
                UnsupportedPlotDimensionError,
                r"signal has 3 independent axes \(x, y, z\)",
                ):
            ensure_supported_plot_dimensions("signal", ["x", "y", "z"])

        self.assertIn(
            "explicit slice or projection",
            unsupported_plot_message("signal", ["x", "y", "z"]),
            )


if __name__ == "__main__":
    unittest.main()
