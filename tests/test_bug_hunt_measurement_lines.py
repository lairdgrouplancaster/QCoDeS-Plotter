"""Do not paint distinct integer measurements as identical float coordinates."""

import csv

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from qplot.windows._native_transforms import NativePlotDataItem
from qplot.windows._plot1d_snap import _line_snap_data
from tests.windows.test_native_integer_transforms import (
    integer_plots as _integer_plots_fixture,
)
from tests.windows.test_native_transform_labels import click_control

integer_plots = _integer_plots_fixture


@pytest.mark.parametrize("axis", ["x", "y"])
def test_native_line_rejects_collapsing_render_coordinates(axis):
    exact = np.array([2**63 + index for index in range(4)], dtype=np.uint64)
    ordinary = np.arange(4.)
    x, y = (exact, ordinary) if axis == "x" else (ordinary, exact)
    line = NativePlotDataItem(x=x, y=y)
    assert line.getData()[0].size == 0
    assert "display precision" in line._qplot_precision_error
    assert _line_snap_data(line) is None
    for actual, original in zip(line.getOriginalDataset(), (x, y), strict=True):
        np.testing.assert_array_equal(actual, original)
    if axis == "x":
        line.setSubtractMeanMode(True)
        assert line.getData()[0].size == 0
    line.setFftMode(True) if axis == "x" else line.setSubtractMeanMode(True)
    assert line.getData()[0].size > 0
    assert line._qplot_precision_error is None
    assert _line_snap_data(line) is not None
    if axis == "y":
        np.testing.assert_array_equal(line.getData()[1], [-1.5, -.5, .5, 1.5])


def test_mean_subtraction_cannot_rescue_collapsed_integer_x_with_boolean_y():
    line = NativePlotDataItem(
        x=np.array([2**63, 2**63 + 1, 2**63 + 2], dtype=np.uint64),
        y=np.array([False, True, False]), subtractMeanMode=True,
    )
    assert line.getData()[0].size == 0
    assert "display precision" in line._qplot_precision_error


@pytest.mark.parametrize("values", [np.array([1, 2, 3], dtype=np.int64),
                                    np.array([2**63, 2**63 + 4096, 2**63 + 8192], dtype=np.uint64),
                                    np.full(3, 2**63 + 1, dtype=np.uint64)])
def test_distinguishable_or_constant_integer_lines_still_render(values):
    line = NativePlotDataItem(x=np.arange(3.), y=values)
    np.testing.assert_array_equal(line.getData()[1], values)
    assert getattr(line, "_qplot_precision_error", None) is None


@pytest.mark.parametrize("integer_plots", [("adjacent_uint_y", False)], indirect=True)
def test_real_integer_line_precision_message_recovery_and_raw_export(integer_plots, tmp_path):
    _window, (plot, _source), x, y, _slopes = integer_plots
    assert plot.line.getData()[0].size == 0
    assert "display precision" in plot.plot_state_overlay.detail_label.text()
    assert plot.plot_state_overlay.frame.isVisible()
    target = tmp_path / "original.csv"
    assert plot._write_line_csv_stage(str(target), CSVExporter(plot.plot))
    with target.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    assert [int(row[1]) for row in rows] == list(map(int, y))
    click_control(plot, "subtractMeanCheck")
    np.testing.assert_array_equal(plot.line.getData()[0], x)
    np.testing.assert_array_equal(plot.line.getData()[1], [1.5, .5, -.5, -1.5])
    assert not plot.plot_state_overlay.frame.isVisible()
    click_control(plot, "subtractMeanCheck")
    assert plot.line.getData()[0].size == 0
    assert plot.plot_state_overlay.frame.isVisible()


@pytest.mark.parametrize("integer_plots", [("adjacent_uint_y", False)], indirect=True)
def test_precision_overlay_tracks_secondary_visibility_and_removal(integer_plots):
    from tests.windows.test_merged_trace_metadata import merge

    window, (plot, source), x, _y, _slopes = integer_plots
    # Keep a representable primary and an exact, rejected secondary trace.
    plot.line.setData(x=x, y=np.arange(len(x), dtype=float))
    key, secondary = merge(window, plot, source, x_axis="Top", y_axis="Right")
    assert plot.plot_state_overlay.frame.isVisible()
    style = plot._trace_styles[key]
    style.visible = False
    plot._apply_trace_style(key, secondary)
    assert not plot.plot_state_overlay.frame.isVisible()
    style.visible = True
    plot._apply_trace_style(key, secondary)
    assert plot.plot_state_overlay.frame.isVisible()
    plot.remove_line(plot._trace_display_label(key, secondary), key)
    assert not plot.plot_state_overlay.frame.isVisible()


@pytest.mark.parametrize("integer_plots", [("adjacent_uint_y", False)], indirect=True)
def test_precision_recovery_preserves_newer_unrelated_error(integer_plots):
    _window, (plot, _source), _x, _y, _slopes = integer_plots
    plot.show_plot_state("Source read failed", "A newer error", kind="error")
    plot.line.setVisible(False)
    plot._update_native_precision_status()
    assert plot.plot_state_overlay.frame.isVisible()
    assert plot.plot_state_overlay.title_label.text() == "Source read failed"
