"""Exact acquired values must not silently merge in heatmap renderers."""

import csv
from decimal import Decimal

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from qplot.windows._display_precision import float_display_loses_distinctions
from qplot.windows._plot2d_layers import HeatmapLayer
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_integration import wait_for


@pytest.mark.parametrize("values,loses", [
    (np.array([2**63 + 1, 2**63 + 2], dtype=np.uint64), True),
    (np.array([-(2**63) + 1, -(2**63) + 2], dtype=np.int64), True),
    (np.array([2**63 + 1, 2**63 + 1], dtype=np.uint64), False),
    (np.array([2**63, 2**63 + 4096], dtype=np.uint64), False),
    (np.array([2**52, 2**52 + 1], dtype=np.int64), False),
    (np.array([Decimal(2**52), Decimal(2**52) + Decimal('.5')]), True),
    (np.array([np.nan, 1., 2.]), False),
    (np.array([1., 2., 3.]), False),
])
def test_display_precision_checks_distinct_values(values, loses):
    assert float_display_loses_distinctions(values) is loses


@pytest.mark.parametrize("nonuniform", [False, True])
@pytest.mark.parametrize("dtype,base", [(np.uint64, 2**63), (np.int64, 2**62)])
def test_exact_heatmap_rejects_collapsed_colors_and_recovers_after_subtraction(
    tmp_path, monkeypatch, nonuniform, dtype, base
):
    x = np.array([0., 1., 3., 7.]) if nonuniform else np.arange(4.)
    values = np.array([[base + 11 + 8 * row + column for column in range(4)]
                       for row in range(2)], dtype=dtype)
    records = [{"x": x, "slow": np.full(4, row), "signal": values[row]}
               for row in range(2)]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (main, plot):
        np.testing.assert_array_equal(plot.dataGrid, values)
        assert plot._heatmap_color_range_rejected
        assert not plot.image.isVisible()
        assert not plot.heatmap_mesh.isVisible()
        assert "display precision" in plot.plot_state_overlay.title_label.text()
        assert plot._qplot_display_synchronized
        destination = tmp_path / "exact.csv"
        assert plot._write_heatmap_csv_stage(str(destination), CSVExporter(plot.plot))
        with destination.open(newline="") as handle:
            rows = list(csv.reader(handle))
        assert [int(row[2]) for row in rows[1:]] == values.ravel().tolist()

        plot.z_index = [0, 0]
        plot.openSweep("h")
        cut = main.windows[-1]
        wait_for(lambda: not cut.worker.running)
        np.testing.assert_array_equal(cut.line.getOriginalDataset()[1], values[0])

        layer = HeatmapLayer(plot, plot)
        try:
            assert layer.geometry is None
            assert not layer.image.isVisible() and not layer.heatmap_mesh.isVisible()
            assert "display precision" in layer.compatibility_error
            np.testing.assert_array_equal(layer.data_grid, values)
            operation_option(plot, "Subtract Row Mean").input.setChecked(True)
            apply_operations(plot)
            assert not plot._heatmap_color_range_rejected
            assert plot.image.isVisible() != nonuniform
            assert plot.heatmap_mesh.isVisible() == nonuniform
            assert layer.geometry is not None
            assert layer.compatibility_error is None
            expected = np.tile([-1.5, -.5, .5, 1.5], (2, 1))
            np.testing.assert_array_equal(plot.dataGrid, expected)
            np.testing.assert_array_equal(layer.data_grid, expected)
        finally:
            layer.disconnect_source_updates()
            layer.remove_renderers()


@pytest.mark.parametrize("dtype", [np.int64, np.float64])
def test_ordinary_heatmap_values_still_render(tmp_path, monkeypatch, dtype):
    values = np.arange(8, dtype=dtype).reshape(2, 4)
    records = [{"x": np.arange(4.), "slow": np.full(4, row), "signal": values[row]}
               for row in range(2)]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (_main, plot):
        assert not plot._heatmap_color_range_rejected
        assert plot.image.isVisible()
        np.testing.assert_array_equal(plot.image.image, values)
